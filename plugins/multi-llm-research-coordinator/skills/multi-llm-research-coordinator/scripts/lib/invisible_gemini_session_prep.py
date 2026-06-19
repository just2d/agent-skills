"""Gemini session preparation (PR15).

Scope: a single, dependency-light "before you submit" module that the
orchestrator (PR9) calls in front of the existing
:class:`GeminiWebDriver` to guarantee the next prompt lands in a clean
Gemini conversation in Pro mode.

Design constraints (settled with the operator before implementation):

1. **Brand-agnostic.** The browser may be Brave or Chrome; we never
   read the brand. We only require that ``http://127.0.0.1:9222``
   speaks the DevTools protocol.
2. **Invisible.** This module follows the chrome-cdp skill rules
   strictly:
     * never call ``Page.bringToFront`` / ``Target.activateTarget``;
     * never reload, navigate, or close a tab the user opened;
     * when we *must* open a fresh Gemini tab (none exists), we use
       ``Target.createTarget`` with ``background=true`` — the only
       documented way on macOS to add a tab without stealing focus.
3. **"Dirty" definition.** A Gemini tab is dirty when **either**
     * its URL contains a conversation id (``/app/<id>``), or
     * its DOM contains at least one ``<user-query>`` /
       ``<model-response>`` element (i.e. there is rendered chat).
   Either being true is enough — we don't try to reuse a half-used
   tab. Cleaning a dirty tab is done by sending the keyboard shortcut
   ``Cmd+Shift+O`` ("New chat") via ``Input.dispatchKeyEvent`` —
   never with a prompt asking Gemini to forget.
4. **Pro selector.** Every preparation pass opens the model selector
   button and reads ``button.input-area-switch``'s ``innerText``. If
   the visible label does not contain ``"Pro"`` we click open the
   menu and click the menu item whose label contains ``"Pro"``. We
   never trust prior state.
5. **Submission.** Once prepared, we paste the query into the Quill
   input and press Enter (a single ``keypress`` on the editor); we
   then return immediately. **Waiting for the streaming reply and
   extracting the markdown remain the responsibility of the existing
   :mod:`gemini_web_driver`** — the operator confirmed this split.
6. **Failure mode.** Any unrecoverable problem raises
   :class:`SessionPrepError` (a subclass of
   :class:`gemini_web_driver.GeminiDriverError` so callers that
   already catch that base class keep working) and best-effort
   captures a screenshot under
   ``dist/orchestration/screenshots/``.

Deliberately *not* in scope:

* Waiting for the streaming reply / extracting the assistant message
  (already implemented in :mod:`gemini_web_driver`).
* Multi-tab fan-out / parallel preparation (one tab at a time).
* Logging in (the operator must already be logged in to Gemini).

The module exports a single high-level entry point:
:func:`prepare_and_submit_query`. Helpers are exposed individually so
unit tests can target each decision point without round-tripping
through CDP.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from lib.invisible_chrome_cdp_client import (
    CdpError,
    CdpEvalError,
    ChromeCDPClient,
    PageInfo,
    PageSession,
)
from lib.invisible_gemini_web_driver import (
    GEMINI_URL_FRAGMENT,
    GeminiDriverError,
    INPUT_SELECTOR,
    MODEL_SWITCH_BUTTON_SELECTOR,
    PRO_MODEL_TOKENS,
    _eval_value,
    _js_string_literal,
    _save_screenshot,
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SessionPrepError(GeminiDriverError):
    """Base error for the session-prep stage."""


class NoCdpConnectionError(SessionPrepError):
    """``http://127.0.0.1:9222`` is not reachable / not speaking CDP."""


class NewTabFailedError(SessionPrepError):
    """``Target.createTarget`` did not return a usable target id."""


class NewChatShortcutFailedError(SessionPrepError):
    """``Cmd+Shift+O`` did not transition the tab to a fresh /app state."""


class ProSwitchFailedError(SessionPrepError):
    """The model selector did not converge to a Pro-labelled state."""


