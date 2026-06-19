# multi-llm-research-coordinator

Fan **one question** out to your already-logged-in **Gemini + GPT + Claude** tabs in parallel over
local Chrome (your paid subscriptions, **no API**), then synthesize. The **agent drives it end to
end** — you state what you want in plain language and never run anything yourself.

Two modes, auto-selected from your request:

- **RESEARCH / 选型** (convergent) — "how does the industry do X / which approach is best". Each model
  returns structured JSON; GPT synthesizes a four-section recommendation.
- **DISCUSSION / 多视角** (divergent) — open or subjective topics ("let three AIs discuss / debate X /
  compare perspectives"). The three models are given **decorrelated roles** (prover / devil's-advocate
  / pre-mortem), answer blind in a single round, then an aggregator emits a fixed 4-slot synthesis
  (**consensus / unique insight / unresolved-tension / synthesis**) with the dissent slot **locked** —
  because with same-family LLMs *consensus ≠ truth*, the disagreement is the most valuable output.

The whole skill is **pure-stdlib Python** (the CDP transport is hand-rolled RFC 6455 over a socket —
no Playwright, no websocket library, no `pip install`). `SKILL.md` is the trigger/declaration layer;
`scripts/` is the deterministic execution core (it vendors `multi-llm-lib`, SHA-pinned).

## Setup (user-side, one-time per session)

This skill drives **your existing logged-in browser tabs** — it never logs in or switches models for
you. Start Chrome with the debug port on a dedicated profile, then log in to the three sites:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 --user-data-dir=$HOME/chrome-cdp-profile &
```

- `https://gemini.google.com/app` — model **Pro**
- `https://chatgpt.com/` — logged in (Thinking recommended)
- `https://claude.ai` — logged in, target model selected

That Chrome login is the **only** thing you do by hand. After that, just ask the agent.

## Use

In Claude Code, after installing this plugin, simply say what you want:

> "让三个 AI 讨论:远程工作让人更自由还是更不自由?"
> "用三家 AI 调研:本地优先笔记软件的主流方案、取舍?"

The agent picks the mode, runs `scripts/preflight.py`, runs the right script with your question (in the
background — 2–5 min/provider with thinking models), reads the result, and reports back — **always
surfacing the ⚠未核查 caveat and the dissent / 分歧 section**. Output lands under
`~/multi-llm-archives/<topic_id>/`.

See [`skills/multi-llm-research-coordinator/SKILL.md`](./skills/multi-llm-research-coordinator/SKILL.md)
for the full agent run protocol, prerequisites, output layout, and known limits.

## Known limits (v0)

- **Unverified output** — no fact_checker/critic yet; `final.md` carries a `⚠ 未核查·v0` banner. Treat
  it as a fast three-way scan, not vetted research; verify load-bearing facts yourself.
- **Drifts** — it automates three vendor chat UIs; a UI change can break a selector. If it fails, the
  agent runs `preflight.py` (bundled) for a plain-language diagnosis.
- **ToS / account risk** — automating logged-in web chats is each vendor's gray area; risk is yours.
- **Per-user logins** — no shared backend; each user needs their own Chrome + three logins.

## Maintaining

This plugin is the **single home** of the skill (the old standalone
`just2d/multi-llm-research-coordinator` repo is archived/read-only). Edit the skill
here. The driver library is **vendored** under `skills/.../scripts/lib/` and
SHA-pinned in its `.synced_sha`. After `multi-llm-lib` changes, re-vendor it:

```bash
./sync-lib.sh            # re-pin the current SHA (idempotent)
./sync-lib.sh <git-sha>  # bump to a new multi-llm-lib SHA
```

then `python3 -m py_compile skills/*/scripts/*.py` and commit + push.

> Dev source for the **library and design** stays in `just2d/multi-llm-lib`
> (driver/CDP lib) and `just2d/multi-llm-coordinator-design` (design docs `00–08`,
> handoff, real-machine sanity). This plugin is the packaged, self-contained distribution.
