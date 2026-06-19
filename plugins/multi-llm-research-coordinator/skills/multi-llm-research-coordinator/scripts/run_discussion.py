#!/usr/bin/env python3
"""DIVERGENT discussion mode (perspective-scan) — the divergent counterpart to
run_scenario1_core.py's convergent research. Validated prototype, 2026-06-19.

WHY this exists / the epistemics (see design `08-epistemics.md`):
  Aggregating LLM views only buys ACCURACY when their errors are decorrelated;
  but Claude/GPT/Gemini share pretraining, so errors are correlated (Kim et al.,
  ICML 2025) and "consensus != truth". The reliable payoff is therefore
  BLIND-SPOT EXPOSURE, not vote-to-truth. So we DECORRELATE on purpose:

    - ROLE DIVERSITY: three different roles, not the same prompt x3 --
        claude = prover (立论)  ·  gpt = devil's advocate (反方)  ·  gemini = pre-mortem/falsifier (证伪)
    - PROMPT INDEPENDENCE: each answers BLIND (no cross-talk -> no information cascade)
    - SINGLE ROUND: no debate rounds (debate has a built-in convergence pull /
        Degeneration-of-Thought that washes divergent topics into fake consensus)
    - LOCKED DISSENT: the aggregator (claude) emits the fixed 4-slot consolidation
        (consensus / unique-insight / UNRESOLVED-TENSION / synthesis) and is told
        NOT to collapse value conflicts, and that consensus != truth.

  NOTE same-source limitation: the aggregator (claude) is also a panelist.
  Acceptable for synthesis (MoA aggregators often are); flagged in the output.

Usage:
  python3 scripts/run_discussion.py --question "..." [--topic-id NAME] [--budget 300]
  (no --question -> the frozen 2026-06-19 example: 有钱人 vs 普通人 / 赚钱与生活)

Prereqs: Chrome :9222, the three sites logged in on target models (run preflight.py).
Output -> ~/multi-llm-archives/<topic_id>/  (role_*.md + synthesis.md)
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # vendored lib at scripts/lib

from lib.invisible_chrome_cdp_client import ChromeCDPClient
from lib.invisible_gpt_web_driver import GptWebDriver
from lib.invisible_claude_web_driver import ClaudeWebDriver
from lib.invisible_gemini_web_driver import GeminiWebDriver
from lib.invisible_driver_utils import extract_value

CDP = os.environ.get("CDP_ENDPOINT", "http://127.0.0.1:9222")
ARCHIVE_ROOT = os.environ.get("ARCHIVE_ROOT", os.path.expanduser("~/multi-llm-archives"))
GEMINI_BASE_LOAD = 4   # seconds after Page.navigate before polling for the composer

# Frozen example question (2026-06-19 validation run)
EXAMPLE = """讨论的母题:人活着的意义,尤其是「有钱人和普通人」的区别(赚钱 + 生活两面)。围绕这几个具体问题:
Q1. 「会赚钱」和「会生活」是鸡生蛋蛋生鸡——你认为先有哪个?
Q2. 有钱人的生活(养狮子、全球旅行、出入高端场所)相比普通人,本质是不是就是「体验更多未知和新鲜感」?
Q3. 有钱人赚钱主要靠什么:信息差 / 个人能力 / 意识与思考深度?
Q4. 回到「人活着的意义」:有钱与否,是放大器,还是无关?"""

# Role templates wrap the mother-topic; they are GENERIC (decorrelation mechanism).
ROLE_TEMPLATES = {
    "claude": ("立论者 prover",
        "\n\n【你的角色:立论者】对上面的问题给出你最真诚、最有结构的**正面立场和理由**。"
        "不要面面俱到、不要和稀泥——每个子问题给一个清晰的判断 + 论证。300–500 字。"),
    "gpt": ("魔鬼代言 devil's-advocate",
        "\n\n【你的角色:魔鬼代言人/反方】专门攻击上面那些「看似显然」的答案,给出**有力的反命题**"
        "(不是抬杠,是真有道理的逆向视角):质疑表层结论、质疑二分法本身、把归因从能力挪向运气/结构。"
        "给最强的反方论证。300–500 字。"),
    "gemini": ("证伪者 pre-mortem",
        "\n\n【你的角色:证伪者/前瞻性复盘】假设这个母题整体上**被框定坏了**。做 pre-mortem:"
        "这个讨论框架本身有什么**盲区**?它默认了哪些没被审视的前提(单一归因?幸存者偏差?把概念偷换?)?"
        "这问题问得对吗?指出框架盲点 + 给出一个**更值得问的问题**。300–500 字。"),
}
DISPATCH = ["gemini", "gpt", "claude"]   # gemini first (navigate + hydrate)
EXTRACT = ["gpt", "claude", "gemini"]
AGGREGATOR = "claude"


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def mk(provider):
    c = ChromeCDPClient(CDP)
    if provider == "gpt":
        d = GptWebDriver(c); return d, d.find_gpt_tab()
    if provider == "claude":
        d = ClaudeWebDriver(c); return d, d.find_claude_tab()
    if provider == "gemini":
        d = GeminiWebDriver(c); return d, d.find_gemini_tab()


def _wait_gemini_input(s, timeout=28):
    """Poll until the Gemini composer contenteditable exists after navigation.
    NOTE: the lib's inject_prompt now self-waits for the composer (SHA 1f1b552),
    so this is belt-and-suspenders — it also gates assert_pro_model below, which
    needs the page interactive before reading the model label."""
    sel = r'rich-textarea div.ql-editor[contenteditable=\"true\"]'
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if extract_value(s.runtime_evaluate(f"!!document.querySelector('{sel}')", timeout=8.0)) is True:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def dispatch(p, d, s, prompt):
    if p == "gemini":
        s._send_method("Page.navigate", {"url": "https://gemini.google.com/app"})
        time.sleep(GEMINI_BASE_LOAD)
        if not _wait_gemini_input(s, 28):
            raise RuntimeError("gemini composer not ready after navigate")
        try: d.assert_pro_model(s)
        except Exception: pass
        base = d.baseline_response_count(s)
        d.inject_prompt(s, prompt); d.submit(s); return base
    d.navigate_new_chat(s)
    base = d.baseline_response_count(s)
    d.inject_prompt(s, prompt); d.submit(s); return base


def extract(p, d, s, base, budget):
    if p == "gpt":
        d.wait_for_streaming_done(s, baseline_response_count=base, timeout_seconds=budget)
        return d.extract_last_assistant_message(s)
    if p == "claude":
        d.wait_for_streaming_done(s, baseline_count=base, timeout_seconds=budget)
        return d.extract_last_assistant_message(s)
    d.wait_for_streaming_done(s, baseline_count=base, timeout_seconds=budget)
    return d.extract_last_assistant_message(s, baseline_count=base)


def synth_prompt(question, outs):
    blocks = []
    for p, (role, _) in ROLE_TEMPLATES.items():
        body = outs.get(p, {}).get("text") or "<<无回答>>"
        blocks.append(f"【{role}({p})的发言】\n{body}")
    joined = "\n\n".join(blocks)
    return (
        "你是聚合器。下面几位带着**不同角色**(立论 / 魔鬼代言 / 证伪)独立讨论了同一母题。"
        "把它们归拢成固定四槽,**用 Markdown**:\n"
        "## 1. 共识(真正重叠的)\n"
        "## 2. 独到见解(只某一方提出、但有价值的,标注是谁)\n"
        "## 3. 未决张力(真正冲突、无法调和的)——**本段最重要,绝不许把价值观冲突抹平或假装一致;有冲突就直说**\n"
        "## 4. 综合视角(叙述式,不要强行排名或给唯一答案)\n\n"
        "**认识论约束**:这些是异构但同源(都基于大规模语料)的模型,**一致 ≠ 真**;"
        "你的产出是「视角扫描/盲区暴露」,不是「投票得到的真理」。第 3 段的分歧本身就是最有价值的输出。"
        "若发现各方共享同一盲区(都不替某个立场辩护),请在第 4 段点出。\n\n"
        f"【母题】\n{question}\n\n{joined}"
    )


def main():
    ap = argparse.ArgumentParser(description="Divergent multi-LLM discussion (perspective-scan).")
    ap.add_argument("--question", help="the discussion topic (default: frozen 2026-06-19 example)")
    ap.add_argument("--topic-id", help="archive folder under ARCHIVE_ROOT (default: auto-timestamped)")
    ap.add_argument("--budget", type=int, default=int(os.environ.get("BUDGET_S", "300")),
                    help="per-model wait budget seconds (default 300)")
    ap.add_argument("--dry-run", action="store_true", help="find tabs, send nothing")
    args = ap.parse_args()

    question = args.question or EXAMPLE
    topic_id = args.topic_id or datetime.now().strftime("%Y-%m-%d--%H%M%S--discussion")
    out = os.path.join(ARCHIVE_ROOT, topic_id)
    budget = args.budget
    os.makedirs(out, exist_ok=True)
    print(f"== role-decorrelated discussion ==  topic={topic_id}  budget={budget}s\n  out={out}")

    drv, ses, base, outs = {}, {}, {}, {}
    for p in ROLE_TEMPLATES:
        try:
            drv[p], ses[p] = mk(p); print(f"  found {p} tab")
        except Exception as e:
            outs[p] = {"err": f"setup {type(e).__name__}: {str(e)[:100]}"}; print(f"  ✗ {p} setup: {e}")

    if args.dry_run:
        print(f"[dry-run] tabs: {sorted(ses)}. STOP."); return 0

    print("\n[dispatch] (blind, parallel-thinking)...")
    for p in DISPATCH:
        if p not in ses: continue
        try:
            prompt = question + ROLE_TEMPLATES[p][1]
            t0 = time.time(); base[p] = dispatch(p, drv[p], ses[p], prompt)
            print(f"  → {p} ({ROLE_TEMPLATES[p][0]}) submitted {time.time()-t0:.0f}s")
        except Exception as e:
            outs[p] = {"err": f"dispatch {type(e).__name__}: {str(e)[:100]}"}; print(f"  ✗ {p} dispatch: {e}")

    print("\n[extract] (serial; focus-emulation renders)...")
    for p in EXTRACT:
        if p not in ses or p in outs: continue
        try:
            t0 = time.time(); txt = extract(p, drv[p], ses[p], base.get(p), budget)
            outs[p] = {"text": txt, "role": ROLE_TEMPLATES[p][0]}
            print(f"  ✓ {p} ({ROLE_TEMPLATES[p][0]}) {time.time()-t0:.0f}s, {len(txt or '')} chars")
            with open(os.path.join(out, f"role_{p}.md"), "w", encoding="utf-8") as f:
                f.write(f"# {ROLE_TEMPLATES[p][0]} ({p})\n\n{txt}\n")
        except Exception as e:
            outs[p] = {"err": f"{type(e).__name__}: {str(e)[:100]}"}; print(f"  ✗ {p}: {outs[p]['err']}")

    for s in ses.values():
        try: s.close()
        except Exception: pass

    ok = [p for p in ROLE_TEMPLATES if outs.get(p, {}).get("text")]
    print(f"\n[synthesize] panelists returned: {ok}")
    if not ok:
        print("  no panelist output; abort"); return 1
    try:
        gd = ClaudeWebDriver(ChromeCDPClient(CDP)); gs = gd.find_claude_tab()
        gd.navigate_new_chat(gs); b = gd.baseline_response_count(gs)
        gd.inject_prompt(gs, synth_prompt(question, outs)); gd.submit(gs)
        gd.wait_for_streaming_done(gs, baseline_count=b, timeout_seconds=budget)
        syn = gd.extract_last_assistant_message(gs); gs.close()
        header = (f"# 角色去相关讨论(发散模式)\n\n"
                  f"> 模式:视角扫描(非投票求真,见 design 08-epistemics)。"
                  f"角色:claude=立论 · gpt=魔鬼代言 · gemini=证伪。单轮盲答 + 锁死异见。"
                  f"聚合器=claude(同源,见限制)。panelists={ok}\n> 生成 {_now()}\n\n---\n\n")
        with open(os.path.join(out, "synthesis.md"), "w", encoding="utf-8") as f:
            f.write(header + syn + "\n")
        print(f"  ✅ synthesis saved ({len(syn)} chars)\n\n" + "=" * 56 + "\n" + syn)
    except Exception as e:
        print(f"  ✗ synthesis failed: {type(e).__name__}: {str(e)[:120]}"); return 1
    print(f"\narchive: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
