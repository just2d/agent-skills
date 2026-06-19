"""Claude AI WebDriver for the PR9 orchestrator.

This module provides a CDP-based driver for interacting with Claude AI
(https://claude.ai) in the user's local browser. It aligns with the public
interface of ``GeminiWebDriver`` but uses internal CDP methods to bypass
the chrome-cdp invisible principle restrictions where necessary.

Key design decisions:
- Uses ``PageSession._send_method`` and ``_await_response`` directly to
  call CDP methods like ``Page.navigate`` and ``Input.insertText`` that
  are intentionally not exposed by ``PageSession``'s public API.
- Submit uses JS ``KeyboardEvent`` dispatch (not CDP
  ``Input.dispatchKeyEvent``) because CDP key events silently fail after
  ``Page.navigate``.
- Does NOT modify ``chrome_cdp_client.py`` or violate the invisible
  principle for general usage; this driver is a specialized exception
  for Claude AI automation.
- All operations target an existing Claude tab; no new tabs are created,
  no focus is stolen, and no tabs are closed automatically.
"""

from __future__ import annotations

import time
from typing import Optional

from lib.invisible_chrome_cdp_client import (
    CdpError,
    CdpEvalError,
    ChromeCDPClient,
    PageInfo,
    PageSession,
)
from lib.invisible_driver_utils import (
    extract_value as _extract_value,
    split_into_safe_chunks as _split_into_safe_chunks,
)


# ---------------------------------------------------------------------------
# Error classes
# ---------------------------------------------------------------------------

class ClaudeDriverError(RuntimeError):
    """Base error for Claude driver operations."""


class ClaudeTabNotFoundError(ClaudeDriverError):
    """Raised when no Claude AI tab is found in the browser."""


class ClaudeInjectionFailedError(ClaudeDriverError):
    """Raised when prompt injection into the input box fails."""


class ClaudeSubmitFailedError(ClaudeDriverError):
    """Raised when submitting the prompt fails."""


class ClaudeStreamingTimeoutError(ClaudeDriverError):
    """Raised when waiting for streaming completion times out."""


