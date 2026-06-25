"""Gemini web-UI driver layered on top of :class:`ChromeCDPClient`.

Scope (PR9.B):

- Drive an *already-open, already-logged-in* Gemini tab in the
  operator's local browser. Never opens a new tab. Never switches the
  model. Never activates the tab.
- Selectors and state-machine signals are derived from the empirical
  probe results in ``docs/PR9_GEMINI_DOM_PROBE_RESULT.md``.
- All Runtime.evaluate expressions are synchronous; any waiting is done
  in Python with short repeated probes (the probe doc §5/§6 found that
  ``await new Promise`` inside ``Runtime.evaluate`` can trigger
  "Promise was collected" under some Chromium builds).
- On any raised error the driver attempts a tab screenshot to
  ``dist/orchestration/screenshots/`` so the operator can debug from the
  audit log alone.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lib.invisible_chrome_cdp_client import (
    CdpError,
    CdpEvalError,
    ChromeCDPClient,
    PageInfo,
    PageSession,
)

# ---------------------------------------------------------------------------
# Constants — selectors & magic numbers (see PR9_GEMINI_DOM_PROBE_RESULT.md)
# ---------------------------------------------------------------------------

GEMINI_URL_FRAGMENT = "gemini.google.com"

# Probe doc §1: input is a Quill editor inside <rich-textarea>.
INPUT_SELECTOR = 'rich-textarea div.ql-editor[contenteditable="true"]'

# Send button. Gemini churns this composer DOM repeatedly — keep it resilient:
#   - originally ``button.send-button``
#   - 2026-06-13 (Chrome 148): a Material ``mdc-icon-button`` (mat-icon
#     fonticon="arrow_upward") inside ``.send-button-container``
#   - 2026-06-23 (re-verified live): wrapper renamed to ``.send-button`` (classes
#     ``send-button has-input submit``); ``.send-button-container`` is gone.
# The button only renders once the composer has text (an empty composer shows
# the mic) and exposes its disabled state via the ``disabled`` property (NOT
# aria-disabled). submit() tries these wrappers in order, then falls back to the
# send/arrow icon's closest <button>, so the next rename degrades gracefully.
# NOTE for wait_for_streaming_done: the same button morphs to fonticon="stop"
# while generating; completion is network-event based (loadingFinished) with the
# model-response innerText-stability path as fallback.
SEND_BUTTON_SELECTOR = ".send-button button"

# Probe doc §6: each model reply lives under a <model-response>; the
# rendered Markdown sits at .markdown inside it. The legacy DingTalk
# helper "Gemini 说" wrapping must be skipped — we go straight to .markdown.
MODEL_RESPONSE_SELECTOR = "model-response"
MARKDOWN_SELECTOR = ".markdown"

# A/B comparison mode (observed live 2026-06-24): Gemini sometimes returns TWO
# candidate answers in a <dual-model-response> / <response-selection-panel>
# ("哪一个回答更实用?" 选项A/选项B) and BLOCKS the conversation until one is picked.
# In this mode there is NO <model-response> — the answers live in
# `.markdown.markdown-main-panel` inside the panel, and a `button.select-button`
# ("此回答更实用") under each option resolves it. The driver reads option A and
# clicks its select-button to collapse the panel to a normal response (which both
# extracts the answer and unblocks any follow-up turn). Intermittent (an A/B
# rollout/experiment), so completion + extraction must handle BOTH shapes.
AB_PANEL_SELECTOR = "response-selection-panel, dual-model-response"
AB_OPTION_MARKDOWN_SELECTOR = ".markdown.markdown-main-panel, .markdown"
AB_SELECT_BUTTON_SELECTOR = "button.select-button"

# Pro model probe (verified live on Brave 9222 during PR9.B Step 2):
# document.querySelector('button.input-area-switch')?.innerText === "Pro"
# (button.aria-label is "打开模式选择器" — read-only check, do NOT click)
MODEL_SWITCH_BUTTON_SELECTOR = "button.input-area-switch"
PRO_MODEL_TOKENS: tuple[str, ...] = ("Pro",)

# MutationObserver namespacing (probe doc §7).
WINDOW_OBSERVER_KEY = "__pr9_observer"
WINDOW_STATE_KEY = "__pr9_state"

# Injection tuning — see ``inject_prompt`` docstring.
#
# History of attempted schemes (PR9.C diagnostics on Brave 9222):
#
# * Single execCommand('insertText') of a ~150 KB Quill payload blocks
#   the renderer for ~105 s, far past the WS recv window.
# * Python-side N×8 KB chunks + N round-trips dropped ~18 % of the
#   chars and produced ~2.5 k extra empty paragraphs (chunk seams at
#   ``\n``).
# * ClipboardEvent('paste') with DataTransfer is rejected by Quill
#   (``isTrusted=false``); empirically verified in
#   ``/tmp/pr9c_probe/probe_paste_event.py``.
# * Single Runtime.evaluate that internally awaits between chunks: when
#   cleanup (selectAll+delete) and the chunked insertText were in the
#   same async function, the first chunk replaced the still-selected
#   region and subsequent execCommand calls returned false. When
#   cleanup was hoisted to a separate evaluate, the single evaluate
#   ran fine for the first ~13 chunks but a 1 KB × 51 chunk variant
#   exceeded the WS recv timeout (240 s).
#
# Scheme finally adopted (PR9.C Task 3.0 方向 2 / Branch B): cleanup is
# its own Runtime.evaluate (with verification) and each chunk is its own
# Runtime.evaluate. The natural Python ↔ Chromium round-trip (~10 ms +
# our explicit 50 ms drain) gives Quill enough time to flush its
# composition listeners and Delta queue between chunks. A 13 × 4 KB
# probe completed in 33 s with markers and paragraphs both PASS.
_INJECT_CHUNK_SIZE = 4000
# Per-chunk Runtime.evaluate timeout. Each ~4 KB execCommand averages
# 3-4 s on Brave; 60 s is a generous ceiling that still catches a
# hung/lost frame.
_INJECT_PER_CHUNK_TIMEOUT_S = 60.0
# Cleanup evaluate timeout (selectAll+delete loop is fast; 30 s is
# defensive).
_INJECT_CLEANUP_TIMEOUT_S = 30.0
# Final readback evaluate timeout. The page may be busy if the previous
# chunk just finished, so allow a generous window.
_INJECT_READBACK_TIMEOUT_S = 60.0
# Python-side sleep between successive chunk evaluates. 50 ms was the
# value that PASSED the 50 KB / 13 chunk probe; lower values were
# untested and may race the Delta queue.
_INJECT_INTER_CHUNK_DRAIN_S = 0.05
# Cleanup loop: how many selectAll+delete batches to attempt before
# giving up. Each batch issues 5 selectAll/delete pairs internally.
_INJECT_CLEANUP_MAX_ATTEMPTS = 5
# Cleanup is considered successful when innerText is at most this many
# characters (Quill leaves a sentinel ``\n`` even on a fully cleared
# input).
_INJECT_CLEANUP_MAX_RESIDUAL = 1
# Strict paragraph count ceiling: ``actual_p_count <= expected_lines + N``.
# Allows tiny normalisation (Quill may add a single trailing empty
# paragraph) while catching the regression where chunk seams at ``\n``
# produced thousands of bogus paragraphs.
_INJECT_PARAGRAPH_TOLERANCE = 5
# Per-newline length overhead Quill adds when consuming a multi-line
# insertText. Empirically each ``\n`` in the input causes Quill to
# render *two* characters in innerText (one for the source ``\n`` and
# one synthesised paragraph terminator). The expected actual_len is
# therefore ``len(prompt) + n * (expected_lines - 1)`` for some integer
# ``n`` that depends on Quill build (1 in our probe). Rather than
# hard-code Quill's overhead constant we accept any actual_len in the
# range ``[expected_len, expected_len + expected_lines * MAX_PER_NL]``.
_INJECT_LEN_PER_NL_MAX = 2

SCREENSHOT_DIR = Path("dist/orchestration/screenshots")

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GeminiDriverError(RuntimeError):
    """Base error for the Gemini driver layer."""

class GeminiTabNotFoundError(GeminiDriverError):
    """No open tab on ``gemini.google.com``; the operator must open one."""

class WrongModelError(GeminiDriverError):
    """The currently selected Gemini model is not Pro."""

class DomDriftError(GeminiDriverError):
    """A required selector returned no element — likely UI redesign."""

class InjectionFailedError(GeminiDriverError):
    """``execCommand('insertText')`` did not write the expected payload."""

class SubmitFailedError(GeminiDriverError):
    """The send button could not be clicked (still aria-disabled)."""

class StreamingTimeoutError(GeminiDriverError):
    """Streaming did not finish within the allotted timeout."""

class ExtractionFailedError(GeminiDriverError):
    """The new assistant reply could not be extracted."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def _eval_value(session: PageSession, expression: str, *, timeout: float = 30.0):
    """Run ``Runtime.evaluate`` and return the unwrapped JS value.

    Always uses ``returnByValue=True`` and the synchronous form (no
    ``awaitPromise``) — see module docstring for why.
    """
    res = session.runtime_evaluate(
        expression, await_promise=False, return_by_value=True, timeout=timeout
    )
    inner = res.get("result", {})
    return inner.get("value")

