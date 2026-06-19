#!/usr/bin/env python3
"""preflight.py — verify the environment BEFORE a real coordinator run (P0).

Checks, in order, and reports each in plain language:
  1. Chrome DevTools endpoint reachable (default http://127.0.0.1:9222).
  2. One tab open per provider (gemini.google.com / chatgpt.com / claude.ai).
  3. Each tab is logged in and on its target model
     (Gemini=Pro, Claude=target model label, GPT=composer model slug readable).

Exit 0 only if ALL green. Exit 1 if anything is wrong (so it can gate a run),
exit 2 if Chrome itself is unreachable. Read-only: attaches and reads the DOM,
sends nothing, switches nothing, never raises a window to the foreground.

Usage: python3 scripts/preflight.py
Env:   CDP_ENDPOINT (default http://127.0.0.1:9222)
"""
import os
import sys
from urllib.error import URLError

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # vendored lib at scripts/lib -> `import lib.X`

from lib import discover_tabs
from lib.invisible_chrome_cdp_client import ChromeCDPClient
from lib.invisible_gpt_web_driver import GptWebDriver
from lib.invisible_claude_web_driver import ClaudeWebDriver
from lib.invisible_gemini_web_driver import GeminiWebDriver

CDP = os.environ.get("CDP_ENDPOINT", "http://127.0.0.1:9222")
CDP_BASE = CDP.rstrip("/")

OPEN_HINT = {
    "gemini": "打开 https://gemini.google.com/app ，登录，并把模型切到 Pro",
    "gpt": "打开 https://chatgpt.com/ ，登录（建议选 Thinking 模型）",
    "claude": "打开 https://claude.ai ，登录，并选好目标模型",
}


def _check_model(provider: str) -> tuple[bool, str]:
    """Attach to the provider tab and read login/model state. Returns (ok, detail).

    The persistent CDP WebSocket lives on the PageSession, so we close THAT
    (ChromeCDPClient has no close()) to avoid leaking sockets across providers.
    """
    client = ChromeCDPClient(CDP)
    session = None
    try:
        if provider == "gemini":
            d = GeminiWebDriver(client)
            session = d.find_gemini_tab()
            d.assert_pro_model(session)  # raises WrongModelError if not Pro / selector gone
            return True, "模型=Pro"
        if provider == "claude":
            d = ClaudeWebDriver(client)
            session = d.find_claude_tab()
            name = d.assert_target_model(session)  # raises if model button unreadable (logged out)
            return True, f"模型={name}"
        if provider == "gpt":
            d = GptWebDriver(client)
            session = d.find_gpt_tab()
            lane = d.read_composer_lane(session)  # None if composer absent (likely logged out / not a chat page)
            if not lane:
                return False, "未读到模型槽位（可能未登录或不在对话页）"
            return True, f"模型={lane}"
        return False, f"未知 provider {provider}"
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def main() -> int:
    print(f"== preflight ==  CDP={CDP_BASE}\n")

    # ---- 1. endpoint reachable ----
    try:
        tabs = discover_tabs.fetch_tabs(CDP_BASE)
    except URLError as e:
        print(f"✗ Chrome DevTools 端口不可达：{e.reason}")
        print(f"  → 起 Chrome：/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\")
        print(f"      --remote-debugging-port=9222 --user-data-dir=$HOME/chrome-cdp-profile &")
        return 2
    except Exception as e:
        print(f"✗ 读取 {CDP_BASE}/json/list 失败：{type(e).__name__}: {e}")
        return 2
    print(f"✓ DevTools 可达（{len(tabs)} 个 target）\n")

    # ---- 2. one tab per provider ----
    present = {}  # provider -> url
    for tab in tabs:
        if tab.get("type") != "page":
            continue
        prov = discover_tabs.match_provider(tab.get("url", ""))
        if prov and prov not in present:
            present[prov] = tab.get("url")

    # ---- 3. login + model per present provider ----
    rows = []  # (provider, ok, detail)
    all_ok = True
    for prov in ("gemini", "gpt", "claude"):
        if prov not in present:
            rows.append((prov, False, f"缺 tab — {OPEN_HINT[prov]}"))
            all_ok = False
            continue
        try:
            ok, detail = _check_model(prov)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {str(e)[:140]}"
        if not ok:
            all_ok = False
        rows.append((prov, ok, detail))

    width = max(len(p) for p, _, _ in rows)
    for prov, ok, detail in rows:
        mark = "✓" if ok else "✗"
        print(f"{mark} {prov.ljust(width)}  {detail}")

    print()
    if all_ok:
        print("✅ preflight 通过 — 可以跑 run_scenario1_core.py")
        return 0
    print("❌ preflight 未通过 — 按上面每行修好再跑。")
    print("   仍崩 → 先跑 multi-llm-lib/scripts/driver_roundtrip_sanity.py 定位 driver 漂移。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
