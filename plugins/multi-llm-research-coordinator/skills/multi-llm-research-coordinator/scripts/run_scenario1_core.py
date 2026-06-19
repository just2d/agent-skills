#!/usr/bin/env python3
"""Scenario-1 CORE CHAIN (v0) — OQ data-collection run.

Coordinator = Claude Code (Case A). Core data flow of
01-scenario-1-research.md, stopping there on purpose (handoff §五.B):
  T2  ONE researcher prompt → gemini + gpt + claude
  T7  a single GPT drafter synthesizes (Case A: Claude is same-source, no draft)

Render strategy (revised 2026-06-17):
  Driving N hidden tabs in parallel does NOT render reliably — Chrome throttles
  paint when several background tabs render at once. But reading them ONE AT A
  TIME renders fine with focus emulation alone — no tab activation, fully
  invisible (validated: serial extract renders all three without bringing any
  tab to the foreground). So we **dispatch concurrently but extract serially**:
    Phase 1 — for all three: reset → inject → submit (returns fast; the slow
              part, model thinking, now overlaps across all three).
    Phase 2 — for each: wait (budget; fail-fast on real errors) → extract. The
              driver's set_focus_emulation (set in submit, re-asserted in the
              wait loop) renders the single tab being read; tabs are never
              activated or raised, so normal Mac use is undisturbed.
  Total ≈ slowest single think time, not the sum.

NOT built yet (designed once OQ data exists): T3/T4 facts, T5/T8 critic, T9
tree, 11 interrupts, strict four-section final. We only RECORD interrupt triggers.

Usage: python3 scripts/run_scenario1_core.py [--dry-run]
                                             [--question "..." | --question-file PATH]
                                             [--topic-id NAME]
Prereqs: Chrome :9222 with the three sites logged in, on target models.
Run `scripts/preflight.py` first to verify the environment.
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # vendored lib at scripts/lib -> `import lib.X`

from lib.invisible_chrome_cdp_client import ChromeCDPClient
from lib.invisible_gpt_web_driver import GptWebDriver
from lib.invisible_claude_web_driver import ClaudeWebDriver
from lib.invisible_gemini_web_driver import GeminiWebDriver
from lib.session_registry import SessionRegistry, SessionRecord
from lib.parse.json_extractor import extract_json_block
from lib.url_capture import read_location_href
from lib.invisible_driver_utils import extract_value
from lib import archive

CDP = os.environ.get("CDP_ENDPOINT", "http://127.0.0.1:9222")
ARCHIVE_ROOT = os.environ.get("ARCHIVE_ROOT", os.path.expanduser("~/multi-llm-archives"))
TOPIC_ID = os.environ.get("TOPIC_ID", "2026-06-17--scanning-line-agent")
COORD = "claude-code"
GEMINI_HYDRATION_S = 8
RESEARCH_BUDGET_S = int(os.environ.get("RESEARCH_BUDGET_S", "360"))  # ceiling/provider; fast-fail is on ERROR signals (drivers raise on loadingFailed), NOT on "still thinking"

PROVIDERS = ["gemini", "gpt", "claude"]   # researchers (symmetric)
DISPATCH_ORDER = ["gemini", "gpt", "claude"]   # gemini first (slowest + 8s hydrate)
EXTRACT_ORDER = ["gpt", "claude", "gemini"]    # gemini last (max think time)
DRAFTER = "gpt"
SAME_SOURCE = "claude"

# T-001 frozen question (06-future-research-topics.md) — DEFAULT when --question is omitted.
T001 = """我是程序员单干副业，想搭一个常驻扫描 agent，每天定时扫
Reddit / Product Hunt / X / 竞品更新，沉淀到一个本地知识库文件，
让我每周/每天可以快速 review。

要求：
- 复用我已有订阅（Claude Max / GPT Pro / Gemini Advanced），不用 API
- 跑在我 Mac 上，关上笔记本也能继续（云端）或睡觉时跑（本地后台）
- 输出是结构化知识库（按日/按主题），可以增量积累
- 我能 review 后筛掉噪音、把高价值信号 promote 给决策线（本项目）

业界做这个的方案有哪些？取舍是什么？最适合我的是哪种？"""

# Runtime-bound in main() from CLI (P0: parameterized delivery). dispatch()/extract()
# read RESEARCHER_PROMPT as a module global; main() rebuilds it before any send.
QUESTION = T001
RESEARCHER_PROMPT = ""

_SCHEMA_INSTRUCTION = """

