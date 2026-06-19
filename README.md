# agent-skills

A collection of [Claude Code](https://claude.com/claude-code) **Agent Skills**, packaged as installable plugins through a Claude Code **plugin marketplace**.

Each skill lives in its own plugin under [`plugins/`](./plugins), so you can install only the ones you want.

## Available skills

| Plugin | What it does |
| --- | --- |
| [`codex-review`](./plugins/codex-review) | Run the local **Codex CLI** as an independent code reviewer. Session-bound by default so one Codex conversation persists across review rounds; a `--standalone` mode handles one-off reviews. |
| [`browser-cdp`](./plugins/browser-cdp) | Drive an already-running **Chrome/Chromium** browser over the **Chrome DevTools Protocol** (`localhost:9222`) — read tabs, extract text/HTML, screenshot, run JS, click/fill forms — **without stealing focus** or changing the active tab. Pure-stdlib Python CLI. |
| [`multi-llm-research-coordinator`](./plugins/multi-llm-research-coordinator) | Fan **one question** to your logged-in **Gemini + GPT + Claude** tabs in parallel over local Chrome (paid subscriptions, **no API**), then synthesize. Agent-driven end to end. Two modes: **research/选型** (convergent) and **discussion/多视角** (divergent — role-decorrelated takes + consensus/dissent). Pure-stdlib Python. |

## Install (recommended: plugin marketplace)

In Claude Code, add this repo as a marketplace once, then install the plugins you want:

```
/plugin marketplace add just2d/agent-skills
/plugin install codex-review@just2d-skills
/plugin install browser-cdp@just2d-skills
/plugin install multi-llm-research-coordinator@just2d-skills
```

> Note: the install/update suffix is the **marketplace name** — the `name` field in
> [`.claude-plugin/marketplace.json`](./.claude-plugin/marketplace.json), which is **`just2d-skills`** —
> not the repo name (`agent-skills`). The repo name is only used by `marketplace add` to clone.

Update later with:

```
/plugin marketplace update just2d-skills
```

## Install (manual)

If you'd rather not use the marketplace, copy a single skill into your personal skills directory:

```bash
git clone https://github.com/just2d/agent-skills.git
cp -r agent-skills/plugins/codex-review/skills/codex-review ~/.claude/skills/
```

Claude Code discovers any `~/.claude/skills/<name>/SKILL.md` automatically.

## Adding a new skill to this repo

1. Create `plugins/<plugin-name>/.claude-plugin/plugin.json`.
2. Put the skill under `plugins/<plugin-name>/skills/<skill-name>/SKILL.md` (plus any bundled scripts).
3. Add one entry to the `plugins` array in [`.claude-plugin/marketplace.json`](./.claude-plugin/marketplace.json).

## License

[MIT](./LICENSE)
