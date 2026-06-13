#!/usr/bin/env python3
"""
Lightweight Chrome DevTools Protocol CLI for any Chrome/Chromium-based browser
(Chrome, Helium, Edge, Brave, Arc, …) on localhost:9222.

Design constraints:
  - NEVER steals focus. No Page.bringToFront, no Target.activateTarget.
  - New tabs created with background:true.
  - Tab queries accept exact id, id prefix, or substring of title/URL.
  - Single-file, pure Python stdlib — no pip dependencies (bundles a minimal
    WebSocket client, see _WS below).

Env: CDP_PORT / CDP_HOST override the endpoint; CDP_WS_TIMEOUT (seconds,
default 10) bounds how long to wait on a slow/throttled tab.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


# Walks open shadow roots so click/type/wait can reach elements inside Web
# Components (Chrome Web Store, YouTube, many modern apps). Closed shadow
# roots are unreachable by design — el.shadowRoot is null for those.
_QUERY_DEEP_JS = (
    "function __qd(sel){"
    "function walk(root){"
    "var hit=root.querySelector(sel); if(hit) return hit;"
    "var all=root.querySelectorAll('*');"
    "for(var i=0;i<all.length;i++){"
    "if(all[i].shadowRoot){var h=walk(all[i].shadowRoot); if(h) return h;}"
    "}"
    "return null;"
    "}"
    "return walk(document);"
    "}"
)


_DEEP_UTILS_JS = r"""
function __deepAll(root=document){
  const out=[];
  function walk(scope){
    if(!scope || !scope.querySelectorAll) return;
    const nodes=Array.from(scope.querySelectorAll('*'));
    for(const el of nodes){
      out.push(el);
      if(el.shadowRoot) walk(el.shadowRoot);
    }
  }
  walk(root);
  return out;
}
function __normText(value){
  return String(value || '').replace(/\s+/g,' ').trim();
}
function __visible(el){
  if(!el || el.nodeType !== 1) return false;
  const style=getComputedStyle(el);
  if(style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
  if(el.hidden || el.getAttribute('aria-hidden') === 'true') return false;
  const rects=el.getClientRects();
  if(!rects || rects.length === 0) return false;
  return true;
}
function __cssPath(el){
  if(!el || el.nodeType !== 1) return '';
  const tag=el.tagName.toLowerCase();
  if(el.id) return tag + '#' + el.id;
  const cls=Array.from(el.classList || []).slice(0,3).join('.');
  return tag + (cls ? '.' + cls : '');
}
function __displayText(el){
  if(!el) return '';
  const parts=[
    el.innerText,
    el.textContent,
    el.getAttribute && el.getAttribute('aria-label'),
    el.getAttribute && el.getAttribute('title'),
    el.getAttribute && el.getAttribute('placeholder'),
    el.value
  ];
  return __normText(parts.filter(Boolean).join(' '));
}
function __fieldLabel(el){
  if(!el) return '';
  const id=el.id;
  const labelledBy=el.getAttribute && el.getAttribute('aria-labelledby');
  const parts=[
    el.getAttribute && el.getAttribute('aria-label'),
    el.getAttribute && el.getAttribute('placeholder'),
    el.getAttribute && el.getAttribute('name'),
    el.getAttribute && el.getAttribute('title')
  ];
  if(labelledBy){
    for(const labelId of labelledBy.split(/\s+/)){
      const label=document.getElementById(labelId);
      if(label) parts.push(label.innerText || label.textContent);
    }
  }
  if(id){
    const labels=__deepAll().filter(x => x.tagName && x.tagName.toLowerCase() === 'label' && x.getAttribute('for') === id);
    for(const label of labels) parts.push(label.innerText || label.textContent);
  }
  const closestLabel=el.closest && el.closest('label');
  if(closestLabel) parts.push(closestLabel.innerText || closestLabel.textContent);
  return __normText(parts.filter(Boolean).join(' '));
}
function __matchesText(haystack, needle, exact=false, caseSensitive=false){
  let h=__normText(haystack);
  let n=__normText(needle);
  if(!caseSensitive){ h=h.toLowerCase(); n=n.toLowerCase(); }
  return exact ? h === n : h.includes(n);
}
function __isInteractive(el){
  if(!el || !el.matches) return false;
  return el.matches('button,a,input,textarea,select,summary,[role="button"],[role="link"],[role="menuitem"],[role="tab"],[role="checkbox"],[role="radio"],[role="option"],[contenteditable="true"]');
}
function __nearestInteractive(el){
  if(!el) return null;
  if(__isInteractive(el)) return el;
  return el.closest && el.closest('button,a,input,textarea,select,summary,[role="button"],[role="link"],[role="menuitem"],[role="tab"],[role="checkbox"],[role="radio"],[role="option"],[contenteditable="true"]');
}
function __findByText(needle, opts={}){
  const exact=!!opts.exact;
  const caseSensitive=!!opts.caseSensitive;
  const selector=opts.selector || null;
  const all=__deepAll().filter(__visible);
  const matches=all.filter(el => {
    if(selector && !(el.matches && el.matches(selector))) return false;
    return __matchesText(__displayText(el), needle, exact, caseSensitive);
  });
  const seen=new Set();
  const interactive=[];
  for(const match of matches){
    const target=__nearestInteractive(match);
    if(target && __visible(target) && !seen.has(target)){
      seen.add(target);
      interactive.push(target);
    }
  }
  const chosen=(opts.any ? (interactive[0] || matches[0]) : interactive[0]) || null;
  return {
    el: chosen,
    matchCount: matches.length,
    interactiveCount: interactive.length,
    text: chosen ? __displayText(chosen).slice(0,160) : '',
    selector: chosen ? __cssPath(chosen) : ''
  };
}
function __fieldCandidates(){
  return __deepAll().filter(el => {
    if(!el.matches) return false;
    if(el.matches('textarea,select,[contenteditable="true"]')) return true;
    if(el.matches('input') && (el.type || '').toLowerCase() !== 'hidden') return true;
    return false;
  });
}
function __findFieldByLabel(label, opts={}){
  const exact=!!opts.exact;
  const caseSensitive=!!opts.caseSensitive;
  const fields=__fieldCandidates();
  for(const field of fields){
    if(__matchesText(__fieldLabel(field), label, exact, caseSensitive)) return field;
  }
  const labels=__deepAll().filter(el => __visible(el) && __matchesText(__displayText(el), label, exact, caseSensitive));
  for(const labelEl of labels){
    if(labelEl.tagName && labelEl.tagName.toLowerCase() === 'label' && labelEl.control) return labelEl.control;
    let node=labelEl;
    for(let i=0; node && i<5; i++, node=node.parentElement){
      if(node.querySelector){
        const local=node.querySelector('textarea,select,[contenteditable="true"],input:not([type="hidden"])');
        if(local) return local;
      }
    }
  }
  const firstLabel=labels[0];
  if(firstLabel){
    const ordered=__deepAll();
    const start=ordered.indexOf(firstLabel);
    if(start >= 0){
      for(let i=start + 1; i<ordered.length; i++){
        const el=ordered[i];
        if(fields.includes(el)) return el;
      }
    }
  }
  return null;
}
function __setElementValue(el, value){
  if(!el) return {ok:false,error:'not found'};
  el.focus && el.focus();
  if(el.isContentEditable){
    el.textContent=value;
    el.dispatchEvent(new Event('input',{bubbles:true}));
    el.dispatchEvent(new Event('change',{bubbles:true}));
    return {ok:true,kind:'contenteditable'};
  }
  let desc=null;
  for(let p=Object.getPrototypeOf(el); p && !desc; p=Object.getPrototypeOf(p)){
    desc=Object.getOwnPropertyDescriptor(p,'value');
  }
  if(!desc || !desc.set) return {ok:false,error:'no value setter on ' + el.tagName};
  desc.set.call(el,value);
  el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
  return {ok:true,kind:el.tagName.toLowerCase() + (el.type ? ':' + el.type : '')};
}
"""


class _WS:
    """Minimal stdlib-only WebSocket client. Text frames; handles fragments + ping."""

    def __init__(self, url, timeout=10):
        u = urlparse(url)
        if u.scheme != "ws":
            raise ValueError(f"only ws:// supported, got {u.scheme}")
        host = u.hostname
        port = u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = b""
        self._handshake(host, port, path)

    def _handshake(self, host, port, path):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self._sock.sendall(req.encode())
        head = self._read_until(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise ConnectionError(f"WS handshake failed: {status!r}")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if expected.encode() not in head:
            raise ConnectionError("WS handshake: Sec-WebSocket-Accept mismatch")

    def _read_until(self, marker):
        while marker not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during handshake")
            self._buf += chunk
        i = self._buf.index(marker) + len(marker)
        out, self._buf = self._buf[:i], self._buf[i:]
        return out

    def _read_n(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("closed mid-frame")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self, opcode, payload=b""):
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bytes(head) + masked

    def send(self, text):
        self._sock.sendall(self._frame(0x1, text.encode("utf-8")))

    def recv(self):
        parts = []
        while True:
            b1, b2 = self._read_n(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_n(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_n(8))[0]
            mk = self._read_n(4) if masked else None
            payload = self._read_n(n) if n else b""
            if mk:
                payload = bytes(b ^ mk[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:  # ping -> pong
                self._sock.sendall(self._frame(0xA, payload))
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:
                raise ConnectionError("server sent close")
            parts.append(payload)
            if fin:
                break
        return b"".join(parts).decode("utf-8")

    def close(self):
        try:
            self._sock.sendall(self._frame(0x8))
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


DEFAULT_PORT = int(os.environ.get("CDP_PORT", "9222"))
DEFAULT_HOST = os.environ.get("CDP_HOST", "localhost")


def _http_json(path, port, host):
    url = f"http://{host}:{port}{path}"
    with urllib.request.urlopen(url, timeout=5) as r:
        body = r.read()
    return json.loads(body) if body else None


def list_tabs(port, host, include_all=False):
    tabs = _http_json("/json", port, host)
    if not include_all:
        tabs = [t for t in tabs if t.get("type") == "page"]
    return tabs


def find_tab(query, port, host):
    tabs = _http_json("/json", port, host) or []
    pages = [t for t in tabs if t.get("type") == "page"]

    for t in tabs:
        if t.get("id") == query:
            return t
    for t in tabs:
        if t.get("id", "").startswith(query) and len(query) >= 6:
            return t

    ql = query.lower()
    matches = [
        t for t in pages
        if ql in t.get("title", "").lower() or ql in t.get("url", "").lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        sys.stderr.write(f"Multiple tabs match {query!r}:\n")
        for m in matches:
            sys.stderr.write(f"  {m['id'][:8]}  {m.get('title','')[:60]}\n")
            sys.stderr.write(f"            {m.get('url','')[:80]}\n")
        sys.exit(2)
    return None


class CDPSession:
    def __init__(self, ws_url):
        timeout = float(os.environ.get("CDP_WS_TIMEOUT", "10"))
        self.ws = _WS(ws_url, timeout=timeout)
        self._id = 0

    def send(self, method, params=None):
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        self.ws.send(json.dumps(msg))
        while True:
            resp = json.loads(self.ws.recv())
            if resp.get("id") == self._id:
                if "error" in resp:
                    err = resp["error"]
                    raise RuntimeError(err.get("message", str(err)))
                return resp.get("result", {})

    def close(self):
        self.ws.close()


def _tab_or_die(query, port, host):
    tab = find_tab(query, port, host)
    if not tab:
        sys.exit(f"No tab matched: {query!r}")
    return tab


def _check_no_focus_steal_args(args):
    """Defensive: if anything ever adds focus-related args, refuse here."""
    for forbidden in ("activate", "bring_to_front", "focus"):
        if getattr(args, forbidden, False):
            sys.exit(f"Refusing: {forbidden} would steal user focus.")


def _print_json(value, compact=False):
    if compact:
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False))


def _js_exception_message(result):
    exc = result.get("exceptionDetails")
    if not exc:
        return None
    desc = exc.get("exception", {}).get("description", "")
    return f"{exc.get('text','')}\n{desc}".strip()


def _eval_result(cdp, expr, *, return_by_value=True, await_promise=True, object_group=None):
    params = {
        "expression": expr,
        "returnByValue": return_by_value,
        "awaitPromise": await_promise,
    }
    if object_group:
        params["objectGroup"] = object_group
    result = cdp.send("Runtime.evaluate", params)
    message = _js_exception_message(result)
    if message:
        sys.exit(f"JS error: {message}")
    return result


def _eval_value(cdp, expr, *, await_promise=True):
    result = _eval_result(cdp, expr, return_by_value=True, await_promise=await_promise)
    return result.get("result", {}).get("value")


def _read_js_arg(expr, file_arg, command):
    if file_arg == "-":
        return sys.stdin.read()
    if file_arg:
        try:
            return Path(file_arg).read_text()
        except OSError as e:
            sys.exit(f"{command} --file: cannot read {file_arg!r}: {e}")
    if not expr:
        sys.exit(f"{command}: pass an expression or --file PATH (or --file -)")
    return expr


def _page_text_snapshot(cdp, max_chars=None):
    limit = "null" if max_chars is None else str(max_chars)
    expr = (
        "(()=>{"
        "const text=document.body && document.body.innerText || '';"
        "return {title:document.title,url:location.href,"
        "readyState:document.readyState,"
        f"text:{limit}===null ? text : text.slice(0,{limit})"
        "};"
        "})()"
    )
    return _eval_value(cdp, expr)


def _grep_text(text, patterns, context=0, case_sensitive=False):
    if not patterns:
        return []
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled = []
    for pattern in patterns:
        try:
            compiled.append((pattern, re.compile(pattern, flags)))
        except re.error as e:
            sys.exit(f"invalid grep pattern {pattern!r}: {e}")
    lines = [line.strip() for line in text.splitlines()]
    matches = []
    seen = set()
    for index, line in enumerate(lines):
        if not line:
            continue
        for pattern, regex in compiled:
            if regex.search(line):
                start = max(0, index - context)
                end = min(len(lines), index + context + 1)
                key = (index, pattern)
                if key in seen:
                    continue
                seen.add(key)
                matches.append({
                    "line": index + 1,
                    "pattern": pattern,
                    "text": line,
                    "before": [x for x in lines[start:index] if x],
                    "after": [x for x in lines[index + 1:end] if x],
                })
    return matches


def _text_options(args):
    return {
        "exact": bool(getattr(args, "exact", False)),
        "caseSensitive": bool(getattr(args, "case_sensitive", False)),
        "selector": getattr(args, "selector", None),
        "any": bool(getattr(args, "any", False)),
    }


def _click_selector(cdp, selector):
    selector_json = json.dumps(selector)
    expr = (
        "(()=>{"
        + _QUERY_DEEP_JS +
        f"const el=__qd({selector_json});"
        "if(!el)return{ok:false,error:'not found'};"
        "el.scrollIntoView({block:'center'});"
        "el.click();"
        "return{ok:true,tag:el.tagName,text:(el.textContent||'').slice(0,80)};"
        "})()"
    )
    result = _eval_value(cdp, expr)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown"))
    return result


def _type_selector(cdp, selector, text):
    selector_json = json.dumps(selector)
    text_json = json.dumps(text)
    expr = (
        "(()=>{"
        + _DEEP_UTILS_JS
        + _QUERY_DEEP_JS +
        f"const el=__qd({selector_json});"
        "if(!el)return{ok:false,error:'not found'};"
        f"return __setElementValue(el,{text_json});"
        "})()"
    )
    result = _eval_value(cdp, expr)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown"))
    return result


def _wait_selector(cdp, selector, *, gone=False, timeout=10.0, poll=0.2):
    if timeout < 0:
        raise ValueError("--timeout must be >= 0")
    if poll <= 0:
        raise ValueError("--poll must be > 0")
    selector_json = json.dumps(selector)
    expr = (
        "(()=>{"
        + _QUERY_DEEP_JS +
        f"return !!__qd({selector_json});"
        "})()"
    )
    return _wait_eval(cdp, expr, want_present=not gone, timeout=timeout, poll=poll)


def _wait_eval(cdp, expr, *, want_present=True, timeout=10.0, poll=0.2):
    deadline = time.monotonic() + timeout
    last_error = None
    while True:
        try:
            result = cdp.send("Runtime.evaluate", {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
            })
        except RuntimeError as e:
            last_error = str(e)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timeout after {timeout}s while polling: {e}")
            time.sleep(poll)
            continue
        message = _js_exception_message(result)
        if message:
            raise RuntimeError(message)
        present = bool(result.get("result", {}).get("value"))
        if present == want_present:
            return {"ok": True, "present": present}
        if time.monotonic() >= deadline:
            state = "present" if want_present else "gone"
            suffix = f" (last error: {last_error})" if last_error else ""
            raise TimeoutError(f"timeout after {timeout}s: condition never became {state}{suffix}")
        time.sleep(poll)


def _click_text(cdp, text, opts):
    expr = (
        "(()=>{"
        + _DEEP_UTILS_JS +
        f"const found=__findByText({json.dumps(text)},{json.dumps(opts)});"
        "if(!found.el)return{ok:false,error:'not found',matchCount:found.matchCount,interactiveCount:found.interactiveCount};"
        "found.el.scrollIntoView({block:'center'});"
        "found.el.click();"
        "return{ok:true,matchCount:found.matchCount,interactiveCount:found.interactiveCount,text:found.text,selector:found.selector};"
        "})()"
    )
    result = _eval_value(cdp, expr)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown"))
    return result


def _wait_text(cdp, text, opts, *, gone=False, timeout=10.0, poll=0.2):
    expr = (
        "(()=>{"
        + _DEEP_UTILS_JS +
        f"const found=__findByText({json.dumps(text)},{json.dumps(opts)});"
        "return !!found.el;"
        "})()"
    )
    return _wait_eval(cdp, expr, want_present=not gone, timeout=timeout, poll=poll)


def _type_label(cdp, label, text, opts):
    expr = (
        "(()=>{"
        + _DEEP_UTILS_JS +
        f"const el=__findFieldByLabel({json.dumps(label)},{json.dumps(opts)});"
        f"const result=__setElementValue(el,{json.dumps(text)});"
        "return {...result,label:el ? __fieldLabel(el) : '',selector:el ? __cssPath(el) : ''};"
        "})()"
    )
    result = _eval_value(cdp, expr)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown"))
    return result


def _probe(cdp, *, max_items=60, max_text_chars=4000, grep=None, context=1, case_sensitive=False):
    expr = (
        "(()=>{"
        + _DEEP_UTILS_JS +
        f"const maxItems={int(max_items)};"
        "const all=__deepAll().filter(__visible);"
        "const item=(el)=>({tag:el.tagName.toLowerCase(),selector:__cssPath(el),"
        "text:__displayText(el).slice(0,140),role:el.getAttribute('role')||'',"
        "type:el.getAttribute('type')||'',label:__fieldLabel(el).slice(0,140)});"
        "const headings=all.filter(el=>/^H[1-6]$/.test(el.tagName)).slice(0,maxItems).map(item);"
        "const buttons=all.filter(el=>__nearestInteractive(el)===el && "
        "el.matches('button,a,input[type=\"button\"],input[type=\"submit\"],summary,[role=\"button\"],[role=\"link\"],[role=\"menuitem\"],[role=\"tab\"]'))"
        ".slice(0,maxItems).map(item);"
        "const inputs=__fieldCandidates().filter(__visible).slice(0,maxItems).map(item);"
        "const alerts=all.filter(el=>['alert','status','dialog'].includes(el.getAttribute('role'))).slice(0,maxItems).map(item);"
        "const links=all.filter(el=>el.tagName==='A' && el.href).slice(0,maxItems).map(el=>({text:__displayText(el).slice(0,140),href:el.href,selector:__cssPath(el)}));"
        "const text=document.body && document.body.innerText || '';"
        "return {title:document.title,url:location.href,readyState:document.readyState,"
        "counts:{visible:all.length,headings:headings.length,buttons:buttons.length,inputs:inputs.length,links:links.length,alerts:alerts.length},"
        "headings,buttons,inputs,links,alerts,"
        f"text:text.slice(0,{int(max_text_chars)})"
        "};"
        "})()"
    )
    data = _eval_value(cdp, expr)
    data["textMatches"] = _grep_text(data.get("text", ""), grep or [], context, case_sensitive)
    return data


def _upload_files(cdp, selector, files):
    resolved = []
    for file_name in files:
        path = Path(file_name).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(str(path))
        resolved.append(str(path))
    selector_json = json.dumps(selector)
    expr = (
        "(()=>{"
        + _QUERY_DEEP_JS +
        f"const el=__qd({selector_json});"
        "if(!el) throw new Error('not found');"
        "if(el.tagName.toLowerCase() !== 'input' || el.type !== 'file') throw new Error('matched element is not input[type=file]');"
        "return el;"
        "})()"
    )
    object_group = "cdp-upload"
    result = _eval_result(cdp, expr, return_by_value=False, object_group=object_group)
    object_id = result.get("result", {}).get("objectId")
    if not object_id:
        raise RuntimeError("file input objectId not returned")
    try:
        cdp.send("DOM.setFileInputFiles", {"objectId": object_id, "files": resolved})
        cdp.send("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": (
                "function(){"
                "this.dispatchEvent(new Event('input',{bubbles:true}));"
                "this.dispatchEvent(new Event('change',{bubbles:true}));"
                "return {ok:true,count:this.files ? this.files.length : 0};"
                "}"
            ),
            "returnByValue": True,
        })
    finally:
        try:
            cdp.send("Runtime.releaseObjectGroup", {"objectGroup": object_group})
        except Exception:
            pass
    return {"ok": True, "files": resolved}


def _cws_snapshot(cdp, kind):
    probe = _probe(cdp, max_items=120, max_text_chars=100000, grep=[
        r"\bPending review\b", r"\bPublished\b", r"\bRejected\b", r"\bDraft\b",
        r"\bReady to submit\b", r"\bVersion\b", r"\bHomepage URL\b",
        r"\bSupport URL\b", r"\bPrivacy\b", r"\bScreenshots?\b",
        r"\bSmall promo\b", r"\bMarquee\b", r"\berror\b", r"\bwarning\b",
        r"\bpermission\b",
    ], context=2)
    text = probe.get("text", "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    versions = sorted(set(re.findall(r"\b\d+(?:\.\d+){1,3}\b", text)))
    input_text = " ".join(
        " ".join(str(item.get(key, "")) for key in ("text", "label"))
        for item in probe.get("inputs", [])
    )
    urls = sorted(set(re.findall(r"https?://[^\s\"'<>]+", text + " " + input_text)))
    status = None
    for candidate, pattern in [
        ("Pending review", r"\bPending review\b|\bdraft is pending review\b"),
        ("Rejected", r"\bRejected\b"),
        ("Ready to submit", r"\bReady to submit\b"),
        ("In review", r"\bIn review\b"),
        ("Published", r"\bStatus:\s*Published\b|\bPublished\s*-\s*public\b"),
        ("Unpublished", r"\bUnpublished\b|\bdraft is unpublished\b"),
        ("Draft", r"\bDraft\b"),
    ]:
        if re.search(pattern, text, re.I):
            status = candidate
            break
    permission_lines = [
        line for line in lines
        if re.search(r"\b(permission|host_permissions|optional_host_permissions|tabs|activeTab|storage|scripting|favicon|content_scripts)\b", line, re.I)
    ][:80]
    asset_lines = [
        line for line in lines
        if re.search(r"\b(screenshots?|small promo|marquee|tile|global screenshots?|localized screenshots?)\b", line, re.I)
    ][:120]
    url_lines = [
        line for line in lines
        if re.search(r"\b(homepage url|support url|privacy policy|website|url)\b", line, re.I)
    ][:80]
    issue_lines = [
        line for line in lines
        if re.search(r"\b(error|warning|rejected|violation|missing|required|failed)\b", line, re.I)
    ][:80]
    result = {
        "kind": kind,
        "title": probe.get("title"),
        "url": probe.get("url"),
        "readyState": probe.get("readyState"),
        "status": status,
        "versions": versions,
        "urls": urls,
        "statusMatches": probe.get("textMatches", []),
        "buttons": probe.get("buttons", [])[:40],
    }
    if kind == "status":
        result.update({"issues": issue_lines})
    elif kind == "package":
        result.update({"permissionLines": permission_lines, "issues": issue_lines})
    elif kind == "listing":
        result.update({
            "urlLines": url_lines,
            "assetLines": asset_lines,
            "inputs": probe.get("inputs", [])[:80],
            "links": probe.get("links", [])[:80],
            "issues": issue_lines,
        })
    return result


def cmd_list(args):
    tabs = list_tabs(args.port, args.host, args.all)
    if args.json:
        print(json.dumps(tabs, indent=2, ensure_ascii=False))
        return
    if not tabs:
        print("(no tabs)")
        return
    for t in tabs:
        kind = t.get("type", "")
        title = t.get("title", "")
        url = t.get("url", "")
        marker = "*" if kind == "page" else " "
        print(f"{marker} {t['id'][:8]}  [{kind:14s}]  {title[:60]}")
        print(f"                              {url}")


def cmd_text(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        r = cdp.send("Runtime.evaluate", {
            "expression": "document.body && document.body.innerText || ''",
            "returnByValue": True,
        })
        print(r.get("result", {}).get("value", ""))
    finally:
        cdp.close()


def cmd_html(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        r = cdp.send("Runtime.evaluate", {
            "expression": "document.documentElement.outerHTML",
            "returnByValue": True,
        })
        print(r.get("result", {}).get("value", ""))
    finally:
        cdp.close()


def cmd_dump(args):
    """title + URL + first N chars of innerText. Best for AI ingestion."""
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        val = _page_text_snapshot(cdp, args.max_chars)
        if args.grep:
            matches = _grep_text(val.get("text", ""), args.grep, args.context, args.case_sensitive)
            if args.compact:
                _print_json({
                    "title": val.get("title"),
                    "url": val.get("url"),
                    "matches": matches,
                }, compact=True)
                return
            print(f"# {val['title']}")
            print(val["url"])
            print()
            for match in matches:
                print(f"## line {match['line']} / {match['pattern']}")
                for before in match["before"]:
                    print(before)
                print(match["text"])
                for after in match["after"]:
                    print(after)
                print()
            return
        if args.compact:
            text = re.sub(r"\s+", " ", val.get("text", "")).strip()
            _print_json({
                "title": val.get("title"),
                "url": val.get("url"),
                "readyState": val.get("readyState"),
                "text": text,
            }, compact=True)
            return
        print(f"# {val['title']}")
        print(val["url"])
        print()
        print(val["text"])
    finally:
        cdp.close()


def cmd_screenshot(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        params = {"format": "png"}
        if args.full_page:
            params["captureBeyondViewport"] = True
        r = cdp.send("Page.captureScreenshot", params)
        data = base64.b64decode(r["data"])
        out = args.out or f"/tmp/cdp-{tab['id'][:8]}.png"
        Path(out).write_bytes(data)
        print(out)
    finally:
        cdp.close()


def cmd_eval(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    expr = _read_js_arg(args.expr, args.file, "eval")
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        val = _eval_value(cdp, expr)
        if val is None:
            print("")
        elif isinstance(val, (str, int, float, bool)):
            print(val)
        else:
            print(json.dumps(val, indent=2, ensure_ascii=False))
    finally:
        cdp.close()


def cmd_click(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        result = _click_selector(cdp, args.selector)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        cdp.close()


def cmd_type(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        _type_selector(cdp, args.selector, args.text)
        print("ok")
    finally:
        cdp.close()


def cmd_wait(args):
    """Poll for a CSS selector to appear (default) or disappear (--gone).

    Canonical post-action verification: after click/type/navigate, wait for
    the next UI state instead of sleeping a fixed time. Pierces open shadow
    DOM (same query helper as click/type). Tolerates transient CDP errors
    while a page is navigating ("Execution context was destroyed") by
    treating them like a not-yet-ready state and retrying until the deadline.
    """
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        _wait_selector(cdp, args.selector, gone=args.gone, timeout=args.timeout, poll=args.poll)
        print("ok")
    finally:
        cdp.close()


def cmd_probe(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        data = _probe(
            cdp,
            max_items=args.max_items,
            max_text_chars=args.max_text_chars,
            grep=args.grep,
            context=args.context,
            case_sensitive=args.case_sensitive,
        )
        _print_json(data, compact=args.compact)
    finally:
        cdp.close()


def cmd_click_text(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        result = _click_text(cdp, args.text, _text_options(args))
        _print_json(result, compact=True)
    finally:
        cdp.close()


def cmd_wait_text(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        opts = _text_options(args)
        opts["any"] = True
        _wait_text(
            cdp,
            args.text,
            opts,
            gone=args.gone,
            timeout=args.timeout,
            poll=args.poll,
        )
        print("ok")
    finally:
        cdp.close()


def cmd_type_label(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        result = _type_label(cdp, args.label, args.text, _text_options(args))
        _print_json(result, compact=True)
    finally:
        cdp.close()


def cmd_upload_file(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        result = _upload_files(cdp, args.selector, args.files)
        _print_json(result, compact=True)
    finally:
        cdp.close()


def _load_run_steps(file_arg):
    if file_arg == "-":
        raw = sys.stdin.read()
    else:
        try:
            raw = Path(file_arg).read_text()
        except OSError as e:
            sys.exit(f"run --file: cannot read {file_arg!r}: {e}")
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"run --file: invalid JSON: {e}")
    if isinstance(spec, list):
        return spec
    if isinstance(spec, dict) and isinstance(spec.get("steps"), list):
        return spec["steps"]
    sys.exit("run --file: JSON must be a list of steps or an object with a steps list")


def _run_step(cdp, step):
    if not isinstance(step, dict):
        raise ValueError("step must be an object")
    action = step.get("action")
    if not action:
        raise ValueError("step missing action")
    if action == "eval":
        expr = step.get("expr")
        if not expr:
            raise ValueError("eval step missing expr")
        return {"value": _eval_value(cdp, expr)}
    if action == "click":
        return _click_selector(cdp, step["selector"])
    if action == "type":
        return _type_selector(cdp, step["selector"], step.get("text", ""))
    if action == "wait":
        return _wait_selector(
            cdp,
            step["selector"],
            gone=bool(step.get("gone", False)),
            timeout=float(step.get("timeout", 10.0)),
            poll=float(step.get("poll", 0.2)),
        )
    if action == "click-text":
        return _click_text(cdp, step["text"], {
            "exact": bool(step.get("exact", False)),
            "caseSensitive": bool(step.get("caseSensitive", False)),
            "selector": step.get("selector"),
            "any": bool(step.get("any", False)),
        })
    if action == "wait-text":
        return _wait_text(
            cdp,
            step["text"],
            {
                "exact": bool(step.get("exact", False)),
                "caseSensitive": bool(step.get("caseSensitive", False)),
                "selector": step.get("selector"),
                "any": bool(step.get("any", True)),
            },
            gone=bool(step.get("gone", False)),
            timeout=float(step.get("timeout", 10.0)),
            poll=float(step.get("poll", 0.2)),
        )
    if action == "type-label":
        return _type_label(cdp, step["label"], step.get("text", step.get("value", "")), {
            "exact": bool(step.get("exact", False)),
            "caseSensitive": bool(step.get("caseSensitive", False)),
        })
    if action == "upload-file":
        files = step.get("files")
        if isinstance(files, str):
            files = [files]
        if not files:
            raise ValueError("upload-file step missing files")
        return _upload_files(cdp, step["selector"], files)
    if action == "probe":
        return _probe(
            cdp,
            max_items=int(step.get("maxItems", 60)),
            max_text_chars=int(step.get("maxTextChars", 4000)),
            grep=step.get("grep") or [],
            context=int(step.get("context", 1)),
            case_sensitive=bool(step.get("caseSensitive", False)),
        )
    if action == "dump":
        return _page_text_snapshot(cdp, int(step.get("maxChars", 20000)))
    if action == "screenshot":
        params = {"format": "png"}
        if step.get("fullPage"):
            params["captureBeyondViewport"] = True
        result = cdp.send("Page.captureScreenshot", params)
        data = base64.b64decode(result["data"])
        out = step.get("out") or "/tmp/cdp-run-screenshot.png"
        Path(out).write_bytes(data)
        return {"ok": True, "out": out}
    if action == "navigate":
        result = cdp.send("Page.navigate", {"url": step["url"]})
        if result.get("errorText"):
            raise RuntimeError(result["errorText"])
        return {"ok": True}
    if action == "sleep":
        seconds = float(step.get("seconds", 0))
        if seconds < 0:
            raise ValueError("sleep seconds must be >= 0")
        time.sleep(seconds)
        return {"ok": True, "seconds": seconds}
    raise ValueError(f"unknown action: {action}")


def cmd_run(args):
    steps = _load_run_steps(args.file)
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    results = []
    try:
        for index, step in enumerate(steps):
            name = step.get("name") if isinstance(step, dict) else None
            try:
                value = _run_step(cdp, step)
                results.append({
                    "index": index,
                    "name": name,
                    "action": step.get("action") if isinstance(step, dict) else None,
                    "ok": True,
                    "result": value,
                })
            except Exception as e:
                results.append({
                    "index": index,
                    "name": name,
                    "action": step.get("action") if isinstance(step, dict) else None,
                    "ok": False,
                    "error": str(e),
                })
                _print_json({"ok": False, "results": results}, compact=args.compact)
                sys.exit(1)
        _print_json({"ok": True, "results": results}, compact=args.compact)
    finally:
        cdp.close()


def cmd_cws_status(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        _print_json(_cws_snapshot(cdp, "status"), compact=args.compact)
    finally:
        cdp.close()


def cmd_cws_package(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        _print_json(_cws_snapshot(cdp, "package"), compact=args.compact)
    finally:
        cdp.close()


def cmd_cws_listing(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        _print_json(_cws_snapshot(cdp, "listing"), compact=args.compact)
    finally:
        cdp.close()


def cmd_new_tab(args):
    version = _http_json("/json/version", args.port, args.host)
    cdp = CDPSession(version["webSocketDebuggerUrl"])
    try:
        r = cdp.send("Target.createTarget", {
            "url": args.url,
            "background": True,
        })
        print(r.get("targetId", ""))
    finally:
        cdp.close()


def cmd_navigate(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    cdp = CDPSession(tab["webSocketDebuggerUrl"])
    try:
        r = cdp.send("Page.navigate", {"url": args.url})
        if r.get("errorText"):
            sys.exit(f"navigate failed: {r['errorText']}")
        print("ok")
    finally:
        cdp.close()


def cmd_close(args):
    tab = _tab_or_die(args.tab, args.port, args.host)
    urllib.request.urlopen(
        f"http://{args.host}:{args.port}/json/close/{tab['id']}", timeout=5
    ).read()
    print("closed")


def main():
    p = argparse.ArgumentParser(
        prog="cdp",
        description="Lightweight CDP CLI for any Chrome/Chromium browser (no focus steal).",
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--host", default=DEFAULT_HOST)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="List open tabs")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--all", action="store_true",
                    help="Include extensions, background pages, service workers")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("text", help="Print visible text of a tab")
    sp.add_argument("tab")
    sp.set_defaults(func=cmd_text)

    sp = sub.add_parser("html", help="Print outerHTML of a tab")
    sp.add_argument("tab")
    sp.set_defaults(func=cmd_html)

    sp = sub.add_parser("dump", help="Print title + URL + innerText (best for AI)")
    sp.add_argument("tab")
    sp.add_argument("--max-chars", type=int, default=20000)
    sp.add_argument("--grep", action="append",
                    help="Regex to extract matching text lines; repeatable")
    sp.add_argument("--context", type=int, default=0,
                    help="Lines of context around --grep matches")
    sp.add_argument("--case-sensitive", action="store_true")
    sp.add_argument("--compact", action="store_true",
                    help="Print compact JSON instead of human text")
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser("screenshot", help="Screenshot a tab without activating it")
    sp.add_argument("tab")
    sp.add_argument("--out")
    sp.add_argument("--full-page", action="store_true")
    sp.set_defaults(func=cmd_screenshot)

    sp = sub.add_parser("eval", help="Run JS in a tab and print result")
    sp.add_argument("tab")
    sp.add_argument("expr", nargs="?",
                    help="JS expression (omit when using --file)")
    sp.add_argument("--file",
                    help="Read JS from a file (use '-' for stdin); preferred for non-trivial scripts")
    sp.set_defaults(func=cmd_eval)

    sp = sub.add_parser("click", help="Click an element by CSS selector (pierces open shadow DOM)")
    sp.add_argument("tab")
    sp.add_argument("selector")
    sp.set_defaults(func=cmd_click)

    sp = sub.add_parser("type", help="Fill input/textarea/contenteditable by CSS selector (pierces shadow DOM)")
    sp.add_argument("tab")
    sp.add_argument("selector")
    sp.add_argument("text")
    sp.set_defaults(func=cmd_type)

    sp = sub.add_parser("wait", help="Poll until selector appears (or --gone to disappear)")
    sp.add_argument("tab")
    sp.add_argument("selector")
    sp.add_argument("--gone", action="store_true",
                    help="wait until selector disappears instead of appearing")
    sp.add_argument("--timeout", type=float, default=10.0,
                    help="seconds before giving up (default 10)")
    sp.add_argument("--poll", type=float, default=0.2,
                    help="seconds between checks (default 0.2)")
    sp.set_defaults(func=cmd_wait)

    sp = sub.add_parser("probe", help="Structured page summary: headings, controls, inputs, links, alerts")
    sp.add_argument("tab")
    sp.add_argument("--max-items", type=int, default=60)
    sp.add_argument("--max-text-chars", type=int, default=4000)
    sp.add_argument("--grep", action="append",
                    help="Regex to extract matching visible text lines; repeatable")
    sp.add_argument("--context", type=int, default=1)
    sp.add_argument("--case-sensitive", action="store_true")
    sp.add_argument("--compact", action="store_true")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("click-text", help="Click the first visible interactive element matching text")
    sp.add_argument("tab")
    sp.add_argument("text")
    sp.add_argument("--exact", action="store_true")
    sp.add_argument("--case-sensitive", action="store_true")
    sp.add_argument("--selector", help="Only consider elements matching this CSS selector")
    sp.add_argument("--any", action="store_true",
                    help="Allow clicking a non-interactive visible text element")
    sp.set_defaults(func=cmd_click_text)

    sp = sub.add_parser("wait-text", help="Poll until visible text appears (or --gone to disappear)")
    sp.add_argument("tab")
    sp.add_argument("text")
    sp.add_argument("--gone", action="store_true")
    sp.add_argument("--timeout", type=float, default=10.0)
    sp.add_argument("--poll", type=float, default=0.2)
    sp.add_argument("--exact", action="store_true")
    sp.add_argument("--case-sensitive", action="store_true")
    sp.add_argument("--selector", help="Only consider elements matching this CSS selector")
    sp.add_argument("--any", action="store_true",
                    help="Allow matching a non-interactive visible text element")
    sp.set_defaults(func=cmd_wait_text)

    sp = sub.add_parser("type-label", help="Fill a field found by label/aria-label/placeholder text")
    sp.add_argument("tab")
    sp.add_argument("label")
    sp.add_argument("text")
    sp.add_argument("--exact", action="store_true")
    sp.add_argument("--case-sensitive", action="store_true")
    sp.set_defaults(func=cmd_type_label)

    sp = sub.add_parser("upload-file", help="Set files on input[type=file] via DOM.setFileInputFiles")
    sp.add_argument("tab")
    sp.add_argument("selector")
    sp.add_argument("files", nargs="+")
    sp.set_defaults(func=cmd_upload_file)

    sp = sub.add_parser("run", help="Run a JSON workflow in one CDP session")
    sp.add_argument("tab")
    sp.add_argument("--file", required=True,
                    help="JSON list of steps, or '-' for stdin")
    sp.add_argument("--compact", action="store_true")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("cws-status", help="Summarize a Chrome Web Store devconsole status page")
    sp.add_argument("tab")
    sp.add_argument("--compact", action="store_true")
    sp.set_defaults(func=cmd_cws_status)

    sp = sub.add_parser("cws-package", help="Summarize a Chrome Web Store devconsole package page")
    sp.add_argument("tab")
    sp.add_argument("--compact", action="store_true")
    sp.set_defaults(func=cmd_cws_package)

    sp = sub.add_parser("cws-listing", help="Summarize a Chrome Web Store devconsole listing page")
    sp.add_argument("tab")
    sp.add_argument("--compact", action="store_true")
    sp.set_defaults(func=cmd_cws_listing)

    sp = sub.add_parser("new-tab", help="Open URL in a background tab")
    sp.add_argument("url")
    sp.set_defaults(func=cmd_new_tab)

    sp = sub.add_parser("navigate", help="Navigate a tab to URL (no focus steal)")
    sp.add_argument("tab")
    sp.add_argument("url")
    sp.set_defaults(func=cmd_navigate)

    sp = sub.add_parser("close", help="Close a tab")
    sp.add_argument("tab")
    sp.set_defaults(func=cmd_close)

    args = p.parse_args()
    _check_no_focus_steal_args(args)
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except urllib.error.URLError as e:
        sys.exit(
            f"Cannot reach CDP at {args.host}:{args.port} — "
            f"is the browser running with --remote-debugging-port? ({e})"
        )
    except RuntimeError as e:
        sys.exit(f"CDP error: {e}")
    except (TimeoutError, socket.timeout) as e:
        sys.exit(
            f"Timed out talking to the tab ({e}). It may be backgrounded and "
            f"throttled by Chromium — ask the user to focus it, raise "
            f"CDP_WS_TIMEOUT, or retry."
        )
    except ConnectionError as e:
        sys.exit(f"CDP connection lost: {e}")
    except OSError as e:
        sys.exit(f"CDP I/O error at {args.host}:{args.port}: {e}")


if __name__ == "__main__":
    main()