---
请独立调研（可联网搜索）后给出候选方案。**只输出一个 ```json 代码块**，schema：
{
  "approaches": [
    {"name": "方案名", "how": "一句话原理", "pros": ["..."], "cons": ["..."],
     "fit": "对我这个问题/场景的适配度与理由", "sources": ["URL 或来源名"]}
  ],
  "recommended": "最推荐哪个 + 一句理由",
  "load_bearing_facts": ["结论依赖的、可被核查的关键事实"]
}
只要这个 JSON，不要额外解说。"""


def build_researcher_prompt(question: str) -> str:
    return question + _SCHEMA_INSTRUCTION


# OQ-3 dirty prefix: the Claude driver's extract converts a leading "Claude
# responded:" <h2> into "## Claude responded:" and (despite its docstring) does
# NOT strip it. Clean it SKILL-side — scripts/lib is the SHA-pinned lib copy and
# sync_lib.sh would clobber any edit there.
_CLAUDE_PREFIX_RE = re.compile(r"^\s*(?:#{1,6}\s*)?Claude responded:?\s*", re.IGNORECASE)


def clean_claude_text(text: str) -> str:
    if not text:
        return text
    return _CLAUDE_PREFIX_RE.sub("", text, count=1).lstrip()


# Researchers answer with a single ```json block. Reading that <pre><code> node's
# textContent gives the raw JSON the model actually wrote — bypassing the lib's
# htmlToMd DOM-reconstruction, which is the source of the OQ-3 Claude artifacts
# (a screen-reader-duplicate summary line + a transposed ```json fence; both are
# UI chrome htmlToMd scrapes). Scoped to the last COMPLETED assistant message so
# it can't grab the ```json schema echoed back in the user's own prompt bubble.
_LAST_CODE_BLOCK_JS = """
(() => {
  const els = document.querySelectorAll('[data-is-streaming="false"]');
  if (!els.length) return null;
  const c = els[els.length - 1];
  const codes = c.querySelectorAll('pre code');
  if (!codes.length) return null;
  return codes[codes.length - 1].textContent;
})()
"""


def read_last_code_block(session):
    """Last assistant message's last <pre><code> textContent, or None if absent."""
    try:
        val = extract_value(session.runtime_evaluate(_LAST_CODE_BLOCK_JS, timeout=15.0))
    except Exception:
        return None
    return val if isinstance(val, str) and val.strip() else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def unverified_banner(ts: str) -> str:
    """P0: every delivered `final` is prefixed with an honesty banner — the v0
    chain has NO fact_checker/critic, so its facts & sources are UNVERIFIED."""
    return (
        "> ⚠ **未核查 · v0** — 下面结论由 Gemini / GPT / Claude **各自独立**作答后机器合成，"
        "**未经 fact_checker / critic 核查**。其中「关键事实(load-bearing facts)」与"
        "「来源(sources)」**均未验证**，采用前请自行核实。\n"
        f"> _生成 {ts} · scenario-1 core-chain v0_\n\n---\n\n"
    )


def _make_driver_and_session(provider, client):
    if provider == "gpt":
        d = GptWebDriver(client); return d, d.find_gpt_tab()
    if provider == "claude":
        d = ClaudeWebDriver(client); return d, d.find_claude_tab()
    if provider == "gemini":
        d = GeminiWebDriver(client); return d, d.find_gemini_tab()
    raise ValueError(provider)


def dispatch(provider, d, s) -> dict:
    """Phase 1: reset + inject + submit. Returns fast; tab starts thinking."""
    notes = []
    if provider == "gpt":
        d.navigate_new_chat(s)
        base = d.baseline_response_count(s)
        d.inject_prompt(s, RESEARCHER_PROMPT); d.submit(s)
        return {"base": base, "notes": notes}
    if provider == "claude":
        try:
            notes.append(f"model={d.assert_target_model(s)}")
        except Exception as e:
            notes.append(f"assert_model_failed={type(e).__name__}")
        d.navigate_new_chat(s)
        base = d.baseline_response_count(s)  # capture BEFORE submit (serial-flow race)
        d.inject_prompt(s, RESEARCHER_PROMPT); d.submit(s)
        return {"base": base, "notes": notes}
    if provider == "gemini":
        s._send_method("Page.navigate", {"url": "https://gemini.google.com/app"})
        time.sleep(GEMINI_HYDRATION_S)
        try:
            d.assert_pro_model(s); notes.append("model=Pro")
        except Exception as e:
            notes.append(f"assert_pro_failed={type(e).__name__}")
        base = d.baseline_response_count(s)
        d.inject_prompt(s, RESEARCHER_PROMPT); d.submit(s)
        return {"base": base, "notes": notes}


