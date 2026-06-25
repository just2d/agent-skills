#!/usr/bin/env python3
"""run_factcheck.py — T3/T4 fact_checker for an existing topic.

Verify the **load-bearing facts** the researchers' conclusions rest on, against
EXTERNAL sources (live web search) — the one step that actually buys "accuracy"
per 08-epistemics.md §4: an internal LLM opinion can't remove shared blind
spots, so the checker must reach OUTSIDE the LLM set.

Roles (Case A: coordinator = Claude Code = Claude lineage → Claude is BANNED here):
  - fact_checker PRIMARY  = Gemini (user trusts its retrieval; forced web search)
  - fact_checker SECONDARY= GPT, re-checks ONLY the disputed (存疑/已证伪/查无源) —
    de-correlates the checker itself without re-running everything.
The coordinator (Claude Code) then spot-checks the top facts with its OWN
WebFetch — the most de-correlated external check available (see SKILL.md).

Facts come from the researchers' self-reported `load_bearing_facts` (unioned
from the archived round_researcher.*.md), or a coordinator-curated --facts-file
(one claim per line). Output → facts/facts_v1.json + a printed summary with the
binary verdict (all 已证实 → eligible to drop the banner).

Usage:
  python3 scripts/run_factcheck.py --topic-id <id>
  python3 scripts/run_factcheck.py --topic-id <id> --facts-file ./facts.txt
  python3 scripts/run_factcheck.py --topic-id <id> --no-secondary   # skip GPT re-check
  python3 scripts/run_factcheck.py --topic-id <id> --dry-run        # show facts, send nothing
"""
import argparse
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lib.invisible_chrome_cdp_client import ChromeCDPClient
from lib.session_registry import SessionRegistry, SessionRecord
from lib.parse.json_extractor import extract_json_block
from lib import archive
from run_scenario1_core import ask_fresh, assess_fact_results

ARCHIVE_ROOT = os.environ.get("ARCHIVE_ROOT", os.path.expanduser("~/multi-llm-archives"))
CDP = os.environ.get("CDP_ENDPOINT", "http://127.0.0.1:9222")
COORD = "claude-code"
RESEARCHERS = ["gemini", "gpt", "claude"]
PRIMARY = "gemini"      # Case A fact_checker primary
SECONDARY = "gpt"       # Case A fact_checker secondary (Claude banned: same-source)
DISPUTED = {"存疑", "已证伪", "查无源"}
# A single Gemini pass over ~19 web-searched facts blew the 360s budget (the
# answer never finished rendering). Batch so each pass stays well inside budget.
BATCH_SIZE = int(os.environ.get("FACTCHECK_BATCH", "6"))


def _chunk(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), max(1, n))]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def collect_facts_from_archive(topic_dir, providers) -> list:
    """Union the researchers' self-reported load_bearing_facts (dedup, order-stable)."""
    rounds = topic_dir / "rounds"
    facts, seen = [], set()
    for p in providers:
        f = rounds / f"round_researcher.{p}.md"
        if not f.exists():
            continue
        parsed, _ = extract_json_block(f.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            for fact in (parsed.get("load_bearing_facts") or []):
                claim = str(fact).strip()
                key = " ".join(claim.split())[:120].lower()
                if key and key not in seen:
                    seen.add(key)
                    facts.append(claim)
    return facts


def build_factcheck_prompt(facts: list) -> str:
    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(facts))
    return (
        "你是事实核查员(fact_checker)。下面是一份「承重事实」清单——某调研结论依赖它们成立。\n"
        "请**逐条联网搜索**核查,**禁止凭记忆**作答;找不到可靠来源就老实标“查无源”,不要猜。\n"
        "**只输出一个 ```json 代码块**,schema:\n"
        '{"facts":[{"id":1,"claim":"<照抄原文>","verdict":"已证实|存疑|已证伪|查无源",'
        '"source":"<一手来源URL;查无源留空>","note":"<一句话依据>"}]}\n'
        "判定:已证实=可信一手来源明确支持;已证伪=来源明确反驳;存疑=来源冲突或证据不足;"
        "查无源=搜不到可靠来源。只要这个 JSON,不要额外解说。\n\n【承重事实清单】\n" + numbered
    )


def build_secondary_prompt(disputed: list) -> str:
    numbered = "\n".join(
        f"{i + 1}. {d.get('claim','')}（前一轮判定：{d.get('verdict','?')}）"
        for i, d in enumerate(disputed)
    )
    return (
        "你是复核员。另一个 AI 把下面这些事实标为 存疑/已证伪/查无源。请你**独立联网复核**,"
        "**禁止凭记忆**。**只输出一个 ```json**,schema 同上:\n"
        '{"facts":[{"id":1,"claim":"...","verdict":"已证实|存疑|已证伪|查无源","source":"...","note":"..."}]}\n'
        "若你能找到可靠一手来源支持/反驳,请明确给出并改判;否则维持。\n\n【待复核】\n" + numbered
    )


def _parse_facts(text):
    parsed, err = extract_json_block(text or "")
    if isinstance(parsed, dict) and isinstance(parsed.get("facts"), list):
        return parsed["facts"], None
    return None, (err or "no facts[] in JSON")


def _seed(reg, provider, role):
    now = _now_iso()
    reg._records[f"{provider}.{role}"] = SessionRecord(
        topic_id=reg.topic_id, provider=provider, role=role, page_id="",
        status="active", coordinator_identity=COORD, created_at=now, last_used_at=now,
        history=[{"at": now, "action": f"{role}_run"}])