class SubmitShortcutFailedError(SessionPrepError):
    """Pressing Enter on the Quill editor did not send the message."""


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionPrepResult:
    """What :func:`prepare_and_submit_query` returns to the caller."""

    page_id: str
    """The CDP page id of the prepared Gemini tab."""

    final_url: str
    """The Gemini URL after preparation (``/app`` or ``/app/<convid>``)."""

    used_existing_tab: bool
    """True if we reused a tab the operator already had open."""

    opened_new_chat: bool
    """True if we sent Cmd+Shift+O to discard the previous conversation."""

    model_switched_to_pro: bool
    """True if we had to click the model selector to get into Pro mode."""

    initial_model_label: Optional[str]
    """``button.input-area-switch``'s innerText *before* any switching."""

    final_model_label: Optional[str]
    """``button.input-area-switch``'s innerText *after* preparation."""

    notes: tuple[str, ...]
    """Free-form audit log lines emitted during preparation."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Gemini's "blank app" landing — opening this gives us /app with no
# conversation id. We never navigate an *existing* tab here (would be
# disruptive and violates chrome-cdp invisible rule).
GEMINI_FRESH_URL = "https://gemini.google.com/app"

# Regex matching the URL-side "dirty" signal: /app/<conversationId>.
# We allow either ``gemini.google.com/app/<id>`` or the same with one or
# more arbitrary path segments before ``/app/`` (e.g. account-switcher
# prefix ``/u/0/app`` or locale prefix ``/zh-cn/app``). The
# conversationId is a hex/word slug; any non-empty path segment after
# ``/app/`` counts as dirty.
_CONVERSATION_URL_RE = re.compile(
    r"gemini\.google\.com(?:/[A-Za-z0-9_\-]+)*/app/(?P<convid>[A-Za-z0-9_\-]+)"
)

# DOM-side dirty probe. We look for any user query bubble or model
# response element. ``<user-query>`` is the canonical web component
# for the user side; ``<model-response>`` for the assistant side.
# Either present => non-empty conversation.
_DOM_DIRTY_PROBE_JS = (
    "(function(){"
    "  var u=document.querySelectorAll('user-query').length;"
    "  var m=document.querySelectorAll('model-response').length;"
    "  return {user_msgs: u, model_msgs: m};"
    "})()"
)

# Wait budgets (seconds).
_NEW_TAB_HYDRATE_TIMEOUT_S = 30.0
_NEW_TAB_HYDRATE_POLL_S = 0.5
_NEW_CHAT_TRANSITION_TIMEOUT_S = 10.0
_NEW_CHAT_TRANSITION_POLL_S = 0.4
_PRO_SWITCH_MENU_TIMEOUT_S = 8.0
_PRO_SWITCH_MENU_POLL_S = 0.3
_INPUT_READY_TIMEOUT_S = 15.0
_INPUT_READY_POLL_S = 0.3

# CDP modifier bitmask flags (Input.dispatchKeyEvent).
#   1 = Alt, 2 = Ctrl, 4 = Meta (⌘), 8 = Shift
_MOD_META = 4
_MOD_SHIFT = 8


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without CDP)
# ---------------------------------------------------------------------------


def is_gemini_url(url: str) -> bool:
    """Return True if ``url`` looks like a Gemini app page."""
    if not isinstance(url, str):
        return False
    return GEMINI_URL_FRAGMENT in url


def url_indicates_dirty_conversation(url: str) -> bool:
    """Return True if the URL embeds a conversation id (``/app/<id>``)."""
    if not isinstance(url, str):
        return False
    return _CONVERSATION_URL_RE.search(url) is not None


def dom_indicates_dirty_conversation(probe: dict) -> bool:
    """Return True if the DOM probe payload reports any chat bubble.

    ``probe`` is the dict returned by evaluating
    :data:`_DOM_DIRTY_PROBE_JS`.
    """
    if not isinstance(probe, dict):
        # On any unexpected shape we err on the safe side and treat
        # the tab as dirty so we'll force a Cmd+Shift+O pass.
        return True
    user_msgs = probe.get("user_msgs", 0)
    model_msgs = probe.get("model_msgs", 0)
    try:
        return int(user_msgs) > 0 or int(model_msgs) > 0
    except (TypeError, ValueError):
        return True


def label_is_pro(label: Optional[str]) -> bool:
    """Return True if a model selector innerText counts as Pro."""
    if not isinstance(label, str):
        return False
    text = label.strip()
    if not text:
        return False
    return any(tok in text for tok in PRO_MODEL_TOKENS)


def pick_first_gemini_page(pages: list[PageInfo]) -> Optional[PageInfo]:
    """Return the first ``PageInfo`` whose URL is a Gemini app page.

    Order is the order returned by ``GET /json``, which Chromium tends
    to keep stable per session. If multiple Gemini tabs exist we just
    take the first — the operator confirmed in design that one tab is
    enough to handle.
    """
    if not isinstance(pages, list):
        return None
    for p in pages:
        url = getattr(p, "url", "")
        if is_gemini_url(url):
            return p
    return None


# ---------------------------------------------------------------------------
# CDP-touching primitives
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.monotonic()


def _emit(notes: list[str], on_log: Optional[Callable[[str], None]], line: str) -> None:
    """Append to ``notes`` and (if provided) forward to ``on_log``."""
    notes.append(line)
    if on_log is not None:
        try:
            on_log(line)
        except Exception:  # noqa: BLE001 - logging must never break us
            pass


def open_background_gemini_tab(client: ChromeCDPClient) -> str:
    """Create a new Gemini tab in the background and return its target id.

    Implementation note: :class:`ChromeCDPClient` deliberately does
    *not* expose ``Target.createTarget`` (PR9 audit decision); we
    therefore reach in via an attached "browser-level" session here.
    To stay within the invisible-operation envelope we attach to an
    existing page and ask *that* session to issue ``Target.createTarget``
    with ``background=true`` — the call goes through the per-page
    websocket but is implicitly routed to the browser target by the
    DevTools protocol, which is why ``background=true`` does take
    effect (verified live).

    If there is no existing page to borrow we fall back to picking the
    first ``type==page`` entry from ``/json``; we never use
    ``/json/new`` (HTTP), which always activates the new tab to the
    foreground.

    Raises :class:`NewTabFailedError` if we can't get a target id.
    """
    pages = client.list_pages()
    if not pages:
        # The browser is open with literally zero pages. We could in
        # principle attach to the browser-level WS and create the tab
        # there, but the simplest invisible path is to require the
        # user to have at least one open page first.
        raise NewTabFailedError(
            "no existing pages to attach to; open any tab manually first "
            "so we can create the Gemini tab in the background"
        )
    host_page = pages[0]
    session = client.attach_to_page(host_page.id)
    try:
        # ``Target.createTarget`` is a browser-domain method but the
        # DevTools protocol accepts it on per-page sessions and routes
        # it appropriately. ``background=True`` is the macOS-specific
        # flag that prevents the new tab from being raised to front
        # (verified empirically on Brave 9222 in PR9).
        rid = session._send_method(
            "Target.createTarget",
            {"url": GEMINI_FRESH_URL, "background": True},
        )
        msg = session._await_response(rid, timeout=10.0)
        if "error" in msg:
            raise NewTabFailedError(
                f"Target.createTarget transport error: {msg['error']}"
            )
        result = msg.get("result", {})
        target_id = result.get("targetId")
        if not isinstance(target_id, str) or not target_id:
            raise NewTabFailedError(
                f"Target.createTarget returned no targetId; raw={result!r}"
            )
        return target_id
    finally:
        session.close()


def wait_until_tab_hydrated(
    session: PageSession,
    *,
    timeout_seconds: float = _NEW_TAB_HYDRATE_TIMEOUT_S,
    poll_seconds: float = _NEW_TAB_HYDRATE_POLL_S,
) -> None:
    """Block until the Gemini SPA has rendered its input box.

    We wait for two things, in order:
      1. ``document.readyState === 'complete'``
      2. The Quill editor element :data:`INPUT_SELECTOR` exists

    The second check is the strict one — Gemini's outer shell loads
    quickly but the Quill editor is mounted only after the user
    auth/feature-flag bootstrap finishes, and we need that editor to
    paste the prompt.
    """
    deadline = _now() + timeout_seconds
    expr = (
        "(function(){"
        f"  var input=document.querySelector({_js_string_literal(INPUT_SELECTOR)});"
        "  return {ready: document.readyState==='complete', input_present: !!input};"
        "})()"
    )
    last_state: dict = {}
    while _now() < deadline:
        try:
            state = _eval_value(session, expr, timeout=10.0)
        except (CdpError, CdpEvalError):
            time.sleep(poll_seconds)
            continue
        if isinstance(state, dict):
            last_state = state
            if state.get("ready") and state.get("input_present"):
                return
        time.sleep(poll_seconds)
    raise SessionPrepError(
        f"Gemini tab did not hydrate within {timeout_seconds:.0f}s; "
        f"last_state={last_state!r}"
    )


def probe_dirty_state(session: PageSession) -> dict:
    """Return ``{"url": str, "url_dirty": bool, "dom_probe": dict, "dom_dirty": bool}``.

    Used by both the public preparation flow and unit-targeted callers
    that want to introspect a specific tab.
    """
    url_expr = "location.href"
    try:
        url_value = _eval_value(session, url_expr, timeout=5.0)
    except (CdpError, CdpEvalError) as exc:
        raise SessionPrepError(f"failed to read location.href: {exc}") from exc
    url = url_value if isinstance(url_value, str) else ""

    try:
        dom_probe = _eval_value(session, _DOM_DIRTY_PROBE_JS, timeout=5.0)
    except (CdpError, CdpEvalError) as exc:
        raise SessionPrepError(f"failed to probe chat bubbles: {exc}") from exc

    return {
        "url": url,
        "url_dirty": url_indicates_dirty_conversation(url),
        "dom_probe": dom_probe if isinstance(dom_probe, dict) else {},
        "dom_dirty": dom_indicates_dirty_conversation(dom_probe),
    }


def send_new_chat_shortcut(session: PageSession) -> None:
    """Press ``Cmd+Shift+O`` on the page using ``Input.dispatchKeyEvent``.

    We send a key-down (with both modifiers held) followed by a key-up
    so the keybinding handler observes a complete event pair.
    """
    base_params = {
        "modifiers": _MOD_META | _MOD_SHIFT,
        "key": "O",
        "code": "KeyO",
        "windowsVirtualKeyCode": 79,  # 'O'
        "nativeVirtualKeyCode": 79,
    }
    for event_type in ("keyDown", "keyUp"):
        params = dict(base_params)
        params["type"] = event_type
        try:
            rid = session._send_method("Input.dispatchKeyEvent", params)
            msg = session._await_response(rid, timeout=5.0)
        except CdpError as exc:
            raise NewChatShortcutFailedError(
                f"dispatchKeyEvent({event_type}) transport error: {exc}"
            ) from exc
        if "error" in msg:
            raise NewChatShortcutFailedError(
                f"dispatchKeyEvent({event_type}) returned error: {msg['error']}"
            )


def wait_until_clean(
    session: PageSession,
    *,
    timeout_seconds: float = _NEW_CHAT_TRANSITION_TIMEOUT_S,
    poll_seconds: float = _NEW_CHAT_TRANSITION_POLL_S,
) -> dict:
    """Block until both URL and DOM dirty signals are False.

    Returns the final probe dict on success.
    """
    deadline = _now() + timeout_seconds
    last: dict = {}
    while _now() < deadline:
        last = probe_dirty_state(session)
        if not last["url_dirty"] and not last["dom_dirty"]:
            return last
        time.sleep(poll_seconds)
    raise NewChatShortcutFailedError(
        f"tab still dirty after {timeout_seconds:.0f}s; last_probe={last!r}"
    )


def read_model_label(session: PageSession) -> Optional[str]:
    """Return ``button.input-area-switch``'s innerText (trimmed) or None."""
    expr = (
        "(function(){"
        f"  var el=document.querySelector({_js_string_literal(MODEL_SWITCH_BUTTON_SELECTOR)});"
        "  return el ? (el.innerText || '').trim() : null;"
        "})()"
    )
    try:
        v = _eval_value(session, expr, timeout=5.0)
    except (CdpError, CdpEvalError):
        return None
    return v if isinstance(v, str) and v else None


