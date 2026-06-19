#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.request

DEBUG_BASE_URL = "http://127.0.0.1:9222"
PROVIDER_PATTERNS = {
    "gemini": ["gemini.google.com"],
    "gpt": ["chatgpt.com", "chat.openai.com"],
    "claude": ["claude.ai"],
}


def fetch_tabs(debug_base_url: str = DEBUG_BASE_URL) -> list[dict]:
    with urllib.request.urlopen(f"{debug_base_url}/json/list", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def match_provider(url: str) -> str | None:
    for provider, patterns in PROVIDER_PATTERNS.items():
        if any(pattern in url for pattern in patterns):
            return provider
    return None


def main() -> None:
    tabs = fetch_tabs()
    out = []
    for tab in tabs:
        if tab.get("type") != "page":
            continue
        out.append(
            {
                "id": tab.get("id"),
                "title": tab.get("title"),
                "url": tab.get("url"),
                "provider": match_provider(tab.get("url", "")),
                "webSocketDebuggerUrl": tab.get("webSocketDebuggerUrl"),
            }
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
