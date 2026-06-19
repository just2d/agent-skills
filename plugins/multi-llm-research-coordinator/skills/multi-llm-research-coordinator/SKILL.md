---
name: multi-llm-research-coordinator
description: Fan ONE question out to the user's logged-in Gemini + GPT + Claude tabs in parallel over local Chrome (their paid subscriptions, no API), then synthesize. The agent drives this end to end — the user never runs anything. Two modes: (1) RESEARCH/选型 (convergent — "how does the industry do X / which approach is best", structured JSON + a synthesized recommendation); (2) DISCUSSION/多视角 (divergent — open or subjective topics, "let three AIs discuss/debate X / compare perspectives on X", role-decorrelated takes + a consensus/dissent synthesis). Trigger whenever the user wants several AIs to answer, research, discuss, debate, or cross-check the same question at once.
---

# multi-llm-research-coordinator

Fan **one research question** out to your logged-in **Gemini + GPT + Claude**
tabs in parallel (over local Chrome via CDP — your paid subscriptions, **no
API**), then have GPT synthesize a single four-section answer.

> **Status: v0 — core chain only, UNVERIFIED output.** There is no
> fact_checker and no critic yet, so the synthesis can carry through any wrong
> fact or hallucinated source from the three models. Every delivered `final.md`
> is prefixed with a **⚠ 未核查·v0** banner. Treat it as a fast three-way
> literature scan, **not** vetted research. See [Known limits](#known-limits).

This skill is for a **technically comfortable self-hoster**: you run your own
Chrome, manage three logins yourself, and are OK fixing a selector when a
vendor ships a UI change. It is **not** a turnkey product — browser automation
of three chat UIs is inherently brittle (see limits).

---

## How the agent runs this (the user never types Python)

The Python scripts below are this skill's **internal, deterministic tools** —
the agent invokes them; the user just states what they want. When triggered:

1. **Pick the mode** from the request:
   - convergent research / 选型 / "which is best" / "how does industry do X" → `scripts/run_scenario1_core.py`
   - open or subjective / "discuss" / "debate" / "compare perspectives" → `scripts/run_discussion.py`
2. **Preflight** (always): `python3 scripts/preflight.py`. If it fails, relay the
   plain-language fix (e.g. "Gemini 不在 Pro,手动切") and stop — do not send.
3. **Run** the chosen script with the user's question, e.g.
   `python3 scripts/run_discussion.py --question "<user's question>"`
   (these are slow — 2–5 min/provider with thinking models; run in the background
   and report when done, don't block).
4. **Verify the load-bearing facts YOURSELF — close the loop, do NOT just paste the banner.**
   The pipeline is unverified by design (no fact_checker, v0), but **you have WebSearch /
   WebFetch**. For research runs the drafter emits a `## 5. ⚠ 待核事实 (needs verification)`
   section — conflicting or single-source facts (version numbers, release dates, capability
   claims). Take those, plus any load-bearing fact the recommendation hinges on, and **verify
   them against primary sources (official docs, GitHub releases) before delivering.** Resolve the
   conflicts; do not hand "X says March, Y says May" to the user as their homework. The
   coordinator IS the fact-checker until the v1 pipeline T4 exists.
5. **Report**: deliver a result that states **what you verified and what's still uncertain.**
   Keep the ⚠未核查·v0 caveat for the three AIs' *raw* synthesis, but your delivered summary
   should be the **verified** version. Always surface the unresolved-tension / 分歧 section — the
   disagreement is the most valuable output; never flatten genuine disagreement into false
   consensus, and **down-weight single-source claims** (don't let one AI's solo suggestion
   outrank what all three converged on).

If Chrome isn't up yet, tell the user to start it (see Prerequisites) and log in —
that's the one thing only they can do.

---

## Prerequisites (do these once)

1. **Chrome with the debug port open**, using a dedicated profile:
   ```bash
   /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
     --remote-debugging-port=9222 --user-data-dir=$HOME/chrome-cdp-profile &
   ```
2. **One tab logged in per provider**, each on its target model:
   - `https://gemini.google.com/app` — model = **Pro**
   - `https://chatgpt.com/` — logged in (Thinking model recommended)
   - `https://claude.ai` — logged in, target model selected
3. **Python 3.13+**. The lib is vendored under `scripts/lib/` (SHA-pinned in
   `scripts/lib/.synced_sha`); no install step beyond a stdlib Python.

The skill **attaches to these existing tabs** — it never opens, logs into, or
switches models for you (`open_new_tab` foregrounds + may land on the wrong
model). Keep the three tabs open while it runs; everything happens in the
background, no window is raised, your Mac stays usable.

---

## Run

```bash
# 0) Always verify the environment first — fails with plain-language fixes:
python3 scripts/preflight.py

# 1) Quota-safe smoke: find + seed tabs, send nothing
python3 scripts/run_scenario1_core.py --dry-run

# 2) Real run — default frozen T-001 question
python3 scripts/run_scenario1_core.py

# 3) Real run — your own question
python3 scripts/run_scenario1_core.py --question "业界怎么做 X？取舍是什么？"
python3 scripts/run_scenario1_core.py --question-file ./my_question.md --topic-id my-topic
```

- A custom `--question` without `--topic-id` gets an auto-timestamped archive
  folder, so it never clobbers the default T-001 archive.
- A deep, web-searching, thinking-model answer legitimately takes **2–5 min per
  provider**; the run dispatches all three concurrently and reads them serially,
  so wall-clock ≈ the slowest single model, not the sum. Budget is
  `RESEARCH_BUDGET_S` (default 360s/provider).

### Output

Everything lands under `~/multi-llm-archives/<topic_id>/` (override with
`ARCHIVE_ROOT`):

| file | what |
|---|---|
| `final.md` | the deliverable — GPT's four-section synthesis, **prefixed ⚠未核查·v0** |
| `draft/`, raw rounds | each model's raw JSON answer + the drafter input |
| `oq_findings.md` | run diagnostics (JSON parse rate, failures, interrupt triggers) |
| `sessions.json`, `revisit.md`, `manifest.json` | provenance / how to revisit |

---

## Known limits

| 限 | 说明 |
|---|---|
| **无核查** | v0 没有 fact_checker / critic。`final.md` 里的事实与来源**都没验证**，采用前自己核。 |
| **会漂移** | driver 跟着三家网页 UI 走，厂商改版（可能同一天）就会断。断了**先跑** `multi-llm-lib/scripts/driver_roundtrip_sanity.py` 定位，再看下面排障。 |
| **ToS + 账号风险** | 自动化驱动登录态的网页聊天属各家 ToS 灰区；风险（含封号）你自负。这是有意的取舍以复用订阅、不走 API。 |
| **每人各自登录** | 没有共享后端——每个使用者要自己的 Chrome + 三个登录。这不是多租户服务。 |
| **未建** | T3/T4 事实抽取与核查、T5/T8 critic、T9 决策树、11 条打断、严格四段 *final* 契约都还没做（等多话题 OQ 数据再锁设计）。本轮只**记录**哪些打断条件会触发。 |

## Troubleshooting

1. `preflight.py` 报哪条就修哪条（缺 tab / 没登录 / 模型不对，都给人话）。
2. 跑挂了、读到空答案 → **先跑** `multi-llm-lib/scripts/driver_roundtrip_sanity.py`
   做三家真机往返 sanity，定位是哪家 driver 漂了。
3. 读到空答案**不要**先归因"限流"——后台标签页是渲染节流，driver 已用焦点模拟处理；
   真限流会有 banner / 429。背景见 `multi-llm-coordinator-design/07-handoff.md` §四.2 / §六.2。
4. 更深的架构与坑：`07-handoff.md`（canonical 全状态 + 警示表）。

## Design notes (for maintainers)

- **Tests / CI**: `python3 tests/test_pure.py` (stdlib, no Chrome) covers prompt
  assembly, the unverified banner, and the Claude prefix cleanup; it runs on
  every push via `.github/workflows/ci.yml`. The real driver path is **not** in
  CI (burns quota + depends on three vendor UIs) — validate it locally with
  `scripts/preflight.py` + `multi-llm-lib/scripts/driver_roundtrip_sanity.py`.
- **Claude researcher extraction** reads the answer's `<pre><code>` textContent
  directly (`read_last_code_block`) — pristine JSON, no reconstruction. Falls
  back to the lib's `htmlToMd` (prose path) if there's no code block. `htmlToMd`
  itself was hardened in lib `4c01e9f` to skip UI chrome (`sr-only` / `button` /
  `aria-hidden`) and emit fenced code with its language, so the prose path is
  clean too; the code-node read is just the simpler, side-effect-free path for JSON.
- **Case A** (coordinator = Claude Code). Researchers are symmetric (all three);
  the same-source path (Claude researcher) is archived and flagged for OQ-5.
- Lib is **SHA-pinned**; re-sync with `./sync_lib.sh` after bumping the SHA.
- Completion detection is **network-event based** (not DOM polling); GPT-5.x is
  the documented exception (action-toolbar container). See `07-handoff.md` §四.
- Full design source of truth: `multi-llm-coordinator-design/`
  (`01-scenario-1-research.md` for the intended 9-step flow;
  `08-epistemics.md` for WHY multi-LLM + the unified 4-slot consolidation protocol).
- **`scripts/run_discussion.py`** — the DIVERGENT counterpart to this convergent
  research flow (perspective-scan via role decorrelation: prover / devil's-advocate /
  pre-mortem, blind + single-round, 4-slot synthesis with dissent LOCKED). Validated
  2026-06-19. `--question "..."` runs any open topic. See `08-epistemics.md` §5–7.
