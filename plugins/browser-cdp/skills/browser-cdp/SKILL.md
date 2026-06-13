---
name: browser-cdp
description: Inspect and control the user's already-running Chrome/Chromium browser (Chrome, Helium, Edge, Brave, Arc, …) over the Chrome DevTools Protocol (CDP) on localhost:9222 — read open tabs, extract page text/HTML, screenshot, run JavaScript, click/fill forms, open or navigate tabs — all WITHOUT stealing mouse focus, changing the active tab, or raising windows. Talks straight to the browser process, not via computer-use or a Chrome-extension MCP. Trigger whenever the user refers to their browser, "my tab", "this page", "what am I looking at", mentions CDP, or asks you to read or act on a site they already have open — anywhere you'd otherwise reach for computer-use or a Chrome MCP just to read or click.
---

# Browser CDP Skill

A single Python script (`scripts/cdp.py`) that drives the user's running Chromium-based browser (Chrome, Helium, Edge, Brave, Arc, …) via the Chrome DevTools Protocol on `localhost:9222`. Pure Python stdlib — no pip dependencies. Designed to be invisible to the user — no focus stealing, no active-tab changes, no new windows popping forward.

**Always drive the browser through `scripts/cdp.py`.** Do not hand-roll your own CDP/WebSocket client or raw `Runtime.evaluate` plumbing — the CLI already encodes the focus-safety invariants below and returns structured JSON. When you need custom JavaScript, run it through the `eval`/`run` subcommands rather than opening a separate connection. (This is also the contract other skills depend on when they call this one as their browser backend.)

## When to use this skill

- "What tab am I on?" / "What's on this page?" / "Read X from my browser"
- "Fill in the form" / "Click that button" / "Submit this"
- "Open <url> in the background"
- Scraping or summarizing content from the user's actual logged-in session (their cookies are already there)
- Any time you considered using `computer-use` or `mcp__Claude_in_Chrome__*` just to read or click something — this is faster and doesn't touch the user's mouse.

## Hard rules (do not break)

1. **Never steal focus.** Do not write code that calls `Page.bringToFront`, `Target.activateTarget`, or any other API that changes the user's active tab/window. The provided CLI does not expose these — keep it that way.
2. **New tabs go to background.** Always use the `new-tab` command, which sets `background: true` on `Target.createTarget`.
3. **Don't navigate the active tab unless asked.** The user may be in the middle of something on a tab. Prefer `dump`, `text`, `eval`, or open a new background tab.
4. **Treat cookies as sensitive.** The CDP endpoint exposes the user's real logged-in sessions. Don't `eval` JS that reads `document.cookie` or auth tokens unless the user explicitly asked.

## CLI reference

All commands accept `--port` (default 9222) and `--host` (default localhost). Also honors `CDP_PORT` / `CDP_HOST` env vars. Set `CDP_WS_TIMEOUT` (seconds, default 10) to give a slow/throttled background tab longer to respond before the WebSocket times out.

### Inspecting

```bash
# List open page tabs
scripts/cdp.py list
scripts/cdp.py list --json          # machine-readable
scripts/cdp.py list --all           # include extensions, background pages, service workers

# Best for AI ingestion: title + URL + first ~20k chars of innerText
scripts/cdp.py dump <tab>
scripts/cdp.py dump <tab> --grep "Status|Version" --context 2 --compact

# Just text or HTML
scripts/cdp.py text <tab>
scripts/cdp.py html <tab>

# Structured page summary: headings, buttons, inputs, links, alerts, grep matches
scripts/cdp.py probe <tab>
scripts/cdp.py probe <tab> --grep "Pending review|Support URL" --compact

# Screenshot WITHOUT activating (PNG written to /tmp by default)
scripts/cdp.py screenshot <tab>
scripts/cdp.py screenshot <tab> --out /tmp/foo.png --full-page
```

### Interacting

```bash
# Run JS, return the value (returnByValue + awaitPromise)
scripts/cdp.py eval <tab> 'document.title'
scripts/cdp.py eval <tab> 'JSON.stringify([...document.querySelectorAll("a")].map(a=>a.href).slice(0,20))'

# Pass JS via file or stdin for non-trivial scripts (no shell-quoting pain)
scripts/cdp.py eval <tab> --file ./probe.js
cat probe.js | scripts/cdp.py eval <tab> --file -

# Click / fill — both PIERCE open shadow DOM, so the same selector works
# whether the element is in the main document or inside a Web Component.
scripts/cdp.py click <tab> 'button[data-testid="submit"]'
scripts/cdp.py type  <tab> 'textarea#prompt' 'Hello'
scripts/cdp.py type  <tab> 'input[type=range]' '4'    # range/number/etc. also work
scripts/cdp.py type  <tab> '[contenteditable]' 'rich text'

# Text/label-driven operations for pages with unstable generated selectors.
# These also pierce open shadow DOM.
scripts/cdp.py click-text <tab> 'Save draft'
scripts/cdp.py wait-text  <tab> 'Pending review'
scripts/cdp.py type-label <tab> 'Support URL' 'https://github.com/example/repo/issues'

# File uploads via CDP DOM.setFileInputFiles. This works for hidden file
# inputs too, as long as the selector resolves to input[type=file].
scripts/cdp.py upload-file <tab> 'input[type=file]' /path/a.png /path/b.png

# Wait for a selector to appear (or --gone to disappear). Canonical
# post-action verification: after click/type/navigate, wait for the next
# UI state instead of sleeping a fixed number of seconds.
scripts/cdp.py wait <tab> '[role=dialog]'              # appear within 10s
scripts/cdp.py wait <tab> 'textarea' --gone --timeout 5  # form closed

# Batch multiple actions through one CDP session. JSON can be a list or
# {"steps":[...]}. Supported actions: eval, click, type, wait, click-text,
# wait-text, type-label, upload-file, probe, dump, screenshot, navigate, sleep.
scripts/cdp.py run <tab> --file steps.json
cat steps.json | scripts/cdp.py run <tab> --file - --compact

# Navigation (all without focus steal)
scripts/cdp.py new-tab https://example.com         # background tab
scripts/cdp.py navigate <tab> https://news.com     # in-place
scripts/cdp.py close <tab>
```

