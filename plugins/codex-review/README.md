# codex-review

A Claude Code skill that runs the local **Codex CLI** as an independent code reviewer — a "second opinion" on your changes.

## Install

```
/plugin marketplace add just2d/agent-skills
/plugin install codex-review@agent-skills
```

Then just ask Claude for a "codex review" / "second opinion" / "independent review", or to re-review after applying fixes.

## Requirements

- The [Codex CLI](https://github.com/openai/codex) installed locally. The skill discovers it via `$CODEX_BIN`, then `codex` on `PATH`, then common install locations. If it can't be found, set `CODEX_BIN=/path/to/codex`.

## How it works

Two modes (see [`skills/codex-review/SKILL.md`](./skills/codex-review/SKILL.md) for full detail):

- **Session-bound (default):** a standing reviewer for the current repo, bound to the Claude session. The first review bootstraps a Codex conversation that reads `AGENTS.md`/`CLAUDE.md` and learns the codebase; later reviews resume it, so you don't re-send background and Codex remembers its earlier findings.
- **Standalone (`--standalone`):** a one-off, ephemeral review with no persistence — for tasks unrelated to the current session's main work.

Reviews are **read-only**: Codex never applies changes. You apply fixes yourself after relaying its verdict.

## License

[MIT](../../LICENSE)
