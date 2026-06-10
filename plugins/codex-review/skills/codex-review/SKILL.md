---
name: codex-review
description: Get an independent code review from the local Codex CLI. Session-bound by default so one Codex conversation persists across review rounds (no re-sending project background, no reloading the codebase each round); a --standalone mode handles one-off reviews unrelated to the current session. Use when the user asks for a "codex review", "second opinion", "independent review", or to re-review after applying fixes.
---

# codex-review

Runs the local **Codex CLI** as a reviewer. Two modes:

- **Session-bound (default):** a *standing reviewer* for the current repo, bound
  to this Claude session (`CLAUDE_CODE_SESSION_ID` + repo path). The first review
  **bootstraps** a Codex conversation (Codex reads `AGENTS.md`/`CLAUDE.md` once
  and learns the codebase); later reviews **resume** it. So you don't re-send the
  background, and Codex remembers the code and its earlier findings — later rounds
  are cheaper and catch "did you fix what I flagged?".
- **Standalone (`--standalone`):** a one-off, ephemeral review with **no** session
  binding and **no** persistence. Use it when the review task is **unrelated to
  the current session's main work** — e.g. reviewing a different project, or this
  skill itself. Don't pollute the session thread with off-topic context.

## How to run

Run the bundled `review.sh` from this skill's own directory (the path below is
the default Claude Code install location; if the skill lives elsewhere, resolve
`review.sh` relative to wherever this SKILL.md is):

```bash
bash ~/.claude/skills/codex-review/review.sh "<optional focus>"
```

- First session-bound call → bootstraps + stores the thread id under
  `~/.cache/claude-codex-review/<key>.thread`.
- Later session-bound calls → resume that thread (run again after applying fixes;
  Codex re-reviews knowing what it said before).
- `--standalone` → independent one-off; nothing stored.
- `--new` → force a fresh session-bound thread (after a major context shift).
- `--base <ref>` → review the branch vs `<ref>` instead of uncommitted changes.

Examples:
```bash
bash ~/.claude/skills/codex-review/review.sh
bash ~/.claude/skills/codex-review/review.sh "focus on the capture invariant"
bash ~/.claude/skills/codex-review/review.sh --base main
bash ~/.claude/skills/codex-review/review.sh --standalone "review this skill for portability bugs"
```

## Picking the mode

If the thing being reviewed is part of the work you're doing in **this** Claude
session and **this** repo → default (session-bound). If it's a tangent / a
different repo / the skill itself → `--standalone`. When unsure, prefer
`--standalone` (a wrong resume pollutes the session thread with off-topic
context; a wrong standalone just costs one cold start).

## Portability & versioning

- **No hardcoded codex path.** Discovery order: `$CODEX_BIN` → `codex` on PATH →
  common install locations (`~/.codex/.../current`, homebrew, `/usr/local/bin`,
  macOS app). On a new environment, if it's not found, set `CODEX_BIN` and re-run.
  (The skill is safe to host/share across machines.)
- Depends only on Codex's stable `exec`/`exec resume` interface; the one schema
  assumption (the session id in `--json`) is parsed leniently, and a failed
  `resume` falls back to a fresh bootstrap — a Codex upgrade won't hard-break it.
- Works in a non-git directory too (scope becomes "read the relevant files").

## Notes

- Reviews the **current working tree** (Codex runs `git diff` itself under a
  read-only sandbox), so apply edits first, then review.
- Output: a `[codex-review] mode=... thread=... codex=...` header, then Codex's
  final message ending in `**Verdict: GO**` / `**Verdict: NO-GO**`. Relay the
  verdict + findings; decide with the user whether to apply nits.
- Codex runs take a few minutes. In Claude Code, launch with `run_in_background`;
  outside it, background the call however your shell does (e.g. `&` + `wait`). The
  script always redirects stdin (`< /dev/null`); a bare `codex exec` with no
  stdin hangs.
- **Review-only** (read-only sandbox). Never let Codex auto-apply changes or
  publish; you apply fixes yourself after relaying findings.