def _click_model_selector_open(session: PageSession) -> bool:
    """Click ``button.input-area-switch`` to open the model menu.

    Returns True if the click was issued; False if the button isn't
    in the DOM. We do not assert "the menu is now visible" here —
    that is checked separately by polling for menu items.
    """
    expr = (
        "(function(){"
        f"  var btn=document.querySelector({_js_string_literal(MODEL_SWITCH_BUTTON_SELECTOR)});"
        "  if(!btn) return false;"
        "  btn.click();"
        "  return true;"
        "})()"
    )
    try:
        v = _eval_value(session, expr, timeout=5.0)
    except (CdpError, CdpEvalError):
        return False
    return bool(v)


def _click_pro_menu_item(session: PageSession) -> dict:
    """Open the menu (if needed) and click the menu item whose text contains "Pro".

    Returns ``{"clicked": bool, "candidates": [...labels seen...]}``.
    Tolerant of the various Gemini menu shapes seen in the wild
    (``mat-action-list-item`` button, ``[role=menuitem]``, etc.).
    """
    expr = (
        "(function(){"
        "  var nodes = document.querySelectorAll("
        "    'button[mat-menu-item], [role=\"menuitem\"], "
        "     mat-action-list-item button, .menu-inner-container button'"
        "  );"
        "  var labels = [];"
        "  var hit = null;"
        "  for (var i=0; i<nodes.length; i++) {"
        "    var t = (nodes[i].innerText || '').trim();"
        "    labels.push(t);"
        "    if (!hit && t && /\\bPro\\b/.test(t)) { hit = nodes[i]; }"
        "  }"
        "  if (hit) { hit.click(); return {clicked: true, candidates: labels}; }"
        "  return {clicked: false, candidates: labels};"
        "})()"
    )
    try:
        v = _eval_value(session, expr, timeout=5.0)
    except (CdpError, CdpEvalError) as exc:
        return {"clicked": False, "candidates": [], "error": str(exc)}
    if not isinstance(v, dict):
        return {"clicked": False, "candidates": []}
    return v