class ClaudeExtractionFailedError(ClaudeDriverError):
    """Raised when extracting the assistant message fails."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLAUDE_URL_FRAGMENT = "claude.ai"
_CLAUDE_NEW_CHAT_URL = "https://claude.ai/new"

# CSS selectors for Claude AI DOM elements
_INPUT_BOX_SELECTOR = '[role=textbox][contenteditable=true]'
_NON_STREAMING_INDICATOR_SELECTOR = '[data-is-streaming="false"]'
_MODEL_BUTTON_SELECTOR = 'button[aria-label^="Model:"]'

_NAVIGATE_HYDRATION_WAIT_SECONDS = 2.5
_INJECT_CHUNK_SIZE = 4000
_INJECT_CHUNK_DELAY_SECONDS = 0.05
# How long to wait for React to mount the Send button after injection
# before giving up (it appears a tick after the editor's beforeinput).
_SEND_BUTTON_WAIT_SECONDS = 6.0


# ---------------------------------------------------------------------------
# ClaudeWebDriver
# ---------------------------------------------------------------------------

class ClaudeWebDriver:
    """CDP-based driver for Claude AI.

    Aligns with the public interface of ``GeminiWebDriver`` but uses
    internal CDP methods for navigation, text injection, and key events.
    """

    def __init__(self, cdp_client: ChromeCDPClient) -> None:
        """Initialize the Claude WebDriver.

        Args:
            cdp_client: An initialized ChromeCDPClient connected to the
                local browser's debugging port.
        """
        self._cdp_client = cdp_client

    # ----- public interface (aligned with GeminiWebDriver) ------------

    def find_claude_tab(self) -> PageSession:
        """Find and attach to an existing Claude AI tab.

        Searches all pages for one whose URL contains 'claude.ai'.
        Raises ``ClaudeTabNotFoundError`` if none is found.

        Returns:
            A ``PageSession`` attached to the Claude tab.
        """
        pages = self._cdp_client.list_pages()
        claude_page: Optional[PageInfo] = None
        for page in pages:
            if _CLAUDE_URL_FRAGMENT in page.url:
                claude_page = page
                break

        if claude_page is None:
            raise ClaudeTabNotFoundError(
                "No Claude AI tab found. Please open https://claude.ai "
                "in your browser and ensure it is logged in."
            )

        try:
            session = self._cdp_client.attach_to_page(claude_page.id)
        except CdpError as exc:
            raise ClaudeTabNotFoundError(
                f"Failed to attach to Claude tab: {exc}"
            ) from exc

        return session

    def assert_target_model(self, session: PageSession) -> str:
        """Assert that the current model is the expected one.

        Reads the model button's aria-label to confirm the model.
        Does NOT switch models; only verifies.

        Args:
            session: An active PageSession on a Claude tab.

        Returns:
            The model name string (e.g., 'Opus 4.7 Adaptive').

        Raises:
            ClaudeDriverError: If the model button cannot be read.
        """
        try:
            result = session.runtime_evaluate(
                f"""
                (() => {{
                    const btn = document.querySelector('{_MODEL_BUTTON_SELECTOR}');
                    if (!btn) return null;
                    return btn.getAttribute('aria-label') || null;
                }})()
                """,
                timeout=10.0,
            )
            model_label = _extract_value(result)
            if not model_label or not isinstance(model_label, str):
                raise ClaudeDriverError(
                    "Could not read Claude model button. "
                    "Ensure you are on a valid Claude chat page."
                )
            # Extract model name from aria-label like "Model: Opus 4.7 Adaptive"
            if "Model:" in model_label:
                model_name = model_label.split("Model:", 1)[1].strip()
            else:
                model_name = model_label.strip()
            return model_name
        except CdpEvalError as exc:
            raise ClaudeDriverError(
                f"Failed to assert Claude model: {exc}"
            ) from exc

    def navigate_new_chat(self, session: PageSession) -> None:
        """Navigate to a new chat page.

        Uses ``Page.navigate`` CDP method to redirect to /new, then waits
        for the page to load and hydrate.

        Args:
            session: An active PageSession on a Claude tab.

        Raises:
            ClaudeDriverError: If navigation fails.
        """
        try:
            # Send Page.navigate via internal method
            rid = session._send_method(
                "Page.navigate", {"url": _CLAUDE_NEW_CHAT_URL}
            )
            nav_result = session._await_response(rid, timeout=15.0)
            if "error" in nav_result:
                raise ClaudeDriverError(
                    f"Page.navigate failed: {nav_result['error']}"
                )

            # Wait for document.readyState === 'complete'
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                try:
                    ready_result = session.runtime_evaluate(
                        "document.readyState", timeout=5.0
                    )
                    if _extract_value(ready_result) == "complete":
                        break
                except CdpEvalError:
                    pass
                time.sleep(0.5)
            else:
                raise ClaudeDriverError(
                    "Timed out waiting for page load after navigation"
                )

            # Additional wait for SPA hydration
            time.sleep(_NAVIGATE_HYDRATION_WAIT_SECONDS)

        except CdpError as exc:
            raise ClaudeDriverError(
                f"Navigation to new chat failed: {exc}"
            ) from exc

    def baseline_response_count(self, session: PageSession) -> int:
        """Count completed (``data-is-streaming="false"``) messages BEFORE submit.

        Capture this before :meth:`submit` and pass it to
        :meth:`wait_for_streaming_done` as ``baseline_count``. Otherwise the wait
        samples its baseline at entry — and if the reply already completed before
        the wait starts (e.g. this provider finished while another was being
        extracted in a serial flow), the new answer is already in the baseline,
        ``count > baseline`` never holds, and the wait times out.
        """
        n = _extract_value(
            session.runtime_evaluate(
                "document.querySelectorAll('"
                + _NON_STREAMING_INDICATOR_SELECTOR
                + "').length",
                timeout=5.0,
            )
        )
        return int(n) if isinstance(n, int) and n >= 0 else 0

    def inject_prompt(self, session: PageSession, prompt: str) -> None:
        """Inject a prompt into the Claude input box.

        Steps:
        1. Focus the input box via Runtime.evaluate.
        2. Inject the prompt in chunks via in-page
           ``document.execCommand('insertText')`` (see the regression note in
           the body for why this replaced CDP ``Input.insertText``).
        3. Verify injection by reading back textContent.length.

        Args:
            session: An active PageSession on a Claude tab.
            prompt: The full prompt text to inject.

        Raises:
            ClaudeInjectionFailedError: If injection or verification fails.
        """
        try:
            # Step 1: Focus the input box
            session.runtime_evaluate(
                f"""
                (() => {{
                    const input = document.querySelector('{_INPUT_BOX_SELECTOR}');
                    if (!input) throw new Error('Input box not found');
                    input.focus();
                    return true;
                }})()
                """,
                timeout=10.0,
            )

            # Step 2: Inject text via in-page execCommand('insertText'),
            # chunked to avoid blocking the renderer on multi-KB prompts.
            #
            # NOTE (2026-06-17 regression fix): CDP ``Input.insertText`` still
            # populates the contenteditable DOM, but in the current Claude build
            # it no longer drives ProseMirror's ``beforeinput`` pipeline — so
            # React's "composer has content" state never flips and the Send
            # button is never mounted (submit then fails with NOT_FOUND).
            # ``document.execCommand('insertText')`` fires the
            # ``beforeinput`` (inputType=insertText) that ProseMirror/React
            # listen for, so the Send button mounts as expected.
            import json as _json
            chunks = _split_into_safe_chunks(prompt, _INJECT_CHUNK_SIZE)
            for idx, chunk in enumerate(chunks):
                ins_result = session.runtime_evaluate(
                    f"""
                    (() => {{
                        const input = document.querySelector('{_INPUT_BOX_SELECTOR}');
                        if (!input) return JSON.stringify({{ok: false, error: 'NO_INPUT'}});
                        input.focus();
                        const ok = document.execCommand('insertText', false, {_json.dumps(chunk)});
                        return JSON.stringify({{ok: !!ok}});
                    }})()
                    """,
                    timeout=15.0,
                )
                ins_raw = _extract_value(ins_result) or "{}"
                ins_parsed = (
                    _json.loads(ins_raw) if isinstance(ins_raw, str) else ins_raw
                )
                if not ins_parsed.get("ok"):
                    raise ClaudeInjectionFailedError(
                        f"execCommand insertText chunk {idx + 1}/{len(chunks)} "
                        f"failed: {ins_parsed.get('error')}"
                    )
                if idx < len(chunks) - 1:
                    time.sleep(_INJECT_CHUNK_DELAY_SECONDS)

            # Step 3: Verify injection by reading back length
            verify_result = session.runtime_evaluate(
                f"""
                (() => {{
                    const input = document.querySelector('{_INPUT_BOX_SELECTOR}');
                    if (!input) return -1;
                    return input.textContent ? input.textContent.length : -1;
                }})()
                """,
                timeout=10.0,
            )
            injected_length = _extract_value(verify_result)
            if injected_length is None:
                injected_length = -1
            expected_length = len(prompt)
            if injected_length < expected_length * 0.9:
                # Allow 10% tolerance for potential encoding differences
                raise ClaudeInjectionFailedError(
                    f"Prompt injection verification failed: "
                    f"expected ~{expected_length} chars, got {injected_length}"
                )

        except CdpEvalError as exc:
            raise ClaudeInjectionFailedError(
                f"Failed to inject prompt: {exc}"
            ) from exc
        except CdpError as exc:
            raise ClaudeInjectionFailedError(
                f"CDP error during prompt injection: {exc}"
            ) from exc

    def submit(self, session: PageSession) -> None:
        """Submit the prompt by dispatching an Enter KeyboardEvent via JS.

        Claude's frontend wraps its ProseMirror/tiptap editor with a React
        ``onKeyDownCapture`` handler that intercepts bare Enter (no Shift,
        Alt, or IME composition) and triggers the internal submit callback.

        **Why JS dispatchEvent instead of CDP Input.dispatchKeyEvent?**

        After ``Page.navigate`` (used by ``navigate_new_chat``), CDP's
        ``Input.dispatchKeyEvent`` silently fails to reach the React
        handler — the CDP command returns success but the keydown event
        never fires in the page context.  A JS-level ``new KeyboardEvent``
        dispatched via ``element.dispatchEvent()`` works reliably in both
        navigated and long-lived tabs, and in background tabs.

        The JS-created event has ``isTrusted=false``, but Claude's React
        ``onKeyDownCapture`` handler does not check ``isTrusted`` — it only
        inspects ``key``, ``shiftKey``, ``altKey``, and ``isComposing``.

        Before dispatching, we verify the Send button is present and
        enabled (indicating the editor has content to send).

        Args:
            session: An active PageSession on a Claude tab.

        Raises:
            ClaudeSubmitFailedError: If submitting fails.
        """
        _SEND_BUTTON_SELECTOR = 'button[aria-label*="Send"]'
        try:
            # Un-throttle background-tab rendering (no window raise) FIRST so the
            # reply actually paints into the DOM for extraction; before
            # enable_network so visibility-resume traffic stays out of the capture.
            session.set_focus_emulation(True)
            # Then enable Network so wait_for_streaming_done can observe the
            # chat-stream request + loadingFinished (not replayed; clears stale).
            session.enable_network()
            # Step 1: Wait for the Send button to be present and enabled.
            # React mounts the Send button a tick after the editor's
            # ``beforeinput`` fires, so a single immediate check races the
            # mount (NOT_FOUND even though injection succeeded). Poll until it
            # is OK, surfacing the last NOT_FOUND/DISABLED state on timeout.
            import json as _json
            btn_status = {}
            _btn_deadline = time.monotonic() + _SEND_BUTTON_WAIT_SECONDS
            while True:
                btn_result = session.runtime_evaluate(
                    f"""
                    (() => {{
                        const btn = document.querySelector('{_SEND_BUTTON_SELECTOR}');
                        if (!btn) return JSON.stringify({{status: 'NOT_FOUND'}});
                        if (btn.disabled) return JSON.stringify({{status: 'DISABLED'}});
                        return JSON.stringify({{status: 'OK'}});
                    }})()
                    """,
                    timeout=10.0,
                )
                btn_raw = _extract_value(btn_result) or "{}"
                btn_status = _json.loads(btn_raw) if isinstance(btn_raw, str) else btn_raw
                if btn_status.get("status") == "OK":
                    break
                if time.monotonic() >= _btn_deadline:
                    break
                time.sleep(0.3)

            if btn_status.get("status") == "NOT_FOUND":
                raise ClaudeSubmitFailedError(
                    "Send button not found on Claude page."
                )
            if btn_status.get("status") == "DISABLED":
                raise ClaudeSubmitFailedError(
                    "Send button is disabled — prompt may be empty or "
                    "Claude is rate-limiting."
                )

            # Step 2: Focus input and dispatch Enter via JS KeyboardEvent.
            # After large text injection (e.g. 80 K chars), ProseMirror
            # needs time to finish DOM updates before focus is stable.
            submit_result = session.runtime_evaluate(
                f"""
                (() => {{
                    const input = document.querySelector('{_INPUT_BOX_SELECTOR}');
                    if (!input) return JSON.stringify({{ok: false, error: 'NO_INPUT'}});
                    input.focus();
                    const event = new KeyboardEvent('keydown', {{
                        key: 'Enter',
                        code: 'Enter',
                        keyCode: 13,
                        which: 13,
                        bubbles: true,
                        cancelable: true,
                    }});
                    input.dispatchEvent(event);
                    return JSON.stringify({{ok: true}});
                }})()
                """,
                timeout=10.0,
            )
            submit_raw = _extract_value(submit_result) or "{}"
            submit_parsed = (
                _json.loads(submit_raw)
                if isinstance(submit_raw, str)
                else submit_raw
            )
            if not submit_parsed.get("ok"):
                raise ClaudeSubmitFailedError(
                    f"JS Enter dispatch failed: {submit_parsed.get('error')}"
                )

        except CdpEvalError as exc:
            raise ClaudeSubmitFailedError(
                f"Failed to submit prompt: {exc}"
            ) from exc
        except CdpError as exc:
            raise ClaudeSubmitFailedError(
                f"CDP error during submit: {exc}"
            ) from exc

    def wait_for_streaming_done(
        self,
        session: PageSession,
        *,
        timeout_seconds: int = 300,
        baseline_count: Optional[int] = None,
    ) -> None:
        """Wait until Claude finishes streaming its response.

        Network-event completion: block until Claude's chat-stream POST
        (``/api/organizations/.../completion``, ``text/event-stream``) finishes,
        instead of polling the ``data-is-streaming`` attribute / Stop button.
        The SSE stream stays open for the whole generation, so this is robust
        where DOM proxies are fragile. ``enable_network()`` was called in
        submit() before dispatch.

        ``baseline_count`` is the number of completed (``data-is-streaming=
        "false"``) messages before submit; the readiness probe requires a NEW
        completed message (count > baseline) so it can't read the previous reply
        during render lag. When omitted it is sampled at entry — safe here
        because Claude streams the new reply as ``data-is-streaming="true"`` and
        only flips it to ``"false"`` on completion.

        Args:
            session: An active PageSession on a Claude tab.
            timeout_seconds: Maximum seconds to wait (default: 300).
            baseline_count: Completed-message count captured before submit.

        Raises:
            ClaudeStreamingTimeoutError: If timeout is exceeded.
        """
        base = baseline_count
        if base is None:
            base = _extract_value(
                session.runtime_evaluate(
                    "document.querySelectorAll('"
                    + _NON_STREAMING_INDICATOR_SELECTOR
                    + "').length",
                    timeout=5.0,
                )
            ) or 0

        def _claude_answer_ready() -> bool:
            r = _extract_value(
                session.runtime_evaluate(
                    "(()=>{const e=document.querySelectorAll('"
                    + _NON_STREAMING_INDICATOR_SELECTOR
                    + "');const l=e[e.length-1];"
                    "return {count:e.length, hasText: !!l && "
                    "(l.innerText||'').trim().length>0};})()",
                    timeout=5.0,
                )
            )
            return (
                isinstance(r, dict)
                and int(r.get("count", 0)) > base
                and bool(r.get("hasText"))
            )

        try:
            session.wait_for_stream_completion(
                lambda p: (
                    p.get("request", {}).get("method") == "POST"
                    and "/completion" in (p.get("request", {}).get("url", "") or "")
                ),
                ready_check=_claude_answer_ready,
                overall_timeout=float(timeout_seconds),
                start_timeout=60.0,
                # Poll for the rendered answer for the whole budget (not the
                # default 10s) so a long-thinking Opus answer isn't abandoned
                # just after its stream's loadingFinished.
                render_timeout=float(timeout_seconds),
            )
        except CdpError as exc:
            raise ClaudeStreamingTimeoutError(
                f"Claude chat stream did not complete within {timeout_seconds}s "
                f"(network signal): {exc}"
            ) from exc

    def extract_last_assistant_message(self, session: PageSession) -> str:
        """Extract the last completed assistant message as Markdown.

        Steps:
        1. Take the last ``[data-is-streaming="false"]`` element (the finished reply).
        2. Walk it HTML->Markdown (headers, tables, lists, bold, fenced code WITH
           language), SKIPPING UI chrome: ``sr-only`` a11y duplicates, ``button``
           (copy), ``aria-hidden`` decorations, and a code block's header (only the
           fenced ``<pre>`` survives). This keeps Claude's screen-reader-duplicate
           summary line and code-block language label out of the result.
        3. Return the trimmed Markdown.

        Note: NO "Claude responded:" prefix stripping happens here (the old
        docstring claimed it; the code never did). Callers needing that do it
        themselves; for a pure-JSON answer prefer reading ``<pre><code>``
        textContent directly.

        Args:
            session: An active PageSession on a Claude tab.

        Returns:
            The cleaned assistant message as Markdown text.

        Raises:
            ClaudeExtractionFailedError: If extraction fails.
        """
        try:
            # Build JS code using string concatenation to avoid f-string
            # escaping issues with JS arrow functions, backslashes, etc.
            js_code = (
                "(()=>{"
                "const elements=document.querySelectorAll('" + _NON_STREAMING_INDICATOR_SELECTOR + "');"
                "let container;"
                "if(elements.length>0){container=elements[elements.length-1];}"
                "else{container=document.querySelector('.message-assistant');}"
                "if(!container)return '';"
                "function htmlToMd(node){"
                "if(node.nodeType===Node.TEXT_NODE)return node.textContent;"
                "if(node.nodeType!==Node.ELEMENT_NODE)return '';"
                "var tag=node.tagName.toLowerCase();"
                # Skip UI chrome so it is not scraped into the answer (OQ-3 root cause):
                # sr-only a11y duplicates, copy buttons, aria-hidden decorations; and for a
                # code block emit ONLY the fenced <pre> (drops the copy-button + language-
                # label header that otherwise leaked a stray 'json' before the fence).
                "if(node.classList&&node.classList.contains('sr-only'))return '';"
                "if(tag==='button')return '';"
                "if(node.getAttribute&&node.getAttribute('aria-hidden')==='true')return '';"
                "if(tag!=='pre'){var cbPre=node.querySelector&&node.querySelector(':scope>pre,:scope>div>pre');if(cbPre&&cbPre.querySelector('code'))return htmlToMd(cbPre);}"
                "var childMd=Array.from(node.childNodes).map(htmlToMd).join('');"
                "if(tag==='h1')return '# '+childMd.trim()+'\\n\\n';"
                "if(tag==='h2')return '## '+childMd.trim()+'\\n\\n';"
                "if(tag==='h3')return '### '+childMd.trim()+'\\n\\n';"
                "if(tag==='h4')return '#### '+childMd.trim()+'\\n\\n';"
                "if(tag==='strong'||tag==='b')return '**'+childMd+'**';"
                "if(tag==='em'||tag==='i')return '*'+childMd+'*';"
                "if(tag==='code'&&node.parentElement&&node.parentElement.tagName!=='PRE')return '`'+childMd+'`';"
                "if(tag==='pre'){var codeEl=node.querySelector('code');"
                "var text=codeEl?codeEl.textContent:node.textContent;"
                "var lang='';if(codeEl){var lm=(codeEl.className||'').match(/language-([\\w-]+)/);if(lm)lang=lm[1];}"
                "return '```'+lang+'\\n'+text+'\\n```\\n\\n';}"
                "if(tag==='p')return childMd.trim()+'\\n\\n';"
                "if(tag==='br')return '\\n';"
                "if(tag==='ul'||tag==='ol')return childMd+'\\n';"
                "if(tag==='li'){"
                "var parent=node.parentElement;"
                "if(parent&&parent.tagName==='OL'){"
                "var idx=Array.from(parent.children).indexOf(node)+1;"
                "return idx+'. '+childMd.trim()+'\\n';}"
                "return '- '+childMd.trim()+'\\n';}"
                "if(tag==='table'){"
                "var md='';"
                "var rows=node.querySelectorAll('tr');"
                "for(var ri=0;ri<rows.length;ri++){"
                "var cells=rows[ri].querySelectorAll('th,td');"
                "var cellTexts=Array.from(cells).map(function(c){return c.textContent.trim();});"
                "md+='| '+cellTexts.join(' | ')+' |\\n';"
                "if(ri===0){md+='|'+cellTexts.map(function(){return '---';}).join('|')+'|\\n';}}"
                "return md+'\\n';}"
                "if(['thead','tbody','tfoot','tr','th','td'].indexOf(tag)>=0)return childMd;"
                "if(tag==='blockquote'){"
                "return childMd.split('\\n').map(function(l){return '> '+l;}).join('\\n')+'\\n\\n';}"
                "if(tag==='hr')return '---\\n\\n';"
                "return childMd;}"
                "return htmlToMd(container);})()"
            )
            raw_text = session.runtime_evaluate(js_code, timeout=15.0)
            message = _extract_value(raw_text) or ""
            if not message or not isinstance(message, str):
                raise ClaudeExtractionFailedError(
                    "No assistant message found or message is empty"
                )
            return message.strip()

        except CdpEvalError as exc:
            raise ClaudeExtractionFailedError(
                f"Failed to extract assistant message: {exc}"
            ) from exc