def extract(provider, d, s, base, budget) -> str:
    """Phase 2: wait (budget) → extract. Focus emulation (set in submit,
    re-asserted in the wait loop) renders the single tab being read — no tab
    activation, so normal Mac use is undisturbed."""
    if provider == "gpt":
        d.wait_for_streaming_done(s, baseline_response_count=base, timeout_seconds=budget)
        return d.extract_last_assistant_message(s)
    if provider == "claude":
        d.wait_for_streaming_done(s, baseline_count=base, timeout_seconds=budget)
        # researcher answers are a single ```json block — read the <code> node
        # directly (raw JSON, no htmlToMd reconstruction → no OQ-3 artifacts).
        code = read_last_code_block(s)
        if code:
            return code
        return clean_claude_text(d.extract_last_assistant_message(s))  # prose fallback
    if provider == "gemini":
        d.wait_for_streaming_done(s, baseline_count=base, timeout_seconds=budget)
        return d.extract_last_assistant_message(s, baseline_count=base)


def build_drafter_prompt(outputs: dict) -> str:
    import json as _json
    blocks = []
    for prov in PROVIDERS:
        o = outputs.get(prov, {})
        if o.get("parsed") is not None:
            body = _json.dumps(o["parsed"], ensure_ascii=False, indent=2)
        else:
            body = (o.get("raw") or "<<无回答>>")[:6000]
        blocks.append(f"【{prov} 的方案】\n{body}")
    joined = "\n\n".join(blocks)
    return (
        "你是汇总者(drafter)。下面是三个 AI 针对同一调研问题各自独立给出的方案。"
        "请合成一份给用户一次看完的结论，**用 Markdown，强制四段**：\n"
        "## 1. 三家共识结论\n## 2. 高价值少数派意见（只1家提但有道理的）\n"
        "## 3. 未消解分歧（三家明显不同取舍的）\n"
        "## 4. 给我的推荐（紧扣【原始问题】里写明的约束与场景，不要泛泛而谈）\n"
        "（某段没有内容就写“无”，不要省略段落。）\n\n"
        f"【原始问题】\n{QUESTION}\n\n{joined}"
    )


