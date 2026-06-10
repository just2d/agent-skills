# agent-skills

A collection of [Claude Code](https://claude.com/claude-code) **Agent Skills**, packaged as installable plugins through a Claude Code **plugin marketplace**.

Each skill lives in its own plugin under [`plugins/`](./plugins), so you can install only the ones you want.

## Available skills

| Plugin | What it does |
| --- | --- |
| [`codex-review`](./plugins/codex-review) | Run the local **Codex CLI** as an independent code reviewer. Session-bound by default so one Codex conversation persists across review rounds; a `--standalone` mode handles one-off reviews. |

## Install (recommended: plugin marketplace)

In Claude Code, add this repo as a marketplace once, then install the plugins you want:

```
/plugin marketplace add just2d/agent-skills
/plugin install codex-review@agent-skills
```

Update later with:

```
/plugin marketplace update agent-skills
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
