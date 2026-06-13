# browser-cdp

Drive an **already-running** Chrome/Chromium browser (Chrome, Helium, Edge, Brave, Arc, …) over the
**Chrome DevTools Protocol** on `localhost:9222` — without stealing mouse focus, switching the active
tab, or raising windows. It talks straight to the browser process, so it's faster than computer-use or
a Chrome-extension MCP for anything you just need to read or click, and it reuses the user's existing
logged-in session.

The whole skill is a single, dependency-free Python CLI (`scripts/cdp.py`, pure stdlib). The `SKILL.md`
is the trigger/declaration layer; `cdp.py` is the deterministic execution core. Other skills can call
`cdp.py` directly as their browser backend.

## What it can do

- **Inspect:** `list`, `dump` (title + URL + innerText, best for AI), `text`, `html`, `probe`
  (structured headings/buttons/inputs/links/alerts), `screenshot` (without activating the tab).
- **Interact:** `click`, `type`, `click-text`, `type-label`, `upload-file` — all pierce open shadow DOM.
- **Verify / wait:** `wait`, `wait-text` — assert the next UI state instead of sleeping.
- **Batch:** `run` — multiple actions through one CDP session from a JSON file.
- **Navigate (no focus steal):** `new-tab` (background), `navigate`, `close`.

See [`skills/browser-cdp/SKILL.md`](./skills/browser-cdp/SKILL.md) for the full command reference and rules.

## Setup (user-side, one-time)

Launch the browser with the debugging port (use the matching app name):

```bash
open -a Helium          --args --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1
open -a "Google Chrome" --args --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1
```

Health check: `curl -s http://localhost:9222/json/version` should return JSON with `"Browser": "Chrome/..."`.

A browser already running *without* the flag won't expose CDP — it must be fully quit and relaunched
(or launched into a separate `--user-data-dir`).

## Install

```
/plugin marketplace add just2d/agent-skills
/plugin install browser-cdp@agent-skills
```

Or copy the skill manually:

```bash
git clone https://github.com/just2d/agent-skills.git
cp -r agent-skills/plugins/browser-cdp/skills/browser-cdp ~/.claude/skills/
```