def ensure_pro_model(
    session: PageSession,
    *,
    notes: list[str],
    on_log: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[str], Optional[str], bool]:
    """Make sure the current model is Pro.

    Returns ``(initial_label, final_label, switched)``.

    The operator's design decision (Q5 in PR15 design): "every time,
    open the selector and look — if it's not Pro, switch". So we
    always read the label first, and only when it's not Pro do we
    open the menu and click the Pro item. We never assume.
    """
    initial = read_model_label(session)
    _emit(notes, on_log, f"  model_label_initial={initial!r}")

    if label_is_pro(initial):
        return initial, initial, False

    # Open the menu.
    if not _click_model_selector_open(session):
        raise ProSwitchFailedError(
            "model selector button not found in DOM; cannot switch to Pro"
        )

    # Wait for menu items to render and try to click "Pro".
    deadline = _now() + _PRO_SWITCH_MENU_TIMEOUT_S
    last_attempt: dict = {}
    while _now() < deadline:
        result = _click_pro_menu_item(session)
        last_attempt = result
        if result.get("clicked"):
            break
        time.sleep(_PRO_SWITCH_MENU_POLL_S)
    else:
        raise ProSwitchFailedError(
            f"Pro menu item never appeared within {_PRO_SWITCH_MENU_TIMEOUT_S:.0f}s; "
            f"candidates_last_seen={last_attempt.get('candidates')!r}"
        )

    # After clicking, poll until the visible label flips to Pro.
    deadline = _now() + _PRO_SWITCH_MENU_TIMEOUT_S
    final: Optional[str] = None
    while _now() < deadline:
        final = read_model_label(session)
        if label_is_pro(final):
            _emit(notes, on_log, f"  model_label_after_switch={final!r}")
            return initial, final, True
        time.sleep(_PRO_SWITCH_MENU_POLL_S)

    raise ProSwitchFailedError(
        f"clicked Pro menu item but label did not converge to Pro; "
        f"final_label={final!r}, menu_candidates={last_attempt.get('candidates')!r}"
    )


