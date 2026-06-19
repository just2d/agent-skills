#!/usr/bin/env python3
from __future__ import annotations

import json
from lib.invisible_chrome_cdp_client import ChromeCDPClient
from lib.invisible_claude_web_driver import ClaudeWebDriver
from lib.invisible_gemini_web_driver import GeminiWebDriver
from lib.invisible_gpt_web_driver import GptWebDriver


def main() -> None:
    client = ChromeCDPClient('http://127.0.0.1:9222')
    out: dict[str, dict] = {}

    # Gemini
    try:
        driver = GeminiWebDriver(client)
        session = driver.find_gemini_tab()
        try:
            label = session.runtime_evaluate(
                """
                (() => {
                    const el = document.querySelector('button.input-area-switch');
                    return el ? (el.innerText || '').trim() : null;
                })()
                """,
                timeout=10.0,
            )
            out['gemini'] = {
                'ok': True,
                'page_id': session.page_id,
                'model_label': label.get('result', {}).get('value'),
            }
        finally:
            session.close()
    except Exception as exc:
        out['gemini'] = {'ok': False, 'error': str(exc)}

    # Claude
    try:
        driver = ClaudeWebDriver(client)
        session = driver.find_claude_tab()
        try:
            out['claude'] = {
                'ok': True,
                'page_id': session.page_id,
                'model_label': driver.assert_target_model(session),
            }
        finally:
            session.close()
    except Exception as exc:
        out['claude'] = {'ok': False, 'error': str(exc)}

    # GPT
    try:
        driver = GptWebDriver(client)
        session = driver.find_gpt_tab()
        try:
            out['gpt'] = {
                'ok': True,
                'page_id': session.page_id,
                'composer_lane': driver.read_composer_lane(session),
                'last_model_slug': driver.extract_last_model_slug(session),
            }
        finally:
            session.close()
    except Exception as exc:
        out['gpt'] = {'ok': False, 'error': str(exc)}

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
