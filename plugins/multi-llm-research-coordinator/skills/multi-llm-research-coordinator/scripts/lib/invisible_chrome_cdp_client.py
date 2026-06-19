"""Tiny stdlib-only Chrome DevTools Protocol client for the PR9 orchestrator.

Scope (PR9.B):

- Used **only** by ``gemini_web_driver`` to talk to the user's local
  browser (Brave / Chrome) on its remote debugging port.
- Strictly limited to the read / evaluate / screenshot subset; any CDP
  method that would steal focus, navigate, reload, or close the user's
  tab is **intentionally not implemented**, per the chrome-cdp skill's
  invisible principle.
- Stdlib only — no Playwright, no ``websockets`` / ``websocket-client``
  package. RFC 6455 framing is implemented inline below.

This module never imports ``copilot_evidence.cdp``: PR9 chose physical
isolation (Plan B) over reuse, so the existing collector path stays
unchanged and any breakage here cannot affect ``copilot-evidence
collect``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

# RFC 6455 fixed magic GUID for Sec-WebSocket-Accept verification.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Generous default; CDP control replies are tiny but Page.captureScreenshot
# can be a few MB, and we want a single recv loop to handle either.
_RECV_CHUNK = 65536

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PageInfo:
    """One ``/json`` entry from the browser, narrowed to the fields we use."""

    id: str
    type: str
    url: str
    title: str
    web_socket_debugger_url: str

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CdpError(RuntimeError):
    """Raised on any CDP-level failure (HTTP, WS, or method error)."""

class CdpEvalError(CdpError):
    """``Runtime.evaluate`` returned a JS exception or non-zero error."""

# ---------------------------------------------------------------------------
# Endpoint validation
# ---------------------------------------------------------------------------

def _validate_local_endpoint(endpoint: str) -> tuple[str, int]:
    """Reject any non-loopback endpoint to keep CDP traffic on the operator's
    machine.

    Returns ``(host, port)`` on success; raises ``ValueError`` otherwise.
    """
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("endpoint must be a non-empty string")
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"endpoint scheme must be http(s), got {parsed.scheme!r}"
        )
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise ValueError(
            "PR9 only allows local CDP endpoint "
            f"(host must be one of {sorted(_ALLOWED_HOSTS)}, got {host!r})"
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 9222)
    return host, port

# ---------------------------------------------------------------------------
# RFC 6455 helpers (stdlib socket only)
# ---------------------------------------------------------------------------

def _ws_handshake(
    host: str, port: int, path: str, *, timeout: float = 10.0
) -> tuple[socket.socket, bytes]:
    """Open a TCP socket and perform a client-side WebSocket handshake.

    Returns ``(sock, leftover)``: ``leftover`` is any bytes read past the
    handshake terminator that already belong to the WS data stream.
    """
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(request.encode("ascii"))

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(_RECV_CHUNK)
        if not chunk:
            sock.close()
            raise CdpError("websocket handshake: server closed before reply")
        buf += chunk
        if len(buf) > 16384:  # arbitrary sanity cap
            sock.close()
            raise CdpError("websocket handshake: header too large")
    head, _, leftover = buf.partition(b"\r\n\r\n")

    status_line = head.split(b"\r\n", 1)[0]
    if b"101" not in status_line:
        sock.close()
        raise CdpError(
            f"websocket handshake failed: status_line={status_line!r}"
        )
    expected = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    )
    if expected not in head:
        sock.close()
        raise CdpError("websocket handshake: Sec-WebSocket-Accept mismatch")
    return sock, leftover

def _ws_send_text(sock: socket.socket, text: str) -> None:
    """Send a single masked text frame (FIN=1, opcode=0x1)."""
    payload = text.encode("utf-8")
    header = bytearray([0x81])  # FIN | opcode=text
    plen = len(payload)
    if plen < 126:
        header.append(0x80 | plen)
    elif plen < (1 << 16):
        header.append(0x80 | 126)
        header += struct.pack(">H", plen)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", plen)
    mask = os.urandom(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)

def _ws_recv_frame(
    sock: socket.socket, leftover: bytes
) -> tuple[int, bytes, bytes]:
    """Read one whole WS frame (handles continuation re-assembly).

    Returns ``(opcode, payload, leftover)``. Server frames are unmasked.
    Control frames (ping / close) are surfaced to the caller via opcode.
    """
    buf = bytearray(leftover)

    def need(n: int) -> None:
        while len(buf) < n:
            chunk = sock.recv(_RECV_CHUNK)
            if not chunk:
                raise CdpError("websocket recv: connection closed")
            buf.extend(chunk)

    payload_acc = bytearray()
    first_opcode: Optional[int] = None

    while True:
        need(2)
        b1, b2 = buf[0], buf[1]
        fin = (b1 & 0x80) != 0
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        plen = b2 & 0x7F
        pos = 2
        if plen == 126:
            need(pos + 2)
            plen = struct.unpack(">H", bytes(buf[pos : pos + 2]))[0]
            pos += 2
        elif plen == 127:
            need(pos + 8)
            plen = struct.unpack(">Q", bytes(buf[pos : pos + 8]))[0]
            pos += 8
        if masked:
            need(pos + 4)
            mask_key: Optional[bytes] = bytes(buf[pos : pos + 4])
            pos += 4
        else:
            mask_key = None
        need(pos + plen)
        frame_payload = bytes(buf[pos : pos + plen])
        if mask_key:
            frame_payload = bytes(
                b ^ mask_key[i % 4] for i, b in enumerate(frame_payload)
            )
        # Drop consumed bytes from buf for the next iteration / call.
        del buf[: pos + plen]

        if first_opcode is None:
            first_opcode = opcode
        payload_acc.extend(frame_payload)

        if fin:
            return first_opcode, bytes(payload_acc), bytes(buf)
        # else: continuation; loop and read the next frame.

def _ws_send_close(sock: socket.socket) -> None:
    """Best-effort RFC 6455 close frame. Never raises."""
    try:
        # opcode=0x8 (close), empty payload, masked.
        mask = os.urandom(4)
        sock.sendall(bytes([0x88, 0x80]) + mask)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# PageSession
# ---------------------------------------------------------------------------

# Cap on events buffered between command responses (safety net; during an
# active wait_for_event the buffer is drained continuously and stays small).
_MAX_BUFFERED_EVENTS = 2000


class PageSession:
    """A single attached CDP page session (one WebSocket).

    The session owns a stdlib ``socket`` and a tiny request-id counter.
    It is **not** thread-safe; one ``GeminiWebDriver`` per session.

    Allowed methods (whitelist):
      - ``runtime_evaluate``
      - ``capture_screenshot``
      - ``enable_network`` + ``wait_for_event`` / ``wait_for_stream_completion``
        (read-only observation of CDP Network events; no focus change)
      - ``set_focus_emulation`` (emulates page-perceived focus/visibility so a
        background tab still paints — does NOT raise the window or change the
        active tab; the invisible principle is preserved)
      - ``close`` (drops the WS, does **not** close the user's tab)

    Intentionally **not** implemented (see chrome-cdp invisible principle):
      - ``bring_to_front``           # Intentionally not implemented: violates chrome-cdp invisible principle.
      - ``activate_target``          # Intentionally not implemented: violates chrome-cdp invisible principle.
      - ``reload``                   # Intentionally not implemented: violates chrome-cdp invisible principle.
      - ``navigate``                 # Intentionally not implemented: violates chrome-cdp invisible principle.
      - ``close_target``             # Intentionally not implemented: violates chrome-cdp invisible principle.
      - ``create_new_tab``           # Intentionally not implemented: violates chrome-cdp invisible principle.
      - ``dispatch_mouse_event``     # Intentionally not implemented: violates chrome-cdp invisible principle.
      - ``dispatch_key_event``       # Intentionally not implemented: violates chrome-cdp invisible principle.
      - ``open_new_window``          # Intentionally not implemented: violates chrome-cdp invisible principle.
    """

    def __init__(
        self,
        sock: socket.socket,
        leftover: bytes,
        page_id: str,
        *,
        recv_timeout: float = 30.0,
    ) -> None:
        self._sock = sock
        self._leftover = leftover
        self._page_id = page_id
        self._next_id = 0
        self._closed = False
        # CDP events seen while awaiting command responses are buffered here so
        # wait_for_event() can consume them later (FIFO-capped, see _await_response).
        self._events: list[dict] = []
        # Block-level timeout for individual recv() calls. Method-level
        # timeouts are managed by callers (``runtime_evaluate`` accepts an
        # explicit ``timeout`` kwarg below).
        self._sock.settimeout(recv_timeout)

    # ----- public id ----------------------------------------------------

    @property
    def page_id(self) -> str:
        return self._page_id

    # ----- low-level CDP request/response -------------------------------

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _send_method(self, method: str, params: Optional[dict] = None) -> int:
        if self._closed:
            raise CdpError("PageSession is closed")
        rid = self._next_request_id()
        msg = {"id": rid, "method": method, "params": params or {}}
        _ws_send_text(self._sock, json.dumps(msg, ensure_ascii=False))
        return rid

    def _await_response(
        self, request_id: int, *, timeout: float
    ) -> dict:
        """Read frames until we see ``{id: request_id}``; ignore CDP events.

        Per-frame timeout uses the socket-level timeout; ``timeout`` is the
        wall-clock budget. We stop and raise once the budget is exhausted.
        """
        import time as _time

        deadline = _time.monotonic() + max(timeout, 0.0)
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise CdpError(
                    f"timeout waiting for CDP response id={request_id}"
                )
            self._sock.settimeout(min(remaining, 30.0))
            opcode, payload, self._leftover = _ws_recv_frame(
                self._sock, self._leftover
            )
            if opcode == 0x9:  # ping; reply with pong
                self._send_pong(payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                self._closed = True
                raise CdpError("server sent WebSocket close")
            if opcode != 0x1:  # text
                continue  # ignore binary; CDP uses text only
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CdpError(
                    f"malformed CDP frame: {exc}"
                ) from exc
            if msg.get("id") == request_id:
                return msg
            # A CDP event (has "method", no matching id). Buffer it so
            # wait_for_event() can consume events that arrived while we were
            # blocked on this command response. FIFO-capped to bound memory.
            if "method" in msg:
                self._events.append(msg)
                if len(self._events) > _MAX_BUFFERED_EVENTS:
                    del self._events[: len(self._events) - _MAX_BUFFERED_EVENTS]

    def _send_pong(self, payload: bytes) -> None:
        try:
            mask = os.urandom(4)
            header = bytearray([0x8A])  # FIN | opcode=pong
            plen = len(payload)
            if plen < 126:
                header.append(0x80 | plen)
            else:
                # control frames must fit in 125 bytes; truncate quietly
                payload = payload[:125]
                header.append(0x80 | len(payload))
            header += mask
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            self._sock.sendall(bytes(header) + masked)
        except OSError:
            pass

    # ----- whitelisted methods ------------------------------------------

    def runtime_evaluate(
        self,
        expression: str,
        *,
        await_promise: bool = False,
        return_by_value: bool = True,
        timeout: float = 30.0,
    ) -> dict:
        """Run ``Runtime.evaluate`` and return the ``result`` envelope.

        ``return_by_value`` defaults to True so callers get plain dicts /
        primitives back rather than remote object handles.

        Raises :class:`CdpEvalError` if the expression threw, and
        :class:`CdpError` for transport problems.
        """
        params = {
            "expression": expression,
            "awaitPromise": bool(await_promise),
            "returnByValue": bool(return_by_value),
        }
        rid = self._send_method("Runtime.evaluate", params)
        msg = self._await_response(rid, timeout=timeout)
        if "error" in msg:
            raise CdpError(
                f"Runtime.evaluate transport error: {msg['error']}"
            )
        result = msg.get("result", {})
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise CdpEvalError(
                f"Runtime.evaluate threw: {details.get('text')!r} "
                f"value={details.get('exception', {}).get('description')!r}"
            )
        return result

    def capture_screenshot(
        self,
        *,
        format: str = "png",
        timeout: float = 30.0,
    ) -> bytes:
        """``Page.captureScreenshot`` with ``captureBeyondViewport=False``.

        Picking ``captureBeyondViewport=False`` means the snapshot is just
        what is currently rendered in the tab — no scroll, no full-page
        re-layout, and no focus change. The browser does not bring the
        target tab to the foreground to grab the bitmap.
        """
        if format not in ("png", "jpeg"):
            raise ValueError("format must be 'png' or 'jpeg'")
        rid = self._send_method(
            "Page.captureScreenshot",
            {"format": format, "captureBeyondViewport": False},
        )
        msg = self._await_response(rid, timeout=timeout)
        if "error" in msg:
            raise CdpError(
                f"Page.captureScreenshot transport error: {msg['error']}"
            )
        result = msg.get("result", {})
        data_b64 = result.get("data")
        if not isinstance(data_b64, str):
            raise CdpError(
                "Page.captureScreenshot returned no 'data' field"
            )
        try:
            return base64.b64decode(data_b64)
        except (ValueError, TypeError) as exc:
            raise CdpError(
                f"Page.captureScreenshot returned invalid base64: {exc}"
            ) from exc

    # ----- CDP events / network-based completion ------------------------

    def enable_network(self, *, timeout: float = 10.0) -> None:
        """Enable the CDP ``Network`` domain for this session so that
        ``requestWillBeSent`` / ``loadingFinished`` events are delivered.
        Idempotent.

        Call this BEFORE the action that triggers the request you want to
        observe — CDP does not replay events from before the domain was on. Any
        events buffered from a prior submit are dropped so the next
        wait_for_event starts from a clean, current window.
        """
        rid = self._send_method("Network.enable")
        msg = self._await_response(rid, timeout=timeout)
        if "error" in msg:
            raise CdpError(f"Network.enable transport error: {msg['error']}")
        self._events.clear()

    def set_focus_emulation(self, enabled: bool = True, *, timeout: float = 10.0) -> None:
        """Make the page BEHAVE as focused/visible WITHOUT raising the window or
        changing the active tab — ``Emulation.setFocusEmulationEnabled``.

        Background tabs throttle/defer DOM rendering (rAF throttling, deferred
        paints), so a chat reply's bytes arrive over the network but are never
        painted into the DOM until the tab is foregrounded. Emulating focus flips
        ``document.visibilityState`` to ``"visible"`` and ``document.hasFocus()``
        to true *for the page only*, which un-throttles rendering — while the real
        window stays exactly where it is. This preserves the invisible principle
        (no ``Page.bringToFront`` / ``Target.activateTarget``): only the page's
        self-perceived visibility changes, not the OS focus or the active tab.

        Call after any navigation and before reading rendered content from a
        background tab. The override is **session-scoped**: it applies only to
        this CDP connection and is dropped automatically when the session
        detaches (i.e. on :meth:`close`), so no explicit teardown is required —
        the user's real tab is never left in an emulated-focus state.
        """
        rid = self._send_method(
            "Emulation.setFocusEmulationEnabled", {"enabled": bool(enabled)}
        )
        msg = self._await_response(rid, timeout=timeout)
        if "error" in msg:
            raise CdpError(
                f"Emulation.setFocusEmulationEnabled error: {msg['error']}"
            )

    def wait_for_event(
        self,
        method,
        predicate=None,
        *,
        timeout: float = 60.0,
    ) -> dict:
        """Block until a CDP event whose name is (one of) ``method`` and whose
        ``params`` satisfy ``predicate`` arrives; return the FULL event dict
        (``{"method": ..., "params": ...}``) so callers can tell which method
        matched (e.g. loadingFinished vs loadingFailed).

        ``method`` is a single method name or an iterable of names. Events seen
        while awaiting command responses are buffered (see :meth:`_await_response`),
        so this drains the buffer first, then reads new frames. Raises
        :class:`CdpError` on timeout.
        """
        import time as _time

        methods = {method} if isinstance(method, str) else set(method)

        def _match(ev: dict) -> bool:
            if ev.get("method") not in methods:
                return False
            return predicate is None or bool(predicate(ev.get("params", {})))

        # 1) already-buffered events (oldest first)
        for i, ev in enumerate(self._events):
            if _match(ev):
                del self._events[i]
                return ev

        # 2) read new frames until a match or the budget runs out. We set the
        # socket timeout to the FULL remaining budget and do NOT retry on a
        # timeout: ``_ws_recv_frame`` discards any partially-read frame on
        # timeout, so retrying mid-frame would desync ``self._leftover`` and
        # corrupt the stream. A single blocking read therefore either returns a
        # whole frame (leftover stays consistent) or, only once the entire
        # budget is gone, raises — and during an active generation frames arrive
        # continuously, so the read returns promptly.
        deadline = _time.monotonic() + max(timeout, 0.0)
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise CdpError(f"timeout waiting for CDP event {sorted(methods)!r}")
            self._sock.settimeout(remaining)
            try:
                opcode, payload, self._leftover = _ws_recv_frame(
                    self._sock, self._leftover
                )
            except socket.timeout:
                raise CdpError(
                    f"timeout waiting for CDP event {sorted(methods)!r}"
                )
            if opcode == 0x9:  # ping
                self._send_pong(payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                self._closed = True
                raise CdpError("server sent WebSocket close")
            if opcode != 0x1:  # text only
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if _match(msg):
                return msg
            # Buffer other events; stray command responses (have "id") are ignored.
            if "method" in msg:
                self._events.append(msg)
                if len(self._events) > _MAX_BUFFERED_EVENTS:
                    del self._events[: len(self._events) - _MAX_BUFFERED_EVENTS]

    def wait_for_stream_completion(
        self,
        request_matches,
        *,
        overall_timeout: float = 180.0,
        start_timeout: float = 60.0,
        ready_check=None,
        render_timeout: float = 10.0,
        settle_seconds: float = 1.0,
    ) -> None:
        """Wait for a chat reply by watching the network, not the DOM.

        ``request_matches(request_params)`` returns True for the chat streaming
        request — the long-lived POST that carries the model's reply. We capture
        its id from ``Network.requestWillBeSent`` and block until that request's
        ``loadingFinished`` (or ``loadingFailed``) — the deterministic "generation
        complete" signal. The stream stays open for the entire generation,
        including extended thinking, so this is robust where DOM heuristics
        (stop button / text-stability) are not.

        Requires :meth:`enable_network` to have been called BEFORE the submit
        that triggers the request.

        ``loadingFinished`` fires when the bytes are received, a beat before the
        DOM paints them. To absorb that render lag deterministically, pass
        ``ready_check`` — a zero-arg callable returning True once the answer is
        rendered (e.g. the answer element has non-empty text). We poll it up to
        ``render_timeout`` (also capped by the overall budget) and return as
        soon as it's True. Without a ``ready_check`` we fall back to a fixed
        ``settle_seconds`` sleep.

        Raises :class:`CdpError` if the request never appears, the stream
        ``loadingFailed``, or (with a ``ready_check``) the answer never renders.
        ``overall_timeout`` bounds the whole call, not just one phase.
        """
        import time as _time

        overall_deadline = _time.monotonic() + max(overall_timeout, 0.0)

        # 1) capture the chat-stream request id (bounded by start_timeout AND the
        #    overall budget — whichever is sooner).
        start_budget = min(start_timeout, max(overall_deadline - _time.monotonic(), 0.0))
        req_ev = self.wait_for_event(
            "Network.requestWillBeSent", request_matches, timeout=start_budget
        )
        request_id = (req_ev.get("params", {}) or {}).get("requestId")
        if not request_id:
            raise CdpError("chat-stream requestWillBeSent had no requestId")

        # 2) wait for that request to terminate; a failed stream is an error,
        #    not a silent success.
        term = self.wait_for_event(
            ("Network.loadingFinished", "Network.loadingFailed"),
            lambda p: p.get("requestId") == request_id,
            timeout=max(overall_deadline - _time.monotonic(), 0.0),
        )
        if term.get("method") == "Network.loadingFailed":
            fp = term.get("params", {})
            raise CdpError(
                f"chat stream loadingFailed: {fp.get('errorText')!r} "
                f"canceled={fp.get('canceled')}"
            )

        # 3) absorb DOM render lag after the bytes arrive.
        #
        # Re-assert focus emulation here: a background tab throttles painting,
        # and a client-side navigation at send time (e.g. the first message
        # turning ``/app`` into ``/app/c/<id>``) clears the focus emulation
        # that submit() set — so the reply bytes arrive (``loadingFinished``)
        # but never paint, and ready_check would spin until timeout. Asserting
        # it while we poll keeps the tab "visible" so the answer renders.
        # Best-effort: a transient CDP hiccup here must not fail the wait.
        def _keep_visible() -> None:
            # Short timeout: a best-effort visibility nudge runs on every ~0.3s
            # render poll, so it must never approach the render budget.
            try:
                self.set_focus_emulation(True, timeout=1.0)
            except CdpError:
                pass

        _keep_visible()
        if ready_check is None:
            if settle_seconds > 0:
                _time.sleep(settle_seconds)
            return
        render_deadline = min(
            _time.monotonic() + max(render_timeout, 0.0), overall_deadline
        )
        last_exc = None
        while True:
            _keep_visible()
            try:
                if ready_check():
                    return
            except Exception as exc:  # noqa: BLE001 — readiness probe is best-effort
                last_exc = exc
            if _time.monotonic() >= render_deadline:
                raise CdpError(
                    "stream finished but answer did not render within budget"
                    + (f" (last readiness error: {last_exc})" if last_exc else "")
                )
            _time.sleep(0.3)

    def close(self) -> None:
        """Disconnect the WebSocket. The user's tab stays open."""
        if self._closed:
            return
        self._closed = True
        _ws_send_close(self._sock)
        try:
            self._sock.close()
        except OSError:
            pass

    # ----- context manager sugar ----------------------------------------

    def __enter__(self) -> "PageSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

# ---------------------------------------------------------------------------
# ChromeCDPClient
# ---------------------------------------------------------------------------

class ChromeCDPClient:
    """Minimal HTTP+WS client for the local browser's remote debugging port.

    Whitelisted methods (the only ones implemented):
      - :meth:`list_pages`        → ``GET /json``
      - :meth:`attach_to_page`    → opens ``ws://.../devtools/page/<id>``
      - :meth:`get_version`       → ``GET /json/version``
      - :meth:`open_new_tab`      → ``PUT /json/new?<url>``    # Controlled exception for SessionRegistry-driven topic isolation.

    Intentionally **not** implemented (chrome-cdp invisible principle):
      - ``activate``          via ``/json/activate/<id>``      # Intentionally not implemented: violates chrome-cdp invisible principle.
      - ``close_target``      via ``/json/close/<id>``         # Intentionally not implemented: violates chrome-cdp invisible principle.
      - ``Target.createTarget`` (background or otherwise)      # Intentionally not implemented: violates chrome-cdp invisible principle.
      - ``Target.activateTarget`` / ``Target.closeTarget``     # Intentionally not implemented: violates chrome-cdp invisible principle.

    Note on the open_new_tab exception:
      The multi-llm-coordinator design needs one fresh chat tab per
      ``(topic, provider, role)`` so concurrent topics don't share chat
      context. We accept ``PUT /json/new?<url>`` as the minimum-surface way
      to do that. We still do NOT activate or close tabs from code; tab cleanup
      is manual by design. (Note: ``PUT /json/new`` itself opens in the foreground
      and takes focus once — see :meth:`open_new_tab`.)
    """

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:9222",
        *,
        http_timeout: float = 5.0,
        ws_timeout: float = 30.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._host, self._port = _validate_local_endpoint(self._endpoint)
        self._http_timeout = http_timeout
        self._ws_timeout = ws_timeout

    # ----- properties (used by tests + audit log) -----------------------

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    # ----- whitelisted HTTP methods -------------------------------------

    def _http_get_json(self, path: str) -> Any:
        url = self._endpoint + path
        try:
            with urllib.request.urlopen(url, timeout=self._http_timeout) as r:
                body = r.read().decode("utf-8")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise CdpError(
                f"HTTP GET {url} failed: {exc}"
            ) from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise CdpError(
                f"HTTP GET {url} returned non-JSON body: {exc}"
            ) from exc

    def _http_put_json(self, path: str) -> Any:
        url = self._endpoint + path
        req = urllib.request.Request(url, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=self._http_timeout) as r:
                body = r.read().decode("utf-8")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise CdpError(
                f"HTTP PUT {url} failed: {exc}"
            ) from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise CdpError(
                f"HTTP PUT {url} returned non-JSON body: {exc}"
            ) from exc

    def get_version(self) -> dict:
        """Return ``GET /json/version`` (browser brand, ws endpoint, etc.)."""
        data = self._http_get_json("/json/version")
        if not isinstance(data, dict):
            raise CdpError("/json/version did not return a JSON object")
        return data

    def list_pages(self) -> list[PageInfo]:
        """Return all entries from ``/json`` whose ``type == 'page'``."""
        data = self._http_get_json("/json")
        if not isinstance(data, list):
            raise CdpError("/json did not return a JSON list")
        out: list[PageInfo] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "page":
                continue
            out.append(
                PageInfo(
                    id=str(entry.get("id", "")),
                    type=str(entry.get("type", "")),
                    url=str(entry.get("url", "")),
                    title=str(entry.get("title", "")),
                    web_socket_debugger_url=str(
                        entry.get("webSocketDebuggerUrl", "")
                    ),
                )
            )
        return out

    def open_new_tab(self, url: str) -> PageInfo:
        """Open a new tab pointing at ``url`` via ``PUT /json/new?<url>``.

        Controlled exception to the chrome-cdp invisible principle: the multi-llm
        coordinator design isolates each ``(topic, provider, role)`` into its own
        chat tab so concurrent topics don't share context. We do not activate or
        close tabs from code.

        ⚠ Known limitation (2026-06): ``PUT /json/new`` opens the tab in the
        FOREGROUND and takes focus once — it is NOT a background open. The driver
        round-trip flow attaches to already-open tabs and never calls this, so the
        validated flow does not steal focus; the background-open fix for skill-time
        ``(topic, provider, role)`` isolation is tracked in handoff §4.3.

        Returns a :class:`PageInfo` for the new tab (id, url, ws debugger url).
        Raises :class:`CdpError` on transport or schema problems, or
        :class:`ValueError` if ``url`` is not a plausible absolute http(s) URL.
        """
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"url must be http(s); got scheme {parsed.scheme!r}")
        # Encode the target URL as a query-string VALUE: keep only the
        # scheme/host separators that ``urllib.parse.urlparse`` already
        # validated. In particular, do NOT leave ``?``, ``&``, ``=``, or
        # ``#`` unencoded — Chrome's ``/json/new`` parser treats anything
        # after the first unencoded ``?`` as separate query state, so an
        # un-encoded fragment or sub-query in the target URL silently
        # truncates the URL Chrome actually opens.
        encoded = urllib.parse.quote(url, safe=":/")
        path = "/json/new?" + encoded
        try:
            data = self._http_put_json(path)
        except CdpError as exc:
            # Older Chromium / some headed builds reject PUT on /json/new with
            # 405 Method Not Allowed and still accept GET. Fall back once.
            msg = str(exc)
            if "405" in msg or "501" in msg:
                data = self._http_get_json(path)
            else:
                raise
        if not isinstance(data, dict):
            raise CdpError("/json/new did not return a JSON object")
        page_id = str(data.get("id", ""))
        if not page_id:
            raise CdpError("/json/new response missing 'id'")
        return PageInfo(
            id=page_id,
            type=str(data.get("type", "page")),
            url=str(data.get("url", "")),
            title=str(data.get("title", "")),
            web_socket_debugger_url=str(data.get("webSocketDebuggerUrl", "")),
        )

    # ----- whitelisted WebSocket attach ---------------------------------

    def attach_to_page(self, page_id: str) -> PageSession:
        """Open a CDP WebSocket session against the given page id.

        We re-resolve the WS URL via ``/json`` (rather than hand-crafting
        ``ws://<host>:<port>/devtools/page/<id>``) so any future change in
        the browser's WS path layout doesn't silently break us.
        """
        if not isinstance(page_id, str) or not page_id:
            raise ValueError("page_id must be a non-empty string")
        for page in self.list_pages():
            if page.id == page_id:
                ws_url = page.web_socket_debugger_url
                if not ws_url:
                    raise CdpError(
                        f"page {page_id!r} has no webSocketDebuggerUrl"
                    )
                parsed = urllib.parse.urlparse(ws_url)
                if parsed.scheme not in ("ws", "wss"):
                    raise CdpError(
                        f"unexpected ws scheme {parsed.scheme!r} in {ws_url!r}"
                    )
                # Reject WS URLs whose host is non-loopback (defence in
                # depth in case ``/json`` is ever proxied).
                ws_host = (parsed.hostname or "").lower()
                if ws_host not in _ALLOWED_HOSTS:
                    raise CdpError(
                        f"WS endpoint host must be loopback, got {ws_host!r}"
                    )
                ws_port = parsed.port or self._port
                ws_path = parsed.path or "/"
                sock, leftover = _ws_handshake(
                    ws_host, ws_port, ws_path, timeout=self._ws_timeout
                )
                session = PageSession(
                    sock, leftover, page_id, recv_timeout=self._ws_timeout
                )
                # Best-effort enable Runtime + Page domains so subsequent
                # evaluate / screenshot calls don't have to do it.
                try:
                    session._send_method("Runtime.enable")
                    session._await_response(
                        session._next_id, timeout=self._ws_timeout
                    )
                    session._send_method("Page.enable")
                    session._await_response(
                        session._next_id, timeout=self._ws_timeout
                    )
                except CdpError:
                    session.close()
                    raise
                return session
        raise CdpError(f"no page with id={page_id!r} found")