def paste_query_into_input(session: PageSession, query: str) -> None:
    """Write ``query`` into the Quill editor and verify it landed.

    For session-prep we expect short to medium prompts (up to ~200 KB
    is fine for Quill in one ``insertText``; the megabyte-scale path
    that needs chunking lives in :mod:`gemini_web_driver`'s
    :meth:`inject_prompt`). We deliberately do **not** import that
    chunking machinery here to keep this module lean — if a caller
    needs to inject a giant prompt they should use the driver
    directly *after* preparation.
    """
    if not isinstance(query, str):
        raise TypeError("query must be a str")
    if not query:
        raise ValueError("query must be non-empty")

    payload = _js_string_literal(query)
    expr = (
        "(function(){"
        f"  var input=document.querySelector({_js_string_literal(INPUT_SELECTOR)});"
        "  if(!input) return {ok:false, reason:'input_not_found'};"
        "  input.focus();"
        "  // Clear any residual content first (Quill keeps a trailing \\n).\n"
        "  document.execCommand('selectAll', false, null);"
        "  document.execCommand('delete', false, null);"
        f"  var ok = document.execCommand('insertText', false, {payload});"
        "  return {ok: ok, length: input.innerText.length};"
        "})()"
    )
    try:
        res = _eval_value(session, expr, timeout=60.0)
    except (CdpError, CdpEvalError) as exc:
        raise SessionPrepError(f"insertText evaluate failed: {exc}") from exc
    if not isinstance(res, dict) or not res.get("ok"):
        raise SessionPrepError(f"insertText reported failure: {res!r}")


