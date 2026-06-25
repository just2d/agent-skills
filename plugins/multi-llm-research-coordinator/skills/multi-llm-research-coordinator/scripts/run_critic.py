#!/usr/bin/env python3
"""run_critic.py — T5/T8 critic for an existing topic.

Adversarially review the coordinator's draft from two de-correlated angles,
independently (the two critics never see each other's output → no anchoring).

Roles after option-1 (coordinator = Claude Code drafts locally, so the draft is
Claude lineage): the critics must AVOID Claude lineage to stay de-correlated, so
  - GPT    = logic slant   (logic gaps, mutually-exclusive assumptions, ignored
                            edge cases, over-claims, contradictions with checked facts)
  - Gemini = industry slant (newer/mainstream practice missed, sources that look
                            supportive but conflict, industry consensus mismatch)
web Claude stays a researcher only. (Original design had Claude as the logic
critic; that was valid when GPT drafted — see 01-scenario-1-research.md.)

Reads the draft (final.md / --draft-file / drafts/draft_v1.md) + the fact-check
results (facts/facts_v1.json) so critics know what's already grounded. Output →
rounds/round_critic.{gpt,gemini}.md + a merged flaw list with a count of
`contradicted_fact` flaws (feeds the binary banner gate).

Usage:
  python3 scripts/run_critic.py --topic-id <id>
  python3 scripts/run_critic.py --topic-id <id> --draft-file ./final.md
  python3 scripts/run_critic.py --topic-id <id> --dry-run
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lib.invisible_chrome_cdp_client import ChromeCDPClient
from lib.session_registry import SessionRegistry, SessionRecord
from lib.parse.json_extractor import extract_json_block
from lib import archive
from run_scenario1_core import ask_fresh

ARCHIVE_ROOT = os.environ.get("ARCHIVE_ROOT", os.path.expanduser("~/multi-llm-archives"))
CDP = os.environ.get("CDP_ENDPOINT", "http://127.0.0.1:9222")
COORD = "claude-code"
# (provider, slant-label, slant-instruction) — GPT+Gemini, NOT Claude (same-source as the draft)
CRITICS = [
    ("gpt", "logic", "**逻辑角度**:逻辑漏洞、互斥/未言明的假设、被忽略的边界条件、过度断言(over-claim)、"
                      "以及与【事实核查结果】里已证实/已证伪条目相矛盾之处。"),
    ("gemini", "industry", "**业界角度**:有没有更新/更主流的做法被漏掉、表面支持实则矛盾的来源、"
                           "业界共识与稿子结论不符之处、以及把小众方案当默认推荐的风险。"),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build_critic_prompt(slant_instruction: str, draft: str, facts_blob: str) -> str:
    return (
        "你是评审(critic)。" + slant_instruction + "\n"
        "独立审下面这份调研汇总稿;别只挑措辞小毛病,**优先 high severity 的实质问题**。\n"
        "**只输出一个 ```json 代码块**,schema:\n"
        '{"flaws":[{"issue":"<问题,一句话>","severity":"high|med|low",'
        '"type":"logic_gap|missing_alternative|contradicted_fact|overclaim|scope|other",'
        '"where":"<指向稿子哪一段/哪条结论>"}]}\n'
        "若稿子没有实质问题,返回 {\"flaws\":[]}。只要 JSON。\n\n"
        f"【事实核查结果(已核查的事实,别和这些打架)】\n{facts_blob}\n\n【调研汇总稿】\n{draft}"
    )


def _load_draft(tdir, draft_file) -> str:
    if draft_file:
        return open(os.path.expanduser(draft_file), encoding="utf-8").read()
    for cand in (tdir / "final.md", tdir / "drafts" / "draft_v1.md"):
        if cand.exists():
            return cand.read_text(encoding="utf-8")
    return ""


def _load_facts_blob(tdir) -> str:
    fp = tdir / "facts" / "facts_v1.json"
    if not fp.exists():
        return "(无 fact_check 结果 —— 建议先跑 run_factcheck.py)"
    data = json.loads(fp.read_text(encoding="utf-8"))
    lines = []
    for f in data.get("facts", []):
        lines.append(f"- [{f.get('verdict')}] {f.get('claim')}  ({f.get('source') or '无源'})")
    return "\n".join(lines) or "(空)"


def _parse_flaws(text):
    parsed, err = extract_json_block(text or "")
    if isinstance(parsed, dict) and isinstance(parsed.get("flaws"), list):
        return parsed["flaws"], None
    return None, (err or "no flaws[] in JSON")


def main() -> int:
    ap = argparse.ArgumentParser(description="Adversarially review a topic's draft (GPT logic + Gemini industry).")
    ap.add_argument("--topic-id", required=True)
    ap.add_argument("--draft-file", help="draft to review (default: final.md, then drafts/draft_v1.md)")
    ap.add_argument("--dry-run", action="store_true", help="resolve inputs, send nothing")
    args = ap.parse_args()

    topic_id = args.topic_id
    tdir = archive.topic_dir(ARCHIVE_ROOT, topic_id)
    draft = _load_draft(tdir, args.draft_file)
    if not draft.strip():
        print(f"ERROR: no draft found for {topic_id!r} (need final.md / drafts/draft_v1.md / --draft-file).",
              file=sys.stderr)
        return 2
    facts_blob = _load_facts_blob(tdir)
    print(f"== critic on {topic_id} ==  draft={len(draft)} chars  facts={'yes' if 'verdict' not in facts_blob[:1] else ''}")
    if args.dry_run:
        print(f"[dry-run] would dispatch to: {[p for p, _, _ in CRITICS]}. STOP before any send.")
        return 0

    reg = SessionRegistry.load(ARCHIVE_ROOT, topic_id, COORD)
    all_flaws = []
    for provider, slant, instruction in CRITICS:
        print(f"\n[critic {provider}/{slant}] reviewing...")
        try:
            text = ask_fresh(provider, ChromeCDPClient(CDP), build_critic_prompt(instruction, draft, facts_blob))
        except Exception as e:  # noqa: BLE001 — isolate critics
            print(f"  ✗ {provider} failed: {type(e).__name__}: {str(e)[:140]}")
            continue
        archive.write_round_response(ARCHIVE_ROOT, topic_id, role="critic", provider=provider, text=text or "<<empty>>")
        flaws, err = _parse_flaws(text)
        if flaws is None:
            print(f"  ⚠ {provider} parse failed: {err}")
            continue
        for fl in flaws:
            fl["_critic"] = f"{provider}/{slant}"
        all_flaws.extend(flaws)
        now = _now_iso()
        reg._records[f"{provider}.critic"] = SessionRecord(
            topic_id=topic_id, provider=provider, role="critic", page_id="", status="active",
            coordinator_identity=COORD, created_at=now, last_used_at=now,
            history=[{"at": now, "action": "critic_run", "slant": slant}])
        print(f"  ✓ {provider} returned {len(flaws)} flaw(s)")

    reg.save()
    factual = [f for f in all_flaws if f.get("type") == "contradicted_fact"]
    highs = [f for f in all_flaws if f.get("severity") == "high"]
    print("\n" + "=" * 56)
    print(f"  CRITIC: {len(all_flaws)} flaw(s) total — {len(highs)} high, {len(factual)} contradicted_fact")
    for f in sorted(all_flaws, key=lambda x: {"high": 0, "med": 1, "low": 2}.get(x.get("severity"), 3)):
        print(f"    - [{f.get('severity')}/{f.get('type')}] ({f.get('_critic')}) {str(f.get('issue',''))[:90]}")
    print("\n  → coordinator: fold high/contradicted_fact into a revised draft; "
          "contradicted_fact count feeds the binary banner gate "
          "(>0 → banner STAYS even if facts verified).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
