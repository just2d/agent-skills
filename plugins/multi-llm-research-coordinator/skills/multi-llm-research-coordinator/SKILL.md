---
name: multi-llm-research-coordinator
description: Fan ONE question out to the user's logged-in Gemini + GPT + Claude tabs in parallel over local Chrome (their paid subscriptions, no API), then synthesize. The agent drives this end to end — the user never runs anything. Two modes: (1) RESEARCH/选型 (convergent — "how does the industry do X / which approach is best", structured JSON + a synthesized recommendation); (2) DISCUSSION/多视角 (divergent — open or subjective topics, "let three AIs discuss/debate X / compare perspectives on X", role-decorrelated takes + a consensus/dissent synthesis). Trigger whenever the user wants several AIs to answer, research, discuss, debate, or cross-check the same question at once. Research runs a fast LIGHT scan by default (~5 min, breadth/blind-spots, unverified); say 深度/核实/verified for the DEEP fact-checked + critiqued version.
---

# multi-llm-research-coordinator

Fan **one research question** out to your logged-in **Gemini + GPT + Claude**
tabs in parallel (over local Chrome via CDP — your paid subscriptions, **no
API**), then the coordinator (this agent) synthesizes a single four-section
answer **locally** — no extra web round-trip.

> **Status — two speed modes (default = light).**
> **light** (default, ~3–5 min): fan-out → one local synthesis → **⚠未核查** deliverable.
> **deep** (opt-in, ~12–15 min): light + `run_factcheck.py` (Gemini web search +
> coordinator `WebFetch` spot-check) + `run_critic.py` (GPT-logic + Gemini-industry) +
> a **binary banner gate** (drops to **✅已核实(附来源)** only if *every* load-bearing
> fact is `已证实` w/ a working source **and** no `contradicted_fact`; else **⚠未核查**
> with the shortfall). **How to pick a mode → see 「两种速度模式」 below.** Not yet built:
> T9 decision tree + 11 interrupts (phase 3). See [Known limits](#known-limits).

This skill is for a **technically comfortable self-hoster**: you run your own
Chrome, manage three logins yourself, and are OK fixing a selector when a
vendor ships a UI change. It is **not** a turnkey product — browser automation
of three chat UIs is inherently brittle (see limits).

---

## 两种速度模式:light(默认,快) vs deep(核实,慢)

时间是瓶颈(token 免费,但每家深度联网思考要 2–5 min),所以分两档,**默认走 light**:

| 模式 | 流程 | 时间 | 交付 |
|---|---|---|---|
| **light(默认)** | 扇出 3 家 → coordinator 一次合成(保留各家视角 + 标分歧)。**不核查、不对抗** | ~3–5 min | **⚠未核查** · 扩面/去盲区的快扫 |
| **deep(opt-in)** | light + fact_checker(联网核实承重事实) + coordinator WebFetch 抽检 + critic(GPT/Gemini 对抗审) + 二元 gate | ~12–15 min | **✅已核实(附来源)** 或 ⚠带具体缺口 |

**用户怎么触发(← 这就是回答"我如何触发轻模式"):**
- **默认就是 light** —— 正常提问(「业界怎么做 X」「大家怎么看 Y」「扫一下 Z」「问问三家」)**什么都不用加**,就走 light。
- **要 deep** —— 请求里带任一关键词:**「深度 / 核实 / 帮我核实 / 选型拍板 / 要落地的决策 / verified / 严格核查」**。
- **强制 light**(怕 agent 自作主张升级)—— 说 **「轻模式 / 快速 / 快扫 / 别核查 / 只要各家观点」**。
- light 产出末尾**必附升级提示**:「要核实版就说『深度核实』,我再跑 fact_checker+critic(约 +10 min)」—— 看完快扫版随时一句话升级,**同话题不必重问**(走 follow-up / 同 `--topic-id` 续,不重跑 researcher)。

> `run_discussion.py`(发散/多视角)本身单轮、轻量,**不分** light/deep;速度档只作用于 research(`run_scenario1_core.py`)。

---

## How the agent runs this (the user never types Python)

The Python scripts below are this skill's **internal, deterministic tools** —
the agent invokes them; the user just states what they want. When triggered:

1. **Pick the mode** (two independent axes):
   - **kind**: convergent research / 选型 / "which is best" → `scripts/run_scenario1_core.py`; open/subjective / "discuss" / "debate" → `scripts/run_discussion.py` (single-round, inherently light)
   - **speed (research only)**: **light = default**; go **deep** only on a deep trigger (深度/核实/选型拍板/verified…) — see 「两种速度模式」 above. On a decision-looking question with no trigger, default light but offer the deep upgrade.
2. **Preflight** (always): `python3 scripts/preflight.py`. If it fails, relay the
   plain-language fix (e.g. "Gemini 不在 Pro,手动切") and stop — do not send.
3. **Run** the chosen script with the user's question, e.g.
   `python3 scripts/run_discussion.py --question "<user's question>"`
   (these are slow — 2–5 min/provider with thinking models; run in the background
   and report when done, don't block).
4. **Synthesize, then (deep only) verify — research mode.** You (the coordinator)
   are the glue. `run_discussion.py` is unaffected (it writes `synthesis.md` itself).
   - **a. Draft (BOTH modes).** `run_scenario1_core.py` stops after the three
     researchers and writes `drafter_input.md`. **You write `final.md` yourself**:
     four sections — 三家共识 / 各家高价值独特点(标是哪家) / 未消解分歧 / 给我的推荐.
     Local; keep the **⚠未核查** banner. **Do NOT flatten into one averaged
     consensus** — preserving each AI's view + the 分歧 is the whole point (§原则1).
   - **★ LIGHT MODE STOPS HERE** → jump to **e**. Do NOT run factcheck/critic. End
     the deliverable with the upgrade line: 「要核实版就说『深度核实』,我再跑
     fact_checker+critic(约 +10 min)」.
   - **b. Fact-check (DEEP only).** `python3 scripts/run_factcheck.py --topic-id <id>`
     — Gemini (forced live web search) verifies the load-bearing facts (auto-unioned,
     or `--facts-file`); GPT re-checks disputed. Then **you `WebFetch`-spot-check the
     most load-bearing sources** — the most de-correlated external check (`08-epistemics.md` §4).
   - **c. Critique (DEEP only).** `python3 scripts/run_critic.py --topic-id <id>` —
     GPT (logic) + Gemini (industry), independent, review your draft vs the fact
     results. Fold high-severity / `contradicted_fact` flaws into a revised draft.
   - **d. Banner gate (DEEP only, binary).** Drop to **✅已核实(附来源)** ONLY if
     *every* load-bearing fact is `已证实` w/ a working source, your spot-check
     passed, **and** no unresolved `contradicted_fact`. Else keep **⚠未核查** naming
     the shortfall. Logic: `assess_fact_results` + `final_status_header`. *Synthesizing
     is not verifying* — never drop the banner on three models merely agreeing (§3 一致≠真).
   - **e. Report (BOTH modes).** Surface `final.md` **as-is** (don't re-summarize) +
     **always** the 未消解分歧 section — disagreement is the most valuable output.

5. **Follow-up / 多轮追问** (same topic, with context): when the user wants to
   push back, narrow, or ask a *next* question on a topic already run, use
   `scripts/run_followup.py --topic-id <id> --question "..."`. It re-opens each
   provider's **original researcher conversation** (they revise/extend with full
   prior context, not from scratch), archives a new `round_followup<n>.*`, and
   refreshes `drafter_input.md` — then you synthesize `final.md` again exactly as
   in step 4. `--dry-run` reports which threads are resumable without sending;
   per-provider isolated (a dead thread is skipped, the other two still run).

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

The skill **attaches to these existing tabs** — it never logs into or switches
models for you, and the core run never opens tabs (`open_new_tab` foregrounds +
may land on the wrong model). Keep the three tabs open while it runs; everything
happens in the background, no window is raised, your Mac stays usable.
(Exception: `run_followup.py` prefers an already-open tab on the same
conversation, but will re-open a **closed/repurposed** researcher conversation
by its archived URL as a fallback — which foregrounds that one tab once.)

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

# 4) Fact-check + critique (research mode; between draft and final — see flow step 4)
python3 scripts/run_factcheck.py --topic-id my-topic              # Gemini web-search the load-bearing facts
python3 scripts/run_factcheck.py --topic-id my-topic --facts-file ./facts.txt  # coordinator-curated facts
python3 scripts/run_critic.py    --topic-id my-topic              # GPT-logic + Gemini-industry review the draft

# 5) Follow-up on an existing topic — same conversations, keeps prior context
python3 scripts/run_followup.py --topic-id my-topic --dry-run     # which threads resume?
python3 scripts/run_followup.py --topic-id my-topic --question "那 X 在 Y 约束下还成立吗？"
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
| `final.md` | the deliverable — the **coordinator's** four-section synthesis; banner is the **binary gate** (✅已核实 only if facts pass + no `contradicted_fact`, else ⚠未核查) |
| `drafter_input.md` | (research mode) assembled three answers + banner + task; the coordinator synthesizes `final.md` from it **locally** (no web round-trip). `--draft` skips this and has GPT write `final.md` in-script |
| `facts/facts_v1.json` | fact_checker verdicts: per load-bearing fact `{verdict 已证实/存疑/已证伪/查无源, source URL, note}` (+ GPT secondary re-checks) |
| `rounds/round_fact_checker.*`, `round_critic.*` | raw fact-check + critic answers; `round_followup<n>.*` for follow-up rounds |
| `draft/`, raw rounds | each model's raw JSON answer + the drafter input |
| `oq_findings.md` | run diagnostics (JSON parse rate, failures, interrupt triggers) |
| `sessions.json`, `revisit.md`, `manifest.json` | provenance / how to revisit |

---

## Known limits

| 限 | 说明 |
|---|---|
| **核查有限** | 已有 fact_checker(`run_factcheck.py`)+ critic(`run_critic.py`):承重事实经 Gemini 联网 + 协调者 `WebFetch` 抽检,critic 找 `contradicted_fact`。**但**只核查"承重事实",非承重论断仍未验证;banner 摘掉(✅已核实)≠ 全文已核,只代表那批承重事实通过了。 |
| **会漂移** | driver 跟着三家网页 UI 走，厂商改版（可能同一天）就会断。断了**先跑** `multi-llm-lib/scripts/driver_roundtrip_sanity.py` 定位，再看下面排障。 |
| **ToS + 账号风险** | 自动化驱动登录态的网页聊天属各家 ToS 灰区；风险（含封号）你自负。这是有意的取舍以复用订阅、不走 API。 |
| **每人各自登录** | 没有共享后端——每个使用者要自己的 Chrome + 三个登录。这不是多租户服务。 |
| **决策树未建** | T3/T4 facts + T5/T8 critic **已做**(见上)。仍未做:T9 决策树(只有极简二元 banner gate,没有 factual_gap→重核 / irreconcilable→打断的完整路由)、11 条打断、严格四段 *final* 契约(等多话题 OQ 数据再锁)。 |

## Troubleshooting

1. `preflight.py` 报哪条就修哪条（缺 tab / 没登录 / 模型不对，都给人话）。
2. 跑挂了、读到空答案 → **先跑** `multi-llm-lib/scripts/driver_roundtrip_sanity.py`
   做三家真机往返 sanity，定位是哪家 driver 漂了。
3. 读到空答案**不要**先归因"限流"——后台标签页是渲染节流，driver 已用焦点模拟处理；
   真限流会有 banner / 429。背景见 `multi-llm-coordinator-design/07-handoff.md` §四.2 / §六.2。
4. **Gemini 超时/读空,先想到两种新漂移**(都已在 driver 处理,2026-06):①**A/B 对比模式**——
   Gemini 有时给"哪个回答更实用?"双选项,此时**无 `<model-response>`**,旧逻辑必超时;driver 现读选项 A
   并点 `select-button` 解阻塞。②**发送按钮选择器**从 `.send-button-container` 漂到 `.send-button`。
   两者都在 lib `7407fab`+。若再漂,真机探 DOM 后改 `invisible_gemini_web_driver.py`(源在 `multi-llm-lib`,改后 `sync_lib.sh`)。
5. **fact_check 慢/超时**:承重事实多时 `run_factcheck.py` 自动分批(默认 6,`FACTCHECK_BATCH`);
   且 fact_checker **会犯相关误差**(实测 Gemini 混淆 gemini-cli / gemini-code-assist)→ coordinator 务必 `WebFetch` 抽检承重项。
6. 更深的架构与坑：`07-handoff.md`（canonical 全状态 + 警示表 §六.11/.12）。

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
- **Synthesis is coordinator-local (v0, since 2026-06-22).** `run_scenario1_core.py`
  fans out the three researchers, then stops at `drafter_input.md`; the coordinator
  writes `final.md` itself. Fusion needs no de-correlated web LLM — only the
  fact_checker/critic must be external (`08-epistemics.md` §4), and they are
  (`run_factcheck.py` / `run_critic.py`). This drops the slowest, most brittle web
  round-trip. `--draft` restores the legacy GPT-web draft for offline batch or A/B.
- **Critic = GPT + Gemini, NOT Claude (Case A).** Since the coordinator now drafts
  locally (Claude lineage), a Claude critic would be same-source as the draft →
  de-correlation lost. So GPT takes the logic slant, Gemini the industry slant;
  web Claude stays a researcher only. (Original design had Claude-logic + Gemini-
  industry, valid when GPT was the drafter — `01-scenario-1-research.md`.)
- **fact_checker = Gemini primary + GPT secondary** (Claude banned: same-source).
  Gemini does forced live web search; GPT re-checks only disputed; the coordinator
  `WebFetch`-spot-checks the top sources (the most de-correlated external check).
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