def press_enter_to_send(session: PageSession) -> None:
    """Dispatch an Enter keypress on the focused Quill editor.

    Quill listens for the Enter key on the editor element directly;
    a single ``keyDown`` + ``keyUp`` pair is sufficient.
    """
    base_params = {
        "key": "Enter",
        "code": "Enter",
        "windowsVirtualKeyCode": 13,
        "nativeVirtualKeyCode": 13,
        "modifiers": 0,
    }
    for event_type in ("keyDown", "keyUp"):
        params = dict(base_params)
        params["type"] = event_type
        try:
            rid = session._send_method("Input.dispatchKeyEvent", params)
            msg = session._await_response(rid, timeout=5.0)
        except CdpError as exc:
            raise SubmitShortcutFailedError(
                f"Enter dispatchKeyEvent({event_type}) transport error: {exc}"
            ) from exc
        if "error" in msg:
            raise SubmitShortcutFailedError(
                f"Enter dispatchKeyEvent({event_type}) returned error: {msg['error']}"
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def prepare_clean_session(
    client: ChromeCDPClient,
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> tuple[PageSession, SessionPrepResult]:
    """Locate (or create) a Gemini tab and return a *clean, Pro-mode* session.

    The caller owns the returned :class:`PageSession` and must close
    it (typically via context manager). We return the underlying
    session so callers can pass it straight to
    :meth:`gemini_web_driver.GeminiWebDriver.inject_prompt` /
    :meth:`submit` / :meth:`wait_for_streaming_done`.

    Steps:

    1. Find any existing Gemini tab via ``client.list_pages()``.
    2. If none, create one in the background and wait for hydration.
    3. Probe the tab for dirty signals (URL or DOM).
    4. If dirty, send Cmd+Shift+O and wait until both signals clear.
    5. Ensure the model selector is showing "Pro" (open the menu and
       click the Pro item if not).
    """
    notes: list[str] = []

    # --- 1) discover or create ------------------------------------------
    try:
        pages = client.list_pages()
    except CdpError as exc:
        raise NoCdpConnectionError(
            f"cannot list pages from {client.endpoint}: {exc}"
        ) from exc

    existing = pick_first_gemini_page(pages)
    used_existing = existing is not None

    if existing is None:
        _emit(notes, on_log, "no Gemini tab open; creating one in background")
        new_target_id = open_background_gemini_tab(client)
        _emit(notes, on_log, f"  new background target_id={new_target_id!r}")

        # The new target needs a moment before /json lists it with a
        # webSocketDebuggerUrl. Poll briefly.
        attached: Optional[PageSession] = None
        deadline = _now() + 10.0
        while _now() < deadline and attached is None:
            time.sleep(0.5)
            for p in client.list_pages():
                if p.id == new_target_id:
                    try:
                        attached = client.attach_to_page(new_target_id)
                    except CdpError:
                        attached = None
                    break
        if attached is None:
            raise NewTabFailedError(
                f"new tab {new_target_id!r} never showed up in /json or "
                f"could not be attached"
            )
        session = attached
        page_id = new_target_id
    else:
        _emit(notes, on_log, f"reusing existing Gemini tab id={existing.id!r}")
        session = client.attach_to_page(existing.id)
        page_id = existing.id

    try:
        # --- 2) wait until input editor is mounted --------------------
        wait_until_tab_hydrated(session)

        # --- 3) probe dirty state -------------------------------------
        probe = probe_dirty_state(session)
        _emit(
            notes,
            on_log,
            f"  initial probe: url_dirty={probe['url_dirty']}, "
            f"dom_dirty={probe['dom_dirty']}, "
            f"user_msgs={probe['dom_probe'].get('user_msgs')}, "
            f"model_msgs={probe['dom_probe'].get('model_msgs')}",
        )

        opened_new_chat = False
        if probe["url_dirty"] or probe["dom_dirty"]:
            _emit(notes, on_log, "  tab is dirty; sending Cmd+Shift+O")
            send_new_chat_shortcut(session)
            try:
                final_probe = wait_until_clean(session)
            except NewChatShortcutFailedError:
                _save_screenshot(session, "session_prep_new_chat_failed")
                raise
            opened_new_chat = True
            _emit(notes, on_log, f"  tab is now clean: url={final_probe['url']!r}")
            # Re-wait for the editor: a New-Chat transition unmounts and
            # remounts the Quill instance.
            wait_until_tab_hydrated(
                session, timeout_seconds=_INPUT_READY_TIMEOUT_S
            )

        # --- 4) ensure Pro model --------------------------------------
        try:
            initial_label, final_label, switched = ensure_pro_model(
                session, notes=notes, on_log=on_log
            )
        except ProSwitchFailedError:
            _save_screenshot(session, "session_prep_pro_switch_failed")
            raise

        # --- 5) read the final URL we ended up on ---------------------
        try:
            url_after = _eval_value(session, "location.href", timeout=5.0)
        except (CdpError, CdpEvalError):
            url_after = ""
        if not isinstance(url_after, str):
            url_after = ""

        result = SessionPrepResult(
            page_id=page_id,
            final_url=url_after,
            used_existing_tab=used_existing,
            opened_new_chat=opened_new_chat,
            model_switched_to_pro=switched,
            initial_model_label=initial_label,
            final_model_label=final_label,
            notes=tuple(notes),
        )
        return session, result
    except Exception:
        # On any failure inside the prepared block we close the
        # session we attached (the *tab* itself stays open per the
        # invisible rule) and re-raise.
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
        raise


def prepare_and_submit_query(
    client: ChromeCDPClient,
    query_text: str,
    *,
    on_log: Optional[Callable[[str], None]] = None,
) -> SessionPrepResult:
    """End-to-end convenience: prep + paste + Enter, then close session.

    .. deprecated::
        INCOMPATIBLE with the network-event completion in
        :meth:`GeminiWebDriver.wait_for_streaming_done`. That wait needs
        ``PageSession.enable_network()`` called on the SAME session *before*
        submit (so ``Network.requestWillBeSent``/``loadingFinished`` for the
        chat stream are captured), but this helper submits and then CLOSES the
        session, so a downstream fresh-session wait can never see those events.
        Has no in-repo call sites. Use the driver's
        find → submit → wait_for_streaming_done flow on one session instead;
        this helper will be removed.

    This was the highest-level helper, intended for callers that handed off
    waiting/extraction to :mod:`gemini_web_driver` with a *fresh* session — a
    pattern that the network-event completion has since invalidated (see the
    deprecation note above: the wait must run on the SAME session that had
    ``enable_network()`` called before submit). The helper closes the prep
    session before returning, which is exactly why it no longer composes with
    the current wait. For new code, drive prep + submit + wait on one session
    via :func:`prepare_clean_session` + the driver, rather than this helper.
    """
    session, result = prepare_clean_session(client, on_log=on_log)
    try:
        paste_query_into_input(session, query_text)
        press_enter_to_send(session)
        if on_log is not None:
            try:
                on_log(f"submitted {len(query_text)} chars to tab {result.page_id}")
            except Exception:  # noqa: BLE001
                pass
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
    return result


__all__ = [
    # errors
    "SessionPrepError",
    "NoCdpConnectionError",
    "NewTabFailedError",
    "NewChatShortcutFailedError",
    "ProSwitchFailedError",
    "SubmitShortcutFailedError",
    # result type
    "SessionPrepResult",
    # pure helpers
    "is_gemini_url",
    "url_indicates_dirty_conversation",
    "dom_indicates_dirty_conversation",
    "label_is_pro",
    "pick_first_gemini_page",
    # CDP-touching helpers (exposed for advanced callers/tests)
    "open_background_gemini_tab",
    "wait_until_tab_hydrated",
    "probe_dirty_state",
    "send_new_chat_shortcut",
    "wait_until_clean",
    "read_model_label",
    "ensure_pro_model",
    "paste_query_into_input",
    "press_enter_to_send",
    # public entry points
    "prepare_clean_session",
    "prepare_and_submit_query",
    # constants
    "GEMINI_FRESH_URL",
]