def main() -> int:
    ap = argparse.ArgumentParser(description="Fact-check a topic's load-bearing facts against live web sources.")
    ap.add_argument("--topic-id", required=True)
    ap.add_argument("--facts-file", help="coordinator-curated facts, one claim per line (else auto-union from researchers)")
    ap.add_argument("--no-secondary", action="store_true", help="skip the GPT re-check of disputed facts")
    ap.add_argument("--dry-run", action="store_true", help="print the facts list, send nothing")
    args = ap.parse_args()

    topic_id = args.topic_id
    tdir = archive.topic_dir(ARCHIVE_ROOT, topic_id)
    if not tdir.exists():
        print(f"ERROR: no archive for topic {topic_id!r} under {ARCHIVE_ROOT}", file=sys.stderr)
        return 2

    if args.facts_file:
        facts = [ln.strip() for ln in open(os.path.expanduser(args.facts_file), encoding="utf-8")
                 if ln.strip() and not ln.lstrip().startswith("#")]
    else:
        facts = collect_facts_from_archive(tdir, RESEARCHERS)
    if not facts:
        print("ERROR: no load-bearing facts found (researchers had none, or pass --facts-file).", file=sys.stderr)
        return 2

    print(f"== fact_check on {topic_id} ==  {len(facts)} load-bearing facts")
    for i, c in enumerate(facts):
        print(f"  {i + 1}. {c[:90]}")
    if args.dry_run:
        print("[dry-run] STOP before any send.")
        return 0

    reg = SessionRegistry.load(ARCHIVE_ROOT, topic_id, COORD)

    # ---- PRIMARY: Gemini, forced web search (batched: ~19 facts in one pass is slow/risky) ----
    batches = _chunk(facts, BATCH_SIZE)
    print(f"\n[fact_check primary={PRIMARY}] {len(facts)} facts in {len(batches)} batch(es) of ≤{BATCH_SIZE}, forced web search...")
    checked, raw_parts = [], []
    for bi, batch in enumerate(batches, 1):
        try:
            text = ask_fresh(PRIMARY, ChromeCDPClient(CDP), build_factcheck_prompt(batch))
        except Exception as e:  # noqa: BLE001 — one batch failing must not lose the rest
            print(f"  ⚠ batch {bi}/{len(batches)} failed ({type(e).__name__}: {str(e)[:70]}); its facts → 查无源")
            checked.extend({"claim": c, "verdict": "查无源", "source": "",
                            "note": f"batch error: {type(e).__name__}"} for c in batch)
            continue
        raw_parts.append(f"### batch {bi}\n{text}")
        part, perr = _parse_facts(text)
        if part is None:
            print(f"  ⚠ batch {bi}/{len(batches)} parse failed ({perr}); its facts → 查无源")
            checked.extend({"claim": c, "verdict": "查无源", "source": "", "note": "parse failed"} for c in batch)
        else:
            checked.extend(part)
            print(f"  ✓ batch {bi}/{len(batches)}: {len(part)} verdicts")
    archive.write_round_response(ARCHIVE_ROOT, topic_id, role="fact_checker", provider=PRIMARY,
                                 text="\n\n".join(raw_parts) or "<<empty>>")
    if not checked:
        print("  ❌ no verdicts from any batch", file=sys.stderr)
        return 1
    _seed(reg, PRIMARY, "fact_checker")
    print(f"  ✓ primary done: {len(checked)} verdicts")

    # ---- SECONDARY: GPT re-checks the disputed only (batched too) ----
    disputed = [f for f in checked if (f or {}).get("verdict") in DISPUTED]
    secondary = []
    if disputed and not args.no_secondary:
        sb = _chunk(disputed, BATCH_SIZE)
        print(f"[fact_check secondary={SECONDARY}] re-checking {len(disputed)} disputed in {len(sb)} batch(es)...")
        sraw = []
        for bi, batch in enumerate(sb, 1):
            try:
                stext = ask_fresh(SECONDARY, ChromeCDPClient(CDP), build_secondary_prompt(batch))
                sraw.append(f"### batch {bi}\n{stext}")
                part, _ = _parse_facts(stext)
                if part:
                    secondary.extend(part)
            except Exception as e:  # noqa: BLE001 — secondary is best-effort
                print(f"  ⚠ secondary batch {bi}/{len(sb)} failed ({type(e).__name__}); keeping primary")
        if sraw:
            archive.write_round_response(ARCHIVE_ROOT, topic_id, role="fact_checker",
                                         provider=SECONDARY, text="\n\n".join(sraw), label="fact_checker2")
        if secondary:
            _seed(reg, SECONDARY, "fact_checker2")
        print(f"  ✓ secondary returned {len(secondary)} re-checks")

    reg.save()
    data = {"facts": checked, "secondary": secondary,
            "primary": PRIMARY, "secondary_provider": (SECONDARY if secondary else None),
            "generated": _now_iso()}
    archive.write_facts(ARCHIVE_ROOT, topic_id, version=1, data=data)

    a = assess_fact_results(checked)
    print("\n" + "=" * 56)
    print(f"  FACTS: {a['total']} total — " + ", ".join(f"{k}:{v}" for k, v in a["by"].items() if v))
    if a["disputed"]:
        print("  ⚠ NOT all verified — coordinator should WebFetch-spot-check / route these:")
        for f in a["disputed"]:
            print(f"    - [{f.get('verdict')}] {str(f.get('claim',''))[:80]}  src={f.get('source') or '—'}")
    verdict = "ALL 已证实 → banner eligible to drop (pending coordinator spot-check + critic)" if a["all_verified"] \
        else "has disputed → banner STAYS"
    print(f"  VERDICT: {verdict}")
    print(f"  archive: {tdir / 'facts' / 'facts_v1.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