def _save_screenshot(session: PageSession, step_label: str) -> Optional[str]:
    """Best-effort screenshot saver; never raises (we're already failing)."""
    try:
        png = session.capture_screenshot(format="png")
    except CdpError:
        return None
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{step_label}_{_utc_stamp()}.png"
    try:
        path.write_bytes(png)
    except OSError:
        return None
    return str(path)

def _js_string_literal(text: str) -> str:
    """Return a JS string literal of ``text`` safe for inline ``evaluate``.

    Using ``json.dumps`` is the cheap-and-correct way to escape backslashes,
    quotes, newlines, control chars, and unicode for embedding in JS source.
    """
    return json.dumps(text, ensure_ascii=False)


def _split_into_safe_chunks(text: str, target_size: int) -> list[str]:
    """Split ``text`` into chunks of ~``target_size`` characters, never
    placing a chunk seam at a ``\\n``.

    Why this exists (PR9.C Task 3.0 Step C diagnosis): the previous
    fixed-stride splitter put roughly 1-in-N seams on newline characters
    because the prompt is markdown with thousands of line breaks. Quill
    treats each ``insertText`` whose tail is ``\\n`` as a paragraph
    terminator, so the seam at a ``\\n`` produced an extra empty
    paragraph per chunk — ~2.5 k bogus paragraphs in our 200 KB sample.

    Algorithm: walk the text in ``target_size`` strides; whenever the
    proposed boundary lands on a ``\\n`` (or sits between two
    consecutive ``\\n`` characters), shift it backwards to the closest
    non-newline character. If that would empty the chunk, shift
    forwards instead. The final chunk takes whatever remains.

    Invariants (verified by unit tests):
      * ``"".join(result) == text``
      * ``len(c) > 0`` for every chunk
      * For 0 ≤ i < len(result)-1, ``result[i][-1] != "\\n"`` and
        ``result[i+1][0] != "\\n"`` is **not** required (we tolerate
        leading ``\\n`` on the next chunk because Quill keeps that as
        text inside the existing paragraph; the harmful case is only
        the trailing ``\\n`` on the previous chunk).
      * ``len(c) <= target_size + 1`` (allow one-char overshoot when we
        slide forward to escape a seam-on-newline).
    """
    if target_size < 1:
        raise ValueError("target_size must be >= 1")
    if not text:
        return []

    chunks: list[str] = []
    n = len(text)
    pos = 0
    while pos < n:
        end = pos + target_size
        if end >= n:
            chunks.append(text[pos:])
            break
        # Slide ``end`` backwards while it points at the character
        # immediately *after* a newline (i.e. the previous char is
        # ``\n``, which would be the chunk's last character).
        while end > pos + 1 and text[end - 1] == "\n":
            end -= 1
        # If we collapsed all the way back to (pos+1) it means the whole
        # window is newlines; slide forwards past them instead so the
        # seam still avoids landing on a ``\n`` tail.
        if end <= pos + 1:
            end = pos + target_size
            while end < n and text[end - 1] == "\n":
                end += 1
            # If even forward-walk hits EOF only newlines remain — emit
            # them as the final chunk.
            if end >= n:
                chunks.append(text[pos:])
                break
        chunks.append(text[pos:end])
        pos = end
    return chunks