### Chrome Web Store recipes

For Chrome Web Store developer console pages, prefer the dedicated compact
summaries instead of raw `dump`:

```bash
scripts/cdp.py cws-status  <tab> --compact   # status page: public/draft state, issues
scripts/cdp.py cws-package <tab> --compact   # package page: versions, permissions, issues
scripts/cdp.py cws-listing <tab> --compact   # listing page: URLs, screenshots/promo labels, fields
```

### Tab targeting

The `<tab>` argument accepts:
- An exact tab ID (`33A62271250B677890EC4C2528B286B4`) or any unique prefix
- A case-insensitive substring of the title (`gemini`, `claude`)
- A case-insensitive substring of the URL (`gmail.com`)

If multiple tabs match, the script prints them and exits — pass a more specific query.

## Practical tips

- **Verification is DOM read-back, not screenshots.** After a click/type, confirm the change by reading state back (`eval` for `.value` / `disabled` / `textContent`, or `wait` for the next UI element to appear or disappear). Screenshots are for *visual* questions — layout, rendering glitches, image content. Using a screenshot to answer "did the form submit?" is the wrong tool: it can't see `.value`, and background tabs return stale frames anyway. The skill consciously mirrors how Playwright/Puppeteer/Selenium test — assert on the DOM / accessibility tree / network, not on pixels.
- **Prefer structured text over screenshots** when you need to understand a page. Use `cws-*` for Chrome Web Store devconsole pages, `probe` for controls/fields, and `dump --grep ... --compact` for targeted text. Screenshots are only for visual/layout questions.
- **Shadow DOM is handled.** `click`, `type`, and `wait` walk open shadow roots automatically, so the same selector works whether the target is in the main document or nested inside Web Components (Chrome Web Store, YouTube, many modern apps). Closed shadow roots (`{mode:'closed'}`) are unreachable by design — the host's `.shadowRoot` is `null` and no API can pierce that. For your own `eval` scripts that need to find elements deeply, walk shadow roots the same way (`for el in root.querySelectorAll('*'): if el.shadowRoot recurse`).
- **`type` works on textarea, text-like `<input>` types (text, range, number, email, search, url, password, date, ...), and `[contenteditable]`.** It walks up the prototype chain to find the right native `value` setter (so customized built-ins still get the framework-visible setter React/Angular fire on), then dispatches `input` + `change`. **Not handled by `type`:** `<input type="checkbox">` / `radio` — use `click` to toggle them; `<input type="file">` — use `upload-file`, which calls CDP `DOM.setFileInputFiles`. For `[contenteditable]`, `type` overwrites plain `textContent`, which strips any pre-existing rich formatting; for incremental edits in rich editors, use `eval` with editor-specific APIs (`document.execCommand`, the editor's own model, etc.).
- **Background tabs are throttled.** If a JS expression hangs (`WebSocket timed out`), the tab is probably sleeping. Either ask the user to focus it, or accept that very-backgrounded tabs may not respond promptly.
- **Screenshots of long-backgrounded tabs may be stale** — Chromium returns the last rasterized frame. If freshness matters, do a quick `eval <tab> 'document.title'` first to force a paint, or `navigate` to the current URL.
- **Iframes**: `Runtime.evaluate` runs in the main frame. For cross-origin iframes, you'd need to attach to that frame's session — out of scope for the basic CLI; use `eval` with `document.querySelector('iframe').contentWindow.postMessage(...)` workarounds if needed.

## Setup (user-side, one-time)

The user must launch their Chromium-based browser with the debugging port. Use the matching app name for whatever they run:

```bash
# Helium
open -a Helium          --args --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1
# Google Chrome
open -a "Google Chrome" --args --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1
# Microsoft Edge / Brave / Arc — swap the -a name accordingly
open -a "Brave Browser" --args --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1
```

This reuses the browser's default profile (e.g. Helium's `~/Library/Application Support/net.imput.helium/`, Chrome's `~/Library/Application Support/Google/Chrome/`). To bind to a separate profile, add `--user-data-dir=/path/to/profile`. To make this the normal way the browser launches, the user can add an alias, e.g.:

```bash
alias helium='open -a Helium --args --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1'
```

Note: a browser already running *without* the debugging flag won't expose CDP — it must be fully quit and relaunched with the flag (or launched into a separate `--user-data-dir`).

## Quick health check

```bash
curl -s http://localhost:9222/json/version
```

If that returns JSON with `"Browser": "Chrome/..."`, CDP is reachable.
