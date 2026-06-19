"""ChatGPT WebDriver for invisible background orchestration.

Design constraints:
- Only attach to an already-open ChatGPT tab in the operator's local browser.
- Never activate the tab, never bring a window to front, never close tabs.
- Fresh chat is created by background navigation within the existing tab.
- Final model is read from the last assistant turn's
  ``data-message-model-slug`` attribute.
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


class GptDriverError(RuntimeError):
    """Base error for GPT driver operations."""


class GptTabNotFoundError(GptDriverError):
    """Raised when no ChatGPT tab is found in the browser."""


class GptInjectionFailedError(GptDriverError):
    """Raised when prompt injection into the input box fails."""


class GptSubmitFailedError(GptDriverError):
    """Raised when submitting the prompt fails."""


class GptStreamingTimeoutError(GptDriverError):
    """Raised when waiting for streaming completion times out."""


class GptExtractionFailedError(GptDriverError):
    """Raised when extracting the assistant message fails."""


_GPT_URL_FRAGMENTS = ("chatgpt.com", "chat.openai.com")
_GPT_NEW_CHAT_URL = "https://chatgpt.com/"
_INPUT_BOX_SELECTOR = "#prompt-textarea"
_ASSISTANT_TURN_SELECTOR = '[data-message-author-role="assistant"]'
_STOP_BUTTON_SELECTOR = 'button[data-testid="stop-button"], button[aria-label*="Stop"], button[aria-label*="停止"]'
_COMPOSER_PILL_SELECTOR = ".__composer-pill"
_NAVIGATE_HYDRATION_WAIT_SECONDS = 2.5
# submit() confirms asynchronously: ChatGPT clears the composer / swaps Send for
# Stop a beat after dispatch, so we poll instead of reading the DOM instantly.
_SUBMIT_CONFIRM_TIMEOUT_SECONDS = 5.0
_SUBMIT_CONFIRM_POLL_SECONDS = 0.25
# Grace period for the chat-stream request to appear after submit. Extended
# thinking starts streaming within seconds, so 60s is ample; a throttled/empty
# reply just times out rather than blocking for minutes.
_ANSWER_START_GRACE_SECONDS = 60
_INJECT_CHUNK_SIZE = 4000
_INJECT_CHUNK_DELAY_SECONDS = 0.05


class GptWebDriver:
    """Invisible CDP-based driver for ChatGPT."""

    def __init__(self, cdp_client: ChromeCDPClient) -> None:
        self._cdp_client = cdp_client

    def find_gpt_tab(self) -> PageSession:
        pages = self._cdp_client.list_pages()
        gpt_page: Optional[PageInfo] = None
        for page in pages:
            if any(fragment in page.url for fragment in _GPT_URL_FRAGMENTS):
                gpt_page = page
                break
        if gpt_page is None:
            raise GptTabNotFoundError(
                "No ChatGPT tab found. Please open https://chatgpt.com/ in your browser and ensure it is logged in."
            )
        try:
            return self._cdp_client.attach_to_page(gpt_page.id)
        except CdpError as exc:
            raise GptTabNotFoundError(f"Failed to attach to ChatGPT tab: {exc}") from exc

    def read_composer_lane(self, session: PageSession) -> str | None:
        try:
            result = session.runtime_evaluate(
                f"""
                (() => {{
                    const pill = document.querySelector('{_COMPOSER_PILL_SELECTOR}');
                    return pill ? (pill.innerText || pill.textContent || '').trim() : null;
                }})()
                """,
                timeout=10.0,
            )
            lane = _extract_value(result)
            return lane.strip() if isinstance(lane, str) and lane.strip() else None
        except CdpEvalError as exc:
            raise GptDriverError(f"Failed to read GPT composer lane: {exc}") from exc

    def navigate_new_chat(self, session: PageSession) -> None:
        """Open a fresh chat without leaving the current workspace.

        - On a Custom GPT URL (`/g/<id>` or `/g/<id>/c/<conv>`), strip the
          conversation id (keep `/g/<id>`) so the new chat stays inside that GPT.
        - On a project URL (`/c/<id>` inside a project) or any other URL,
          navigate to `/` (default workspace).
        """
        try:
            current_url = _extract_value(session.runtime_evaluate("window.location.href", timeout=5.0)) or ""
            target_url = _GPT_NEW_CHAT_URL
            if isinstance(current_url, str) and "/g/" in current_url:
                # Custom GPT: keep /g/<id>, drop trailing /c/<conv-id> if present.
                try:
                    base, _, after_g = current_url.partition("/g/")
                    gpt_id = after_g.split("/", 1)[0]
                    if gpt_id:
                        target_url = f"{base}/g/{gpt_id}"
                except Exception:
                    target_url = _GPT_NEW_CHAT_URL
            rid = session._send_method("Page.navigate", {"url": target_url})
            nav_result = session._await_response(rid, timeout=15.0)
            if "error" in nav_result:
                raise GptDriverError(f"Page.navigate failed: {nav_result['error']}")
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                try:
                    ready_result = session.runtime_evaluate("document.readyState", timeout=5.0)
                    if _extract_value(ready_result) == "complete":
                        break
                except CdpEvalError:
                    pass
                time.sleep(0.5)
            else:
                raise GptDriverError("Timed out waiting for ChatGPT page load after navigation")
            time.sleep(_NAVIGATE_HYDRATION_WAIT_SECONDS)
        except CdpError as exc:
            raise GptDriverError(f"Navigation to new chat failed: {exc}") from exc

    def inject_prompt(self, session: PageSession, prompt: str) -> None:
        try:
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
            chunks = _split_into_safe_chunks(prompt, _INJECT_CHUNK_SIZE)
            for idx, chunk in enumerate(chunks):
                rid = session._send_method("Input.insertText", {"text": chunk})
                insert_result = session._await_response(rid, timeout=15.0)
                if "error" in insert_result:
                    raise GptInjectionFailedError(
                        f"Input.insertText chunk {idx + 1}/{len(chunks)} failed: {insert_result['error']}"
                    )
                if idx < len(chunks) - 1:
                    time.sleep(_INJECT_CHUNK_DELAY_SECONDS)
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
                raise GptInjectionFailedError(
                    f"Prompt injection verification failed: expected ~{expected_length} chars, got {injected_length}"
                )
        except CdpEvalError as exc:
            raise GptInjectionFailedError(f"Failed to inject prompt: {exc}") from exc
        except CdpError as exc:
            raise GptInjectionFailedError(f"CDP error during prompt injection: {exc}") from exc

    def baseline_response_count(self, session: PageSession) -> int:
        try:
            result = session.runtime_evaluate(
                f"document.querySelectorAll('{_ASSISTANT_TURN_SELECTOR}').length",
                timeout=10.0,
            )
            count = _extract_value(result)
            return int(count or 0)
        except (CdpEvalError, TypeError, ValueError) as exc:
            raise GptDriverError(f"Failed to read GPT baseline response count: {exc}") from exc

    def submit(self, session: PageSession) -> None:
        try:
            # Un-throttle DOM rendering for this background tab (no window raise)
            # FIRST: otherwise the reply's bytes arrive but are never painted into
            # the DOM until the tab is foregrounded, so extract reads empty. Doing
            # it before enable_network() also keeps any visibility-resume traffic
            # out of the Network capture window (which enable_network clears).
            session.set_focus_emulation(True)
            # Then enable the Network domain so wait_for_streaming_done can observe
            # the chat-stream request + loadingFinished (CDP does not replay
            # pre-enable events; enable_network also clears stale buffered events).
            session.enable_network()
            btn_result = session.runtime_evaluate(
                f"""
                (() => {{
                    const input = document.querySelector('{_INPUT_BOX_SELECTOR}');
                    if (!input) return {{status: 'NO_INPUT'}};
                    const hasText = !!((input.innerText || input.textContent || '').trim());
                    if (!hasText) return {{status: 'EMPTY'}};
                    return {{status: 'OK'}};
                }})()
                """,
                timeout=10.0,
            )
            btn_status = _extract_value(btn_result) or {}
            if btn_status.get("status") == "NO_INPUT":
                raise GptSubmitFailedError("GPT input box not found.")
            if btn_status.get("status") == "EMPTY":
                raise GptSubmitFailedError("GPT input is empty — refusing to submit.")
            # Submit with a SINGLE action to avoid a double-fire race: pressing
            # Enter AND then clicking risks the click landing on the Stop button
            # (Enter already submitted, Send morphed into Stop async) which
            # aborts the generation into an empty turn. Prefer clicking the
            # positively-identified send button — after submit its data-testid
            # flips to "stop-button", so this selector can never re-fire. Fall
            # back to Enter only when no send button is present. Success is NOT
            # judged here (the UI clears asynchronously); the poll below confirms.
            session.runtime_evaluate(
                f"""
                (() => {{
                    const input = document.querySelector('{_INPUT_BOX_SELECTOR}');
                    if (!input) return {{dispatched: false}};
                    input.focus();
                    const btn = document.querySelector('button[data-testid="send-button"]');
                    if (btn && !btn.disabled && btn.getAttribute('aria-disabled') !== 'true') {{
                        btn.click();
                        return {{dispatched: 'click'}};
                    }}
                    for (const type of ['keydown', 'keypress', 'keyup']) {{
                        input.dispatchEvent(new KeyboardEvent(type, {{
                            key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
                            bubbles: true, cancelable: true,
                        }}));
                    }}
                    return {{dispatched: 'enter'}};
                }})()
                """,
                timeout=10.0,
            )
            # Confirm the submit actually took effect: composer cleared, or a
            # Stop button appeared (streaming started). Poll to absorb the async
            # clear instead of reading once and racing the UI.
            confirm_js = f"""
            (() => {{
                const input = document.querySelector('{_INPUT_BOX_SELECTOR}');
                const inputEmpty = !!input && !((input.innerText || input.textContent || '').trim());
                const streaming = !!document.querySelector('{_STOP_BUTTON_SELECTOR}');
                return {{inputEmpty, streaming}};
            }})()
            """
            deadline = time.monotonic() + _SUBMIT_CONFIRM_TIMEOUT_SECONDS
            while True:
                state = _extract_value(session.runtime_evaluate(confirm_js, timeout=10.0)) or {}
                if state.get("inputEmpty") or state.get("streaming"):
                    return
                if time.monotonic() >= deadline:
                    raise GptSubmitFailedError(
                        "GPT submit not confirmed: composer never cleared and no "
                        f"Stop button appeared within {_SUBMIT_CONFIRM_TIMEOUT_SECONDS}s."
                    )
                time.sleep(_SUBMIT_CONFIRM_POLL_SECONDS)
        except CdpEvalError as exc:
            raise GptSubmitFailedError(f"Failed to submit GPT prompt: {exc}") from exc
        except CdpError as exc:
            raise GptSubmitFailedError(f"CDP error during GPT submit: {exc}") from exc

    def wait_for_streaming_done(
        self,
        session: PageSession,
        *,
        timeout_seconds: int = 180,
        baseline_response_count: Optional[int] = None,
    ) -> None:
        # Network-event completion: block until the chat-stream POST finishes,
        # instead of polling DOM proxies. Robust to thinking models — ChatGPT's
        # SSE stream (text/event-stream on /backend-api/f/conversation) stays
        # open through the entire think+answer and only then loadingFinishes,
        # whereas the stop button vanishes mid-think. ``enable_network()`` was
        # called in submit() before dispatch.
        #
        # ``baseline_response_count`` (the assistant-turn count captured by the
        # caller BEFORE submit) gates the readiness probe: in an existing chat
        # the previous answer would otherwise satisfy "has non-empty markdown"
        # during render lag. We require a NEW turn (count > baseline) first.
        base = baseline_response_count
        if base is None:
            base = self.baseline_response_count(session)

        # Completion = the VISIBLE per-turn action toolbar container has mounted.
        # That container (Tailwind ``min-h-[46px]`` — the copy/share/… bar)
        # appears ONLY when the turn is fully complete. Validated live: it stays
        # ABSENT while the answer streams (ansLen 0→50→113…) and flips present
        # exactly when the text settles. It succeeds where every other signal
        # failed for GPT-5.x Thinking:
        #   - stop button: vanishes mid-think
        #   - /conversation loadingFinished: fires on the early stream_handoff,
        #     not the answer (which streams over a separate resume channel)
        #   - text-stability: false-fires during the model's mid-answer pauses
        #   - the copy-button data-testid: present (even visible) THROUGHOUT,
        #     including mid-think — carries zero completion info
        # Gated on a NEW turn (count>baseline) + non-thinking answer text; the
        # toolbar COUNT must reach the assistant-turn count, so a prior turn's
        # lingering toolbar in a non-fresh chat can't satisfy `done` early.
        def _gpt_answer_ready() -> bool:
            r = _extract_value(
                session.runtime_evaluate(
                    "(()=>{const a=document.querySelectorAll('"
                    + _ASSISTANT_TURN_SELECTOR
                    + "');const l=a[a.length-1];"
                    "const m=l?[...l.querySelectorAll('.markdown')]"
                    ".filter(x=>!x.className.includes('result-thinking')):[];"
                    "const hasText=m.length>0 && (m[m.length-1].innerText||'').trim().length>0;"
                    "const done=document.querySelectorAll('[class*=\\\"min-h-[46px]\\\"]').length>=a.length;"
                    "return {count:a.length, hasText:hasText, done:done};})()",
                    timeout=5.0,
                )
            )
            return (
                isinstance(r, dict)
                and int(r.get("count", 0)) > base
                and bool(r.get("hasText"))
                and bool(r.get("done"))
            )

        try:
            session.wait_for_stream_completion(
                lambda p: (
                    p.get("request", {}).get("method") == "POST"
                    and "/backend-api/f/conversation"
                    in (p.get("request", {}).get("url", "") or "")
                    and "/conversation/prepare"
                    not in (p.get("request", {}).get("url", "") or "")
                ),
                ready_check=_gpt_answer_ready,
                overall_timeout=float(timeout_seconds),
                start_timeout=float(_ANSWER_START_GRACE_SECONDS),
                # Poll for the rendered answer for the whole budget, not the
                # default 10s. GPT-5.x Thinking fires an early /conversation
                # request whose loadingFinished lands seconds in — long before
                # the answer renders — so a short render window gives up
                # mid-think ("stream finished but answer did not render").
                render_timeout=float(timeout_seconds),
            )
        except CdpError as exc:
            raise GptStreamingTimeoutError(
                f"GPT chat stream did not complete within {timeout_seconds}s "
                f"(network signal): {exc}"
            ) from exc

    def extract_last_assistant_message(self, session: PageSession) -> str:
        js = (
            "(()=>{"
            f"var turns=document.querySelectorAll('{_ASSISTANT_TURN_SELECTOR}');"
            "var last=turns.length?turns[turns.length-1]:null;"
            "if(!last)return '';"
            "var mds=Array.from(last.querySelectorAll('.markdown'));"
            "var answer=null;"
            "for(var i=mds.length-1;i>=0;i--){"
            "if(mds[i].classList.contains('result-thinking'))continue;"
            "var t=(mds[i].innerText||mds[i].textContent||'').trim();"
            "if(t){answer=mds[i];break;}}"
            "var container=answer||last.querySelector('.markdown:not(.result-thinking)')||last.querySelector('.markdown')||last;"
            "function htmlToMd(node){"
            "if(!node)return '';"
            "if(node.nodeType===3)return node.textContent;"
            "if(node.nodeType!==1)return '';"
            "var tag=node.tagName.toLowerCase();"
            "var childMd=Array.from(node.childNodes).map(htmlToMd).join('');"
            "if(tag==='br')return '\\n';"
            "if(tag==='p'||tag==='div')return childMd+'\\n\\n';"
            "if(/^h[1-6]$/.test(tag))return '#'.repeat(parseInt(tag[1]))+' '+childMd+'\\n\\n';"
            "if(tag==='strong'||tag==='b')return '**'+childMd+'**';"
            "if(tag==='em'||tag==='i')return '*'+childMd+'*';"
            "if(tag==='code'&&node.parentElement&&node.parentElement.tagName.toLowerCase()!=='pre')return '`'+childMd+'`';"
            "if(tag==='pre'){"
            "var code=node.querySelector('code');"
            "var lang='';"
            "if(code){var cls=code.className||'';var m=cls.match(/language-(\\S+)/);if(m)lang=m[1];}"
            "var raw=code?code.textContent:node.textContent;"
            "return '```'+lang+'\\n'+raw+'\\n```\\n\\n';}"
            "if(tag==='a'){var href=node.getAttribute('href')||'';return '['+childMd+']('+href+')';}"
            "if(tag==='ul'||tag==='ol'){"
            "var items=Array.from(node.children).filter(function(c){return c.tagName.toLowerCase()==='li';});"
            "var ordered=tag==='ol';"
            "return items.map(function(li,i){var prefix=ordered?(i+1)+'. ':'- ';return prefix+htmlToMd(li).replace(/\\n+$/,'');}).join('\\n')+'\\n\\n';}"
            "if(tag==='li')return childMd;"
            "if(tag==='table'){"
            "var md='';var rows=node.querySelectorAll('tr');"
            "for(var ri=0;ri<rows.length;ri++){"
            "var cells=rows[ri].querySelectorAll('th,td');"
            "var cellTexts=Array.from(cells).map(function(c){return c.textContent.trim();});"
            "md+='| '+cellTexts.join(' | ')+' |\\n';"
            "if(ri===0){md+='|'+cellTexts.map(function(){return '---';}).join('|')+'|\\n';}}"
            "return md+'\\n';}"
            "if(['thead','tbody','tfoot','tr','th','td'].indexOf(tag)>=0)return childMd;"
            "if(tag==='blockquote')return childMd.split('\\n').map(function(l){return '> '+l;}).join('\\n')+'\\n\\n';"
            "if(tag==='hr')return '---\\n\\n';"
            "return childMd;}"
            "return htmlToMd(container).trim();})()"
        )
        try:
            result = session.runtime_evaluate(js, timeout=15.0)
            text = _extract_value(result) or ""
            if not text or not isinstance(text, str):
                raise GptExtractionFailedError("No GPT assistant message found or message is empty")
            return text
        except CdpEvalError as exc:
            raise GptExtractionFailedError(f"Failed to extract GPT assistant message: {exc}") from exc

    def extract_last_model_slug(self, session: PageSession) -> str | None:
        try:
            result = session.runtime_evaluate(
                f"""
                (() => {{
                    const turns = document.querySelectorAll('{_ASSISTANT_TURN_SELECTOR}');
                    const last = turns.length ? turns[turns.length - 1] : null;
                    return last ? (last.getAttribute('data-message-model-slug') || null) : null;
                }})()
                """,
                timeout=10.0,
            )
            slug = _extract_value(result)
            return slug if isinstance(slug, str) and slug.strip() else None
        except CdpEvalError as exc:
            raise GptExtractionFailedError(f"Failed to extract GPT model slug: {exc}") from exc


__all__ = [
    "GptDriverError",
    "GptTabNotFoundError",
    "GptInjectionFailedError",
    "GptSubmitFailedError",
    "GptStreamingTimeoutError",
    "GptExtractionFailedError",
    "GptWebDriver",
]