# ---------------------------------------------------------------------------
# GeminiWebDriver
# ---------------------------------------------------------------------------

class GeminiWebDriver:
    """High-level Gemini tab driver.

    Each public method that can raise will save a screenshot before
    raising so the orchestrator's failure record can point an operator at
    a concrete artifact.
    """

    def __init__(self, cdp_client: ChromeCDPClient) -> None:
        self._cdp = cdp_client

    # ----- tab discovery -------------------------------------------------

    def find_gemini_tab(self) -> PageSession:
        """Return a :class:`PageSession` attached to the Gemini tab.

        Raises :class:`GeminiTabNotFoundError` if no such tab exists.
        We do **not** create one — that would steal focus and violate
        the chrome-cdp invisible principle.
        """
        candidates: list[PageInfo] = [
            p for p in self._cdp.list_pages()
            if GEMINI_URL_FRAGMENT in (p.url or "")
        ]
        if not candidates:
            raise GeminiTabNotFoundError(
                "No tab with URL containing "
                f"{GEMINI_URL_FRAGMENT!r} is open in the local browser. "
                "Open https://gemini.google.com/app, sign in, and switch "
                "the model to Pro before running this workflow."
            )
        # If the operator has multiple Gemini tabs, prefer the most
        # recently opened (CDP /json returns newest first in practice on
        # Chromium; we don't depend on it being authoritative — first one
        # wins is acceptable).
        return self._cdp.attach_to_page(candidates[0].id)

    # ----- model & DOM health checks ------------------------------------

    def assert_pro_model(self, session: PageSession) -> None:
        """Read-only check that the active Gemini model is Pro.

        We resolve the model name from ``button.input-area-switch``'s
        ``innerText`` (verified live on Brave 9222, PR9.B Step 2). If the
        UI changes layout we surface a ``WrongModelError`` rather than a
        cryptic null — the operator must switch manually; the driver
        **never** clicks the model selector.
        """
        expr = (
            "(function(){"
            f"  var el=document.querySelector({_js_string_literal(MODEL_SWITCH_BUTTON_SELECTOR)});"
            "  return el ? (el.innerText || '').trim() : null;"
            "})()"
        )
        # Retry the read: right after navigation the model-switch button label
        # intermittently reads back empty (hydration timing), which used to raise
        # a spurious "did not return a label". Poll up to ~6s for a non-empty label.
        label = None
        last_exc = None
        deadline = time.monotonic() + 6.0
        while True:
            try:
                label = _eval_value(session, expr)
                last_exc = None
            except CdpError as exc:
                last_exc = exc
            if isinstance(label, str) and label:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.8)
        if last_exc is not None and not (isinstance(label, str) and label):
            self._screenshot_and_raise(
                session, "assert_pro_model",
                WrongModelError(
                    f"Could not read model selector ({last_exc}). "
                    "Switch Gemini model to Pro manually before running."
                ),
            )
            return  # unreachable; satisfies type checkers
        if not isinstance(label, str) or not label:
            self._screenshot_and_raise(
                session, "assert_pro_model",
                WrongModelError(
                    "Gemini model selector did not return a label; "
                    "open the Gemini tab and switch to Pro manually."
                ),
            )
            return
        if not any(token in label for token in PRO_MODEL_TOKENS):
            self._screenshot_and_raise(
                session, "assert_pro_model",
                WrongModelError(
                    f"Gemini model is {label!r}; switch to Pro before "
                    "running PR9 workflow (the driver does not auto-switch)."
                ),
            )

    def probe_dom_health(self, session: PageSession) -> dict:
        """Sanity check that input + send button selectors still resolve.

        Returns a small diagnostic dict; raises :class:`DomDriftError` if
        any required element is missing.
        """
        expr = (
            "(function(){"
            f"  var input=document.querySelector({_js_string_literal(INPUT_SELECTOR)});"
            f"  var send=document.querySelector({_js_string_literal(SEND_BUTTON_SELECTOR)});"
            "  return {"
            "    input_present: !!input,"
            "    send_present: !!send,"
            "    send_aria_disabled: send ? send.getAttribute('aria-disabled') : null"
            "  };"
            "})()"
        )
        try:
            info = _eval_value(session, expr)
        except CdpError as exc:
            self._screenshot_and_raise(
                session, "probe_dom_health",
                DomDriftError(f"DOM probe failed at evaluate: {exc}"),
            )
            return {}
        if not isinstance(info, dict):
            self._screenshot_and_raise(
                session, "probe_dom_health",
                DomDriftError(f"DOM probe returned unexpected value: {info!r}"),
            )
            return {}
        if not info.get("input_present") or not info.get("send_present"):
            self._screenshot_and_raise(
                session, "probe_dom_health",
                DomDriftError(
                    f"Required selectors missing: {info!r}. Gemini UI "
                    "may have changed; re-run PR9.B Step 1 probes."
                ),
            )
        return info

    # ----- prompt injection ---------------------------------------------

    def inject_prompt(self, session: PageSession, prompt: str) -> None:
        """Write ``prompt`` into the Gemini Quill input box.

        Architecture (PR9.C Task 3.0 方向 2 / Branch B — Python multi-eval):

        Sequence:
          1. Cleanup evaluate: ``selectAll+delete`` × N, verify
             ``innerText`` length is at most ``_INJECT_CLEANUP_MAX_RESIDUAL``.
             If cleanup never converges we raise — refusing to inject on
             top of pre-existing user content.
          2. Per-chunk evaluate: split the prompt into chunks of
             ``_INJECT_CHUNK_SIZE`` chars (boundary never on ``\\n``),
             then for each chunk issue **its own** ``Runtime.evaluate``
             that does a single ``execCommand('insertText', chunk)``.
             Sleep ``_INJECT_INTER_CHUNK_DRAIN_S`` between chunks so
             Quill's Delta queue drains before the next chunk starts.
          3. Readback evaluate: read ``innerText.length`` and the count
             of ``<p>`` nodes once, then enforce two strict invariants
             (see below).

        Why this shape (vs single-evaluate alternatives):
          * Single ``execCommand`` of a 100 KB blob blocks the renderer
            for ~100 s, way past the WS recv timeout.
          * Single ``Runtime.evaluate`` with internal awaits between
            chunks raced Quill's selection state — the first chunk
            replaced the still-selected region from the inline cleanup.
          * 1 KB × 51 chunks in a single evaluate exceeded 240 s.
          * Programmatic ``ClipboardEvent('paste')`` is rejected by
            Quill (``isTrusted=false``).

        Verified empirically by ``/tmp/pr9c_probe/probe_b_only.py``
        (50 KB, 13 × 4 KB chunks, 33 s, markers + paragraphs PASS).

        Strict post-injection invariants:
          * ``expected_len <= actual_len <= expected_len +
            expected_lines * _INJECT_LEN_PER_NL_MAX`` — Quill renders
            each input ``\\n`` with one or two characters of overhead
            in ``innerText``; anything outside this window indicates
            either silent truncation or a runaway insertion.
          * ``paragraph_count <= expected_lines +
            _INJECT_PARAGRAPH_TOLERANCE`` — catches the regression
            where seams at ``\\n`` produced thousands of bogus paragraphs.

        Failure: a screenshot is captured and ``InjectionFailedError``
        is raised. We do NOT click submit, ever.
        """
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a str")
        if not prompt:
            raise ValueError("prompt is empty; refusing to inject")

        # Step 0 — wait for the composer to be interactive. After a navigation
        # the Gemini SPA hydrates asynchronously; a fixed hydration sleep was
        # too short intermittently, surfacing as "cleanup verify could not find
        # input". Poll for the Quill input before touching it.
        self._wait_for_input_ready(session)

        # Step 1 — cleanup, with verification.
        self._cleanup_input_with_verify(session)

        # Step 2 — per-chunk insertText, one Runtime.evaluate per chunk.
        chunks = _split_into_safe_chunks(prompt, _INJECT_CHUNK_SIZE)
        chunk_count = len(chunks)
        total = len(prompt)
        expected_lines = prompt.count("\n") + 1

        for idx, chunk in enumerate(chunks):
            chunk_literal = _js_string_literal(chunk)
            chunk_expr = (
                "(function(){"
                f"  var el=document.querySelector({_js_string_literal(INPUT_SELECTOR)});"
                "  if(!el) return {ok:false, reason:'input_not_found'};"
                "  el.focus();"
                f"  var ok = document.execCommand('insertText', false, {chunk_literal});"
                "  return {"
                "    ok: !!ok,"
                "    running_len: (el.innerText || '').length"
                "  };"
                "})()"
            )
            try:
                chunk_res = _eval_value(
                    session, chunk_expr, timeout=_INJECT_PER_CHUNK_TIMEOUT_S
                )
            except CdpError as exc:
                self._screenshot_and_raise(
                    session, "inject_prompt",
                    InjectionFailedError(
                        f"chunk {idx}/{chunk_count} evaluate failed: {exc}"
                    ),
                )
                return
            if not isinstance(chunk_res, dict) or not chunk_res.get("ok"):
                self._screenshot_and_raise(
                    session, "inject_prompt",
                    InjectionFailedError(
                        f"chunk {idx}/{chunk_count} insertText returned "
                        f"{chunk_res!r}"
                    ),
                )
                return
            # Drain pause so Quill can flush its Delta queue before the
            # next chunk's evaluate arrives. Skip on the final iteration
            # — the readback evaluate provides its own settle window.
            if idx < chunk_count - 1:
                time.sleep(_INJECT_INTER_CHUNK_DRAIN_S)

        # Step 3 — readback in a single evaluate so we get atomically
        # consistent length + paragraph count.
        readback_expr = (
            "(function(){"
            f"  var el=document.querySelector({_js_string_literal(INPUT_SELECTOR)});"
            "  if(!el) return {ok:false, reason:'input_not_found'};"
            "  var actual = el.innerText || '';"
            "  return {"
            "    ok: true,"
            "    actual_len: actual.length,"
            "    paragraph_count: el.querySelectorAll('p').length,"
            "    scroll_height: el.scrollHeight"
            "  };"
            "})()"
        )
        try:
            value = _eval_value(
                session, readback_expr, timeout=_INJECT_READBACK_TIMEOUT_S
            )
        except CdpError as exc:
            self._screenshot_and_raise(
                session, "inject_prompt",
                InjectionFailedError(
                    f"readback evaluate failed after {chunk_count} chunks: {exc}"
                ),
            )
            return
        if not isinstance(value, dict):
            self._screenshot_and_raise(
                session, "inject_prompt",
                InjectionFailedError(
                    f"readback returned non-dict value: {value!r}"
                ),
            )
            return

        if not value.get("ok"):
            self._screenshot_and_raise(
                session, "inject_prompt",
                InjectionFailedError(
                    f"readback returned ok=false after {chunk_count} chunks; "
                    f"value={value!r}"
                ),
            )
            return

        actual_len = value.get("actual_len")
        paragraph_count = value.get("paragraph_count")
        scroll_height = value.get("scroll_height")

        # Quill renders each input ``\n`` with a small per-newline
        # overhead in ``innerText`` (1-2 chars on the builds we tested).
        # Anything outside [expected_len, expected_len + n_newlines * MAX]
        # is either silent truncation or runaway insertion. We use
        # ``expected_lines`` (not ``expected_lines - 1``) as the ceiling
        # multiplier so single-line prompts also tolerate one Quill-added
        # trailing newline.
        len_floor = total
        len_ceiling = total + expected_lines * _INJECT_LEN_PER_NL_MAX
        if (
            not isinstance(actual_len, int)
            or actual_len < len_floor
            or actual_len > len_ceiling
        ):
            self._screenshot_and_raise(
                session, "inject_prompt",
                InjectionFailedError(
                    f"length out of range: expected in "
                    f"[{len_floor}, {len_ceiling}] "
                    f"(total={total} + lines={expected_lines} * "
                    f"{_INJECT_LEN_PER_NL_MAX}), got {actual_len!r} "
                    f"after {chunk_count} chunks; "
                    f"paragraphs={paragraph_count!r} scrollH={scroll_height!r}"
                ),
            )
            return

        ceiling_paragraphs = expected_lines + _INJECT_PARAGRAPH_TOLERANCE
        if not isinstance(paragraph_count, int) or paragraph_count > ceiling_paragraphs:
            self._screenshot_and_raise(
                session, "inject_prompt",
                InjectionFailedError(
                    f"paragraph ceiling breached: expected<={ceiling_paragraphs} "
                    f"(={expected_lines}+{_INJECT_PARAGRAPH_TOLERANCE}), "
                    f"got {paragraph_count!r} after {chunk_count} chunks; "
                    f"len={actual_len!r} scrollH={scroll_height!r}"
                ),
            )
            return

    # ----- submit --------------------------------------------------------

    def submit(self, session: PageSession) -> None:
        """Click the send button. Refuses to click if it's missing or disabled.

        Resilient to Gemini's recurring composer churn: try the known
        send-button wrappers in order, then fall back to the send/arrow icon's
        closest <button>. Treats BOTH the ``disabled`` property and
        ``aria-disabled`` as "not clickable" (the Material button uses
        ``disabled``)."""
        expr = (
            "(function(){"
            "  var sels=['.send-button button','.send-button-container button','button.send-button'];"
            "  var btn=null,i;"
            "  for(i=0;i<sels.length;i++){btn=document.querySelector(sels[i]);if(btn)break;}"
            "  if(!btn){"
            "    var ic=document.querySelector('mat-icon[fonticon=\"send\"],mat-icon[fonticon=\"arrow_upward\"]');"
            "    if(ic)btn=ic.closest('button');"
            "  }"
            "  if(!btn) return {ok:false, reason:'send_btn_not_found'};"
            "  if(btn.disabled || btn.getAttribute('aria-disabled')==='true')"
            "    return {ok:false, reason:'disabled', cls:[...btn.classList]};"
            "  btn.click();"
            "  return {ok:true, matched: btn.getAttribute('aria-label')};"
            "})()"
        )
        try:
            # Un-throttle background-tab rendering (no window raise) FIRST so the
            # reply actually paints into the DOM for extraction; before
            # enable_network so visibility-resume traffic stays out of the capture.
            session.set_focus_emulation(True)
            # Then enable Network so wait_for_streaming_done can observe the
            # chat-stream request + loadingFinished (not replayed; clears stale).
            session.enable_network()
            res = _eval_value(session, expr)
        except CdpError as exc:
            self._screenshot_and_raise(
                session, "submit",
                SubmitFailedError(f"submit evaluate failed: {exc}"),
            )
            return
        if not isinstance(res, dict) or not res.get("ok"):
            self._screenshot_and_raise(
                session, "submit",
                SubmitFailedError(f"send button not clickable: {res!r}"),
            )

    # ----- streaming wait -----------------------------------------------

    def baseline_response_count(self, session: PageSession) -> int:
        """Return the current number of <model-response> elements.

        Capture this *before* :meth:`submit` so :meth:`extract_last_assistant_message`
        can verify the new reply actually appeared.
        """
        expr = (
            f"document.querySelectorAll({_js_string_literal(MODEL_RESPONSE_SELECTOR)}).length"
        )
        try:
            n = _eval_value(session, expr)
        except CdpError as exc:
            raise GeminiDriverError(
                f"baseline_response_count evaluate failed: {exc}"
            ) from exc
        if not isinstance(n, int) or n < 0:
            raise GeminiDriverError(
                f"baseline_response_count returned unexpected value: {n!r}"
            )
        return n

    def wait_for_streaming_done(
        self,
        session: PageSession,
        *,
        timeout_seconds: int = 300,
        baseline_count: int = 0,
    ) -> None:
        """Block until Gemini finishes streaming the current reply.

        Hybrid signal. Gemini's transport does NOT expose a clean answer-
        completion request — Pro Extended fires a separate "thinking"
        StreamGenerate that ``loadingFinishes`` ~10 s before the answer renders,
        and the send-button fonticon does not reliably reset. So the chat-stream
        POST (``.../StreamGenerate``) reaching ``loadingFinished`` is used only
        as the "generation started/progressing" GATE; completion itself is the
        newest ``<model-response>``'s markdown TEXT being non-empty and STABLE
        across several polls.

        :meth:`submit` calls :meth:`PageSession.enable_network` before clicking,
        so the events are captured. ``baseline_count`` (the model-response count
        before submit) gates the readiness probe so a previous reply is never
        mistaken for the new one during render lag.
        """
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if baseline_count < 0:
            raise ValueError("baseline_count must be >= 0")
        # Gemini's transport does NOT expose a clean answer-completion request:
        # Pro Extended fires a separate "thinking" StreamGenerate that
        # loadingFinishes ~10 s before the answer renders, and the send-button
        # fonticon does not reliably flip back from ``stop`` on completion. So we
        # use the network only as the "generation started" gate (first
        # StreamGenerate request appears), then treat the newest model-response's
        # markdown TEXT being STABLE (gated on count > baseline_count) for several
        # consecutive polls as completion — the one Gemini signal that proved
        # reliable. ``enable_network()`` ran in submit().
        _stab = {"prev": None, "n": 0}

        def _answer_settled() -> bool:
            # Completion = the newest answer's markdown TEXT non-empty and STABLE
            # across polls. Two shapes: the normal <model-response> (gated on
            # count > baseline_count so a previous reply isn't mistaken for the new
            # one), OR the A/B comparison panel (no model-response yet — read option
            # A). Comparing actual text (not length) so a same-length edit can't
            # read as stable.
            r = _eval_value(
                session,
                "(function(){"
                "var rs=document.querySelectorAll('model-response');"
                "if(rs.length>" + str(baseline_count) + "){"
                "  var m=rs[rs.length-1].querySelector('.markdown');"
                "  return {has:true, text:(m?(m.innerText||'').trim():'')};"
                "}"
                "var p=document.querySelector(" + _js_string_literal(AB_PANEL_SELECTOR) + ");"
                "if(p){"
                "  var es=p.querySelectorAll(" + _js_string_literal(AB_OPTION_MARKDOWN_SELECTOR) + ");"
                "  var m=es.length?es[0]:null;"
                "  return {has:true, ab:true, text:(m?(m.innerText||'').trim():'')};"
                "}"
                "return {has:false, text:''};"
                "})()",
            )
            if not isinstance(r, dict) or not r.get("has"):
                _stab["prev"] = None
                _stab["n"] = 0
                return False
            text = r.get("text") or ""
            if not text:
                _stab["prev"] = None
                _stab["n"] = 0
                return False
            if text == _stab["prev"]:
                _stab["n"] += 1
            else:
                _stab["prev"] = text
                _stab["n"] = 1
            # render poll is ~0.3 s; require ~6 s of unchanged text to outlast
            # the mid-stream pauses Gemini Pro takes on long structured outputs.
            return _stab["n"] >= 20

        try:
            session.wait_for_stream_completion(
                # The chat RPC is ``.../assistant.lamda.../StreamGenerate``;
                # match on the StreamGenerate RPC (other assistant.lamda RPCs are
                # not the generation stream).
                lambda p: (
                    p.get("request", {}).get("method") == "POST"
                    and "StreamGenerate" in (p.get("request", {}).get("url", "") or "")
                ),
                ready_check=_answer_settled,
                render_timeout=float(timeout_seconds),
                overall_timeout=float(timeout_seconds),
                start_timeout=60.0,
            )
        except CdpError as exc:
            self._screenshot_and_raise(
                session, "wait_for_streaming_done",
                StreamingTimeoutError(
                    f"Gemini chat stream did not complete within {timeout_seconds}s "
                    f"(network signal): {exc}"
                ),
            )

    # ----- extraction ----------------------------------------------------

    def extract_last_assistant_message(
        self,
        session: PageSession,
        *,
        baseline_count: int,
    ) -> str:
        """Return the Markdown text of the newest assistant reply.

        ``baseline_count`` is the value returned by
        :meth:`baseline_response_count` before submit; we require the
        current ``<model-response>`` count to be strictly greater so we
        don't accidentally re-extract an earlier reply.
        """
        if baseline_count < 0:
            raise ValueError("baseline_count must be >= 0")

        expr = (
            "(function(){"
            f"  var resp=document.querySelectorAll({_js_string_literal(MODEL_RESPONSE_SELECTOR)});"
            "  if(resp.length){"
            "    var last=resp[resp.length-1];"
            f"    var md=last.querySelector({_js_string_literal(MARKDOWN_SELECTOR)});"
            "    if(!md) return {ok:false, reason:'no_markdown', count:resp.length};"
            "    return {ok:true, count:resp.length, text: md.innerText || ''};"
            "  }"
            # A/B comparison fallback: read option A, then click its select-button
            # to pick it — collapses the panel to a normal response and unblocks the
            # chat so a follow-up turn can proceed.
            f"  var p=document.querySelector({_js_string_literal(AB_PANEL_SELECTOR)});"
            "  if(p){"
            f"    var es=p.querySelectorAll({_js_string_literal(AB_OPTION_MARKDOWN_SELECTOR)});"
            "    var md=es.length?es[0]:null;"
            "    var text=md?(md.innerText||''):'';"
            f"    var btn=p.querySelector({_js_string_literal(AB_SELECT_BUTTON_SELECTOR)});"
            "    if(btn) btn.click();"
            "    return {ok:!!text.trim(), count:" + str(baseline_count + 1) + ", text:text, ab:true, resolved:!!btn};"
            "  }"
            "  return {ok:false, reason:'no_model_response', count:0};"
            "})()"
        )
        try:
            res = _eval_value(session, expr)
        except CdpError as exc:
            self._screenshot_and_raise(
                session, "extract_last_assistant_message",
                ExtractionFailedError(f"extract evaluate failed: {exc}"),
            )
            return ""
        if not isinstance(res, dict) or not res.get("ok"):
            self._screenshot_and_raise(
                session, "extract_last_assistant_message",
                ExtractionFailedError(
                    f"extract failed: {res!r} (baseline={baseline_count})"
                ),
            )
            return ""
        count = res.get("count")
        text = res.get("text")
        if not isinstance(count, int) or count <= baseline_count:
            self._screenshot_and_raise(
                session, "extract_last_assistant_message",
                ExtractionFailedError(
                    f"no new assistant reply since baseline "
                    f"(count={count}, baseline={baseline_count})"
                ),
            )
            return ""
        if not isinstance(text, str) or not text.strip():
            self._screenshot_and_raise(
                session, "extract_last_assistant_message",
                ExtractionFailedError(
                    f"new reply has empty markdown (count={count})"
                ),
            )
            return ""
        return text

    # ----- cleanup -------------------------------------------------------

    def cleanup_input(self, session: PageSession) -> None:
        """Reset the Gemini input box back to empty.

        We use focus + select-all + delete; we deliberately do **not**
        fall back to ``el.innerHTML='<p><br></p>'`` because Gemini ships
        a strict ``Trusted Types`` policy that rejects raw HTML strings
        (PR9.C diagnostic: "This document requires 'TrustedHTML'"). If
        select-all + delete leaves residue, we issue a second pass
        rather than touch ``innerHTML``.

        We also delete the PR9 MutationObserver namespacing keys in case
        future code paths attach one.
        """
        expr = (
            "(function(){"
            f"  var el=document.querySelector({_js_string_literal(INPUT_SELECTOR)});"
            "  if(!el) return {ok:false, reason:'input_not_found'};"
            "  el.focus();"
            "  document.execCommand('selectAll', false, null);"
            "  document.execCommand('delete', false, null);"
            "  if((el.innerText||'').trim().length > 0) {"
            "    document.execCommand('selectAll', false, null);"
            "    document.execCommand('delete', false, null);"
            "  }"
            f"  try {{ delete window[{_js_string_literal(WINDOW_OBSERVER_KEY)}]; }} catch(e) {{}}"
            f"  try {{ delete window[{_js_string_literal(WINDOW_STATE_KEY)}]; }} catch(e) {{}}"
            "  return {ok:true, final_len:(el.innerText||'').length};"
            "})()"
        )
        # Cleanup is best-effort — we don't raise if it fails because the
        # caller is usually already past the critical section.
        try:
            _eval_value(session, expr)
        except CdpError:
            pass

    def _wait_for_input_ready(self, session: PageSession, timeout: float = 25.0) -> None:
        """Poll until the Quill composer (``INPUT_SELECTOR``) exists.

        Gemini's SPA hydrates asynchronously after a navigation; a fixed
        hydration sleep was intermittently too short, surfacing downstream as
        "cleanup verify could not find input". We poll here so callers don't
        have to time the hydration themselves. On timeout we return quietly and
        let the cleanup step raise its descriptive error (covers genuine drift).
        """
        expr = (
            "(function(){return !!document.querySelector("
            f"{_js_string_literal(INPUT_SELECTOR)});}})()"
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if _eval_value(session, expr, timeout=8.0) is True:
                    return
            except CdpError:
                pass
            time.sleep(1.0)

    def _cleanup_input_with_verify(self, session: PageSession) -> None:
        """Strict variant of cleanup_input used by ``inject_prompt``.

        Issues a batch of selectAll+delete pairs in one evaluate, then
        re-reads ``innerText.length`` in a second evaluate to verify
        cleanup actually took effect. Repeats up to
        ``_INJECT_CLEANUP_MAX_ATTEMPTS`` times because Quill sometimes
        leaves Delta queue artifacts after a single sweep.

        Why a separate evaluate (and not just the public
        ``cleanup_input``)?
          * Public ``cleanup_input`` is best-effort and silent — it
            never raises. The injection path needs the opposite: hard
            failure when we cannot empty the box, so we don't append
            to user content.
          * Verification must happen in a *fresh* evaluate, otherwise
            the same async context that ran selectAll+delete is racing
            its own readback (the chunk-1 abort observed in PR9.C
            single-evaluate scheme).
        """
        for attempt in range(_INJECT_CLEANUP_MAX_ATTEMPTS):
            sweep_expr = (
                "(function(){"
                f"  var el=document.querySelector({_js_string_literal(INPUT_SELECTOR)});"
                "  if(!el) return {ok:false, reason:'input_not_found'};"
                "  el.focus();"
                "  for(var i=0;i<5;i++){"
                "    document.execCommand('selectAll', false, null);"
                "    document.execCommand('delete', false, null);"
                "  }"
                "  return {ok:true, residual:(el.innerText||'').length};"
                "})()"
            )
            try:
                _eval_value(session, sweep_expr, timeout=_INJECT_CLEANUP_TIMEOUT_S)
            except CdpError as exc:
                self._screenshot_and_raise(
                    session, "inject_prompt",
                    InjectionFailedError(
                        f"cleanup sweep #{attempt} evaluate failed: {exc}"
                    ),
                )
                return
            # Tiny breather so Quill's Delta queue settles before we read.
            time.sleep(_INJECT_INTER_CHUNK_DRAIN_S)
            verify_expr = (
                "(function(){"
                f"  var el=document.querySelector({_js_string_literal(INPUT_SELECTOR)});"
                "  return el ? (el.innerText||'').length : -1;"
                "})()"
            )
            try:
                residual = _eval_value(
                    session, verify_expr, timeout=_INJECT_CLEANUP_TIMEOUT_S
                )
            except CdpError as exc:
                self._screenshot_and_raise(
                    session, "inject_prompt",
                    InjectionFailedError(
                        f"cleanup verify #{attempt} evaluate failed: {exc}"
                    ),
                )
                return
            if not isinstance(residual, int):
                self._screenshot_and_raise(
                    session, "inject_prompt",
                    InjectionFailedError(
                        f"cleanup verify returned non-int: {residual!r}"
                    ),
                )
                return
            if residual == -1:
                self._screenshot_and_raise(
                    session, "inject_prompt",
                    InjectionFailedError("cleanup verify could not find input"),
                )
                return
            if residual <= _INJECT_CLEANUP_MAX_RESIDUAL:
                return  # success
        # Exhausted attempts — refuse to inject on top of dirty state.
        self._screenshot_and_raise(
            session, "inject_prompt",
            InjectionFailedError(
                f"cleanup did not converge after "
                f"{_INJECT_CLEANUP_MAX_ATTEMPTS} attempts; residual={residual}"
            ),
        )

    # ----- internal ------------------------------------------------------

    def _screenshot_and_raise(
        self,
        session: PageSession,
        step_label: str,
        exc: GeminiDriverError,
    ) -> None:
        """Save a screenshot if possible, then raise ``exc``."""
        path = _save_screenshot(session, step_label)
        if path:
            # Append the screenshot path to the exception args so callers
            # logging ``str(exc)`` see it without extra plumbing.
            exc.args = (f"{exc.args[0] if exc.args else ''} | screenshot={path}",)
        raise exc

# ---------------------------------------------------------------------------
# Re-export for external typing convenience.
# ---------------------------------------------------------------------------

__all__ = [
    "GEMINI_URL_FRAGMENT",
    "INPUT_SELECTOR",
    "SEND_BUTTON_SELECTOR",
    "MODEL_RESPONSE_SELECTOR",
    "MARKDOWN_SELECTOR",
    "MODEL_SWITCH_BUTTON_SELECTOR",
    "PRO_MODEL_TOKENS",
    "WINDOW_OBSERVER_KEY",
    "WINDOW_STATE_KEY",
    "SCREENSHOT_DIR",
    "GeminiDriverError",
    "GeminiTabNotFoundError",
    "WrongModelError",
    "DomDriftError",
    "InjectionFailedError",
    "SubmitFailedError",
    "StreamingTimeoutError",
    "ExtractionFailedError",
    "GeminiWebDriver",
]