def measure_oq(results, outputs, draft) -> str:
    lines = [f"# OQ findings — {TOPIC_ID}", "", f"_generated {_now_iso()}_",
             f"_render strategy: parallel dispatch + serial extract (focus-emulation only, no foregrounding); budget {RESEARCH_BUDGET_S}s_", ""]
    lines.append("## OQ-1 — JSON 输出稳定性")
    n_ok = sum(1 for p in PROVIDERS if outputs.get(p, {}).get("parsed") is not None)
    lines.append(f"- 解析成功 **{n_ok}/{len(PROVIDERS)}**")
    for p in PROVIDERS:
        o = outputs.get(p, {})
        if o.get("parsed") is not None:
            na = len(o["parsed"].get("approaches", [])) if isinstance(o["parsed"], dict) else "?"
            lines.append(f"  - {p}: ✅ parsed ({na} approaches)")
        else:
            lines.append(f"  - {p}: ❌ {o.get('parse_err', 'no response')}")
    lines.append("\n## OQ-2 — rate limit / 失败体感")
    for p in PROVIDERS:
        o = outputs.get(p, {})
        lines.append(f"  - {p}: {'ok' if o.get('ok') else 'FAIL: ' + str(o.get('err'))}  {o.get('notes')}")
    lines.append("  (串行取答 + 焦点模拟即可渲染;读到空+无报错≠真限流，真限流会有 banner/429)")
    lines.append("\n## OQ-3 — DOM 抽取脆性")
    empties = [p for p in PROVIDERS if not outputs.get(p, {}).get("ok")]
    lines.append(f"- 抽取失败/空: {empties or '无'}")
    if outputs.get("claude", {}).get("ok"):
        lines.append("  - claude `## Claude responded:` 脏前缀已在 skill 端清洗（clean_claude_text）")
    lines.append("\n## OQ-4 — 会触发哪些打断条件（只记录）")
    trig = [f"A2/A3 ({p}: {outputs[p].get('err')})" for p in PROVIDERS if not outputs.get(p, {}).get("ok")]
    if n_ok < len(PROVIDERS):
        trig.append("A3 (JSON 解析失败，正式版需 1 次重试后才打断)")
    lines.append(f"- {trig or '本轮无 A 类打断触发'}")
    lines.append("\n## OQ-5 — 同源路（claude researcher）增量价值")
    co = outputs.get(SAME_SOURCE, {})
    lines.append(f"- claude 是 coordinator 同源（Case A）。其 researcher {'有产出' if co.get('ok') else '无产出'}。")
    lines.append("  - v0 无 critic，无法完整评估增量价值；已归档供后续对比。")
    lines.append(f"\n## 草稿\n- drafter(gpt): {'✅' if draft.get('ok') else '❌ ' + str(draft.get('error'))}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Scenario-1 core-chain v0: fan one question to Gemini/GPT/Claude, GPT drafts a synthesis.")
    ap.add_argument("--dry-run", action="store_true", help="find tabs, send nothing (quota-safe)")
    ap.add_argument("--question", help="research question to fan out (default: frozen T-001)")
    ap.add_argument("--question-file", help="read the question from a file (overrides --question)")
    ap.add_argument("--topic-id", help="archive folder under ARCHIVE_ROOT (default: env TOPIC_ID; "
                                       "auto-timestamped for a custom --question)")
    args = ap.parse_args()

    global QUESTION, RESEARCHER_PROMPT, TOPIC_ID
    if args.question_file:
        QUESTION = open(os.path.expanduser(args.question_file), encoding="utf-8").read().strip()
    elif args.question:
        QUESTION = args.question
    if not QUESTION.strip():
        print("ERROR: empty question (--question / --question-file).", file=sys.stderr)
        return 2
    custom = QUESTION is not T001
    RESEARCHER_PROMPT = build_researcher_prompt(QUESTION)
    if args.topic_id:
        TOPIC_ID = args.topic_id
    elif custom:
        # never clobber the default T-001 archive with an unrelated question
        TOPIC_ID = datetime.now().strftime("%Y-%m-%d--%H%M%S--custom")

    print(f"== scenario-1 core chain (Case A) ==\n  topic={TOPIC_ID}\n"
          f"  question={'custom' if custom else 'T-001 (default)'}\n"
          f"  budget={RESEARCH_BUDGET_S}s/provider  dry_run={args.dry_run}")

    reg = SessionRegistry(ARCHIVE_ROOT, TOPIC_ID, COORD)
    archive.init_manifest(ARCHIVE_ROOT, TOPIC_ID, problem_statement=QUESTION,
                          scenario="scenario-1-core-chain-v0",
                          coordinator={"identity": COORD, "case": "A"},
                          providers={p: {"role": "researcher"} for p in PROVIDERS})
    archive.write_conversation_turn(ARCHIVE_ROOT, TOPIC_ID, speaker="coordinator",
                                    text=f"[T1] 冻结 T-001，派发给 {PROVIDERS}（并发发起 + 串行取答）。")

    # set up driver+session per provider (own client each); contain per-provider
    # setup failures so one missing/failed tab doesn't abort the whole run.
    outputs = {p: {"ok": False} for p in PROVIDERS}
    drv, ses, bases = {}, {}, {}
    for p in PROVIDERS:
        try:
            drv[p], ses[p] = _make_driver_and_session(p, ChromeCDPClient(CDP))
            print(f"  found {p} tab: {ses[p].page_id}")
        except Exception as e:
            outputs[p]["err"] = f"setup {type(e).__name__}: {str(e)[:120]}"
            print(f"  ✗ {p} setup failed: {e}")

    if args.dry_run:
        print(f"[dry-run] tabs found: {sorted(ses)}. STOP before any send.")
        return 0

    # ---- Phase 1: dispatch all three (thinking overlaps) ----
    print("\n[T2.dispatch] sending to all three (concurrent thinking)...")
    for p in DISPATCH_ORDER:
        if p not in ses:  # setup failed for this provider
            continue
        t0 = time.time()
        try:
            info = dispatch(p, drv[p], ses[p])
            bases[p] = info["base"]
            outputs[p]["notes"] = info["notes"]
            print(f"  → {p} submitted ({time.time()-t0:.1f}s) {info['notes']}")
        except Exception as e:
            outputs[p].update({"err": f"dispatch {type(e).__name__}: {str(e)[:120]}", "notes": []})
            print(f"  ✗ {p} dispatch failed: {e}")

    # ---- Phase 2: serial wait + extract (focus-emulation renders; fail-fast on errors) ----
    print("\n[T2.extract] extracting each (serial; focus-emulation renders, no foregrounding)...")
    for p in EXTRACT_ORDER:
        if "err" in outputs[p]:  # dispatch already failed
            txt = None
        else:
            t0 = time.time()
            try:
                txt = extract(p, drv[p], ses[p], bases.get(p), RESEARCH_BUDGET_S)
                url = read_location_href(ses[p])
                outputs[p]["url"] = url
                print(f"  ✓ {p} extracted ({time.time()-t0:.1f}s, {len(txt or '')} chars)")
            except Exception as e:
                txt = None
                outputs[p]["err"] = f"{type(e).__name__}: {str(e)[:120]}"
                print(f"  ✗ {p} ({time.time()-t0:.1f}s): {outputs[p]['err']}")
        archive.write_round_response(ARCHIVE_ROOT, TOPIC_ID, role="researcher", provider=p,
                                     text=(txt or f"<<NO RESPONSE: {outputs[p].get('err')}>>"))
        parsed, perr = (extract_json_block(txt) if txt else (None, "no response"))
        outputs[p].update({"raw": txt, "parsed": parsed, "parse_err": perr, "ok": bool(txt and txt.strip())})
        # seed registry record for provenance (only if the tab actually attached)
        if p in ses:
            now = _now_iso()
            reg._records[f"{p}.researcher"] = SessionRecord(
                topic_id=TOPIC_ID, provider=p, role="researcher", page_id=ses[p].page_id,
                url=outputs[p].get("url"), status="active", coordinator_identity=COORD,
                created_at=now, last_used_at=now, history=[{"at": now, "action": "researcher_run"}])
    # close researcher sessions before the drafter reuses the GPT tab (avoids a
    # second Network subscription on the same page).
    for _s in ses.values():
        try:
            _s.close()
        except Exception:
            pass
    reg.save()

    # ---- T7: GPT drafter (sequential, reuse GPT tab fresh chat) ----
    print("\n[T7] GPT drafter synthesizing...")
    draft = {"ok": False}
    try:
        gd = GptWebDriver(ChromeCDPClient(CDP)); gs = gd.find_gpt_tab()
        gd.navigate_new_chat(gs)
        base = gd.baseline_response_count(gs)
        gd.inject_prompt(gs, build_drafter_prompt(outputs)); gd.submit(gs)
        gd.wait_for_streaming_done(gs, baseline_response_count=base, timeout_seconds=360)
        text = gd.extract_last_assistant_message(gs)
        url = read_location_href(gs)
        now = _now_iso()
        reg._records["gpt.drafter"] = SessionRecord(
            topic_id=TOPIC_ID, provider="gpt", role="drafter", page_id=gs.page_id, url=url,
            status="active", coordinator_identity=COORD, created_at=now, last_used_at=now,
            history=[{"at": now, "action": "drafter_run"}])
        reg.save()
        draft = {"ok": bool(text and text.strip()), "text": text}
        if draft["ok"]:
            archive.write_draft(ARCHIVE_ROOT, TOPIC_ID, version=1, text=text)
            archive.write_final(ARCHIVE_ROOT, TOPIC_ID, unverified_banner(_now_iso()) + text)
            print(f"  ✅ draft v1 archived ({len(text)} chars; final prefixed ⚠未核查·v0)")
        else:
            print("  ❌ drafter empty")
    except Exception as e:
        draft = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        print(f"  ❌ drafter failed: {draft['error']}")

    # ---- provenance + OQ findings ----
    archive.generate_revisit(ARCHIVE_ROOT, TOPIC_ID, reg, title=f"# {TOPIC_ID} — 回看入口")
    archive.update_manifest(ARCHIVE_ROOT, TOPIC_ID,
                            round_counts={"researcher": 1, "fact_checker": 0, "drafter": 1, "critic": 0},
                            ended_at=_now_iso(),
                            final_status=("completed_with_draft" if draft.get("ok") else "completed_no_draft"))
    findings = measure_oq(None, outputs, draft)
    (archive.topic_dir(ARCHIVE_ROOT, TOPIC_ID) / "oq_findings.md").write_text(findings, encoding="utf-8")
    print("\n" + "=" * 56 + "\n  OQ-1~5 FINDINGS\n" + "=" * 56 + "\n" + findings)
    print(f"\narchive: {archive.topic_dir(ARCHIVE_ROOT, TOPIC_ID)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
