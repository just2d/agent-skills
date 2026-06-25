#!/usr/bin/env python3
"""run_followup.py — multi-round follow-up on an existing topic.

Send a FOLLOW-UP question to the three researcher threads of an already-run
topic, IN THEIR ORIGINAL CONVERSATIONS, so each model revises/extends its
previous answer WITH its prior context — instead of answering from scratch.

How it differs from run_scenario1_core.py:
  - core run opens a NEW chat per provider (navigate_new_chat) → no memory.
  - this run re-opens each provider's researcher thread by the URL stored in
    sessions.json and SKIPS navigate_new_chat → the model keeps its last turn.

Why re-open by URL (not "the current tab"): the core run's --draft path reuses
the GPT tab for drafting (navigates it away to a fresh chat), so the live GPT
tab is often the *drafter* conversation, not the researcher thread. Re-opening
``sessions.json``'s ``gpt.researcher`` URL is the only reliable way back to the
right conversation. (Same reason resume_topic re-opens by URL.)

Each provider is isolated: a dead/unreachable thread is marked dead and skipped;
the other two still run. New answers append to ``rounds/round_followup<n>.*`` and
``conversation.md`` under the SAME ``--topic-id`` archive, and a refreshed
``drafter_input.md`` is written for coordinator-local synthesis (see SKILL.md).

Usage:
  python3 scripts/run_followup.py --topic-id <id> --question "追问：……？"
  python3 scripts/run_followup.py --topic-id <id> --question-file ./q.md
  python3 scripts/run_followup.py --topic-id <id> --dry-run   # resume + report, send nothing

Prereqs: Chrome :9222 logged in to the three sites (same as the core run).
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
from lib.session_registry import SessionRegistry
from lib.url_capture import read_location_href
from lib import archive
# Reuse the core run's pure helpers so the banner + Claude prefix cleanup stay
# in one place (both are covered by tests/test_pure.py).
from run_scenario1_core import unverified_banner, clean_claude_text

CDP = os.environ.get("CDP_ENDPOINT", "http://127.0.0.1:9222")
ARCHIVE_ROOT = os.environ.get("ARCHIVE_ROOT", os.path.expanduser("~/multi-llm-archives"))
COORD = "claude-code"
PROVIDERS = ["gemini", "gpt", "claude"]
BUDGET_S = int(os.environ.get("RESEARCH_BUDGET_S", "360"))  # ceiling per provider
# A resumed tab re-loads the prior conversation before the composer is usable;
# gemini's SPA hydrates slowest. inject_prompt has its own readiness polling on
# gemini, but a short settle still avoids racing gpt/claude's composer.
HYDRATE_S = {"gemini": 8, "gpt": 5, "claude": 5}
RESUME_RETRIES = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _make_driver(provider, client):
    if provider == "gpt":
        return GptWebDriver(client)
    if provider == "claude":
        return ClaudeWebDriver(client)
    if provider == "gemini":
        return GeminiWebDriver(client)
    raise ValueError(provider)


def _assert_model(provider, d, s, notes):
    """Best-effort model check (claude/gemini); never fatal for a follow-up."""
    try:
        if provider == "claude":
            notes.append(f"model={d.assert_target_model(s)}")
        elif provider == "gemini":
            d.assert_pro_model(s)
            notes.append("model=Pro")
    except Exception as e:  # noqa: BLE001
        notes.append(f"assert_model_failed={type(e).__name__}")


def _wait_and_extract(provider, d, s, base) -> str:
    """Per-provider wait + extract. Signatures intentionally differ:
      gpt   → wait(baseline_response_count=), extract(s)
      claude→ wait(baseline_count=),          extract(s) [+ prefix cleanup]
      gemini→ wait(baseline_count=),          extract(s, baseline_count=) REQUIRED
    """
    if provider == "gpt":
        d.wait_for_streaming_done(s, baseline_response_count=base, timeout_seconds=BUDGET_S)
        return d.extract_last_assistant_message(s)
    if provider == "claude":
        d.wait_for_streaming_done(s, baseline_count=base, timeout_seconds=BUDGET_S)
        return clean_claude_text(d.extract_last_assistant_message(s))
    if provider == "gemini":
        d.wait_for_streaming_done(s, baseline_count=base, timeout_seconds=BUDGET_S)
        return d.extract_last_assistant_message(s, baseline_count=base)
    raise ValueError(provider)


def _find_open_tab(client, url) -> str | None:
    """page_id of an already-open tab on this exact conversation (path match,
    ignoring query/fragment), or None."""
    def _norm(u):
        return (u or "").split("?")[0].split("#")[0].rstrip("/")
    base = _norm(url)
    try:
        pages = client.list_pages()
    except Exception:  # noqa: BLE001
        return None
    for p in pages:
        if _norm(p.url) == base:
            return p.id
    return None


def _resume(client, reg, provider, *, persist):
    """Get a driveable session on this provider's researcher conversation.
    Returns ``(session, record, error, page_id)``.

    Prefer an already-open tab on the exact conversation URL — no focus steal, no
    duplicate tab (keeps the project's invisible principle). Fall back to
    ``open_new_tab`` (which foregrounds once) only when the tab was closed or
    repurposed — the core run's --draft path reuses the GPT tab for drafting, so
    the live GPT tab is the drafter conversation, not the researcher thread.

    ``persist=False`` (dry-run): no sessions.json mutation (no replace_page_id /
    mark_dead). ``persist=True``: record the landed page_id; mark_dead only after
    every open retry fails.
    """
    rec = reg.get(provider, "researcher")
    if rec is None:
        return None, None, "no researcher session recorded", None
    if rec.status != "active":
        return None, rec, f"session status={rec.status} (not active)", None
    if not rec.url:
        return None, rec, "no captured URL (thread never produced a turn)", None

    # 1) reuse an existing tab already on this conversation
    pid = _find_open_tab(client, rec.url)
    if pid:
        try:
            s = client.attach_to_page(pid)
            if persist and rec.page_id != pid:
                reg.replace_page_id(provider, "researcher", pid,
                                    reason="followup attach (existing tab)")
            return s, (reg.get(provider, "researcher") or rec), None, pid
        except Exception:  # noqa: BLE001 — stale id; fall through to a fresh open
            pass

    # 2) open the conversation in a fresh tab (foregrounds once), with retries
    last = None
    for attempt in range(1, RESUME_RETRIES + 1):
        try:
            page = client.open_new_tab(rec.url)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < RESUME_RETRIES:
                time.sleep(attempt * 1.0)
            continue
        if persist:
            reg.replace_page_id(provider, "researcher", page.id,
                                reason=f"followup resume (attempt {attempt}/{RESUME_RETRIES})")
        try:
            s = client.attach_to_page(page.id)
        except Exception as e:  # noqa: BLE001
            return None, rec, f"attach failed: {type(e).__name__}: {str(e)[:120]}", page.id
        return s, (reg.get(provider, "researcher") or rec), None, page.id

    if persist:
        reg.mark_dead(provider, "researcher", reason=f"followup resume failed: {last}")
    return None, rec, f"resume failed after {RESUME_RETRIES} retries: {last}", None


def _next_followup_index(topic_dir) -> int:
    """1 + the highest existing round_followup<n>.* index, else 1."""
    rounds = topic_dir / "rounds"
    nums = []
    if rounds.exists():
        for f in rounds.glob("round_followup*.md"):
            m = re.search(r"round_followup(\d+)\.", f.name)
            if m:
                nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def build_followup_input(question: str, answers: dict, n: int, ts: str) -> str:
    """Coordinator-local synthesis brief for a follow-up round (mirrors the core
    run's drafter_input.md, but frames the answers as revisions building on the
    prior final.md). Pure — unit-tested in tests/test_pure.py."""
    blocks = []
    for p in PROVIDERS:
        a = answers.get(p)
        body = a if (a and a.strip()) else "<<无回答>>"
        blocks.append(f"【{p} 的追问回答(第 {n} 轮 follow-up)】\n{body}")
    joined = "\n\n".join(blocks)
    return (
        "<!-- COORDINATOR (Claude Code): this is a FOLLOW-UP round. Synthesize the "
        "UPDATED deliverable from THIS file. (1) prepend the BANNER block below "
        "VERBATIM; (2) write the four sections, treating these answers as "
        "revisions/additions that BUILD ON the prior final.md (not a fresh start); "
        "(3) Write the whole thing to final.md. Then surface final.md + its "
        "未消解分歧 section to the user AS-IS — do NOT compress or re-summarize. -->\n\n"
        "## ⬇ BANNER — prepend to final.md VERBATIM\n"
        "```md\n" + unverified_banner(ts) + "```\n\n"
        f"## ⬇ FOLLOW-UP SYNTHESIS TASK (round {n})\n"
        "把下面三家对【追问】的新回答合成进更新版结论,**用 Markdown,强制四段**:\n"
        "## 1. 三家共识结论\n## 2. 高价值少数派意见（只1家提但有道理的）\n"
        "## 3. 未消解分歧（三家明显不同取舍的）\n## 4. 给我的推荐（紧扣【追问】）\n"
        "(某段没有内容就写“无”,不要省略段落;结合上一轮 final.md 一起看。)\n\n"
        f"【追问（第 {n} 轮 follow-up）】\n{question}\n\n{joined}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Send a follow-up question to a topic's three researcher threads "
                    "(same conversations, with prior context).")
    ap.add_argument("--topic-id", required=True, help="existing archive folder under ARCHIVE_ROOT")
    ap.add_argument("--question", help="the follow-up question")
    ap.add_argument("--question-file", help="read the follow-up from a file (overrides --question)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resume threads + report reachability, send nothing (no sessions.json mutation)")
    args = ap.parse_args()

    topic_id = args.topic_id
    reg = SessionRegistry.load(ARCHIVE_ROOT, topic_id, COORD)
    if not reg.list_all():
        print(f"ERROR: no sessions.json for topic {topic_id!r} under {ARCHIVE_ROOT}. "
              f"Run run_scenario1_core.py for this topic first.", file=sys.stderr)
        return 2

    question = ""
    if args.question_file:
        question = open(os.path.expanduser(args.question_file), encoding="utf-8").read().strip()
    elif args.question:
        question = args.question
    if not args.dry_run and not question.strip():
        print("ERROR: --question / --question-file required (unless --dry-run).", file=sys.stderr)
        return 2

    tdir = archive.topic_dir(ARCHIVE_ROOT, topic_id)
    n = _next_followup_index(tdir)
    print(f"== follow-up round {n} on topic {topic_id} ==\n"
          f"  budget={BUDGET_S}s/provider  dry_run={args.dry_run}")

    # ---- resume the three researcher threads (researchers only; not drafter) ----
    sessions = {}  # provider -> (client, session, record)
    for p in PROVIDERS:
        client = ChromeCDPClient(CDP)
        s, rec, err, pid = _resume(client, reg, p, persist=not args.dry_run)
        if err:
            print(f"  ✗ {p}: {err}")
            continue
        sessions[p] = (client, s, rec)
        print(f"  ✓ {p} resumed → {pid}  {rec.url[:58]}")

    if not sessions:
        print("No researcher threads resumable. Nothing to do.")
        return 1

    if args.dry_run:
        print(f"\n[dry-run] resumable: {sorted(sessions)}. STOP before any send.")
        for _p, (_c, s, _r) in sessions.items():
            try:
                s.close()
            except Exception:
                pass
        return 0

    archive.write_conversation_turn(ARCHIVE_ROOT, topic_id, speaker="user",
                                    text=f"[follow-up {n}] {question}")

    # ---- send the follow-up into each resumed thread (no navigate_new_chat) ----
    answers = {}
    for p in PROVIDERS:
        if p not in sessions:
            answers[p] = None
            continue
        client, s, rec = sessions[p]
        notes, t0 = [], time.time()
        try:
            time.sleep(HYDRATE_S[p])           # let the resumed conversation hydrate
            d = _make_driver(p, client)
            _assert_model(p, d, s, notes)
            base = d.baseline_response_count(s)
            d.inject_prompt(s, question)       # SAME thread — deliberately no navigate_new_chat
            d.submit(s)
            ans = _wait_and_extract(p, d, s, base)
            answers[p] = ans
            url = read_location_href(s)
            if url:
                reg.update_url(p, "researcher", url)
            archive.write_round_response(ARCHIVE_ROOT, topic_id, role="researcher",
                                         provider=p, text=(ans or "<<empty>>"),
                                         label=f"followup{n}")
            print(f"  ✓ {p} answered ({time.time()-t0:.1f}s, {len(ans or '')} chars) {notes}")
        except Exception as e:  # noqa: BLE001 — isolate: one provider must not abort the others
            answers[p] = None
            err = f"{type(e).__name__}: {str(e)[:140]}"
            # Do NOT mark_dead here: a send/extract error is often transient (UI
            # drift, slow render). The thread stays active so a re-run can retry.
            archive.write_round_response(ARCHIVE_ROOT, topic_id, role="researcher",
                                         provider=p, text=f"<<NO RESPONSE: {err}>>",
                                         label=f"followup{n}")
            print(f"  ✗ {p} ({time.time()-t0:.1f}s): {err}")
        finally:
            try:
                s.close()
            except Exception:
                pass

    reg.save()
    ok = [p for p in PROVIDERS if answers.get(p)]
    print(f"\n[synthesis] follow-up {n} answers from: {ok or 'none'}")
    if not ok:
        print("  all three follow-ups failed; not writing drafter_input.md.")
        archive.update_manifest(ARCHIVE_ROOT, topic_id, ended_at=_now_iso(),
                                final_status=f"followup{n}_all_failed")
        return 1

    di_path = tdir / "drafter_input.md"
    di_path.write_text(build_followup_input(question, answers, n, _now_iso()), encoding="utf-8")
    archive.update_manifest(ARCHIVE_ROOT, topic_id, ended_at=_now_iso(),
                            final_status=f"followup{n}_done_pending_coordinator_synthesis")
    print(f"  ✅ drafter_input.md refreshed for follow-up {n} (answers: {ok}).")
    print(f"  → NEXT (coordinator): synthesize {tdir / 'final.md'} from it, then surface — no re-summary.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
