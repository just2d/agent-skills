"""Incremental archive IO for the multi-LLM coordinator.

Per ``04-archive-format.md``: every topic owns a directory under
``~/multi-llm-archives/{topic_id}/`` containing manifest, conversation,
sessions (managed by SessionRegistry), round responses, facts, drafts,
final, interventions, and the human-readable revisit.md.

This module is **pure atomic IO**. WHEN to write is the skill's
decision; the lib just exposes individual write/append/update calls.
Each writer is idempotent at the file-overwrite level (versioned files
take an explicit ``version`` argument).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_ARCHIVE_ROOT = Path.home() / "multi-llm-archives"

# Pre-filled role keys for manifest.round_counts, per
# 04-archive-format.md §iii. Skill bumps these as rounds complete; the lib
# initializes them all to 0 so the manifest has the full shape from t=0.
_ROUND_COUNT_ROLES = ("researcher", "fact_checker", "drafter", "critic")

# Note on threading: manifest / interventions / conversation are all written
# from the coordinator main thread (T1/T3/T6/T9 in scenario 1). They are
# NOT touched by the parallel worker threads in execute_parallel — workers
# only call SessionRegistry.update_url. So we do not lock here. The atomic
# rename in _write_*_atomic is for *crash* safety (no half-written files
# if the process dies mid-write), not concurrency. If a future skill
# genuinely needs concurrent manifest writes, add locking then.


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def topic_dir(archive_root: Path | str, topic_id: str) -> Path:
    return Path(archive_root) / topic_id


def init_topic_dir(archive_root: Path | str, topic_id: str) -> Path:
    """Create the topic directory and standard subdirs. Idempotent."""
    root = topic_dir(archive_root, topic_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "rounds").mkdir(exist_ok=True)
    (root / "facts").mkdir(exist_ok=True)
    (root / "drafts").mkdir(exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# manifest.json
# ---------------------------------------------------------------------------

def init_manifest(
    archive_root: Path | str,
    topic_id: str,
    *,
    problem_statement: str,
    scenario: str,
    coordinator: dict,
    providers: dict,
    started_at: str | None = None,
) -> Path:
    """Create manifest.json with required fields. Won't overwrite an existing one.

    ``round_counts`` is pre-populated with all four role keys at 0 so the
    manifest matches the 04-archive-format.md §iii shape from t=0; the skill
    increments them as rounds complete.

    Must be called before any other archive writer that touches the manifest
    (``update_manifest``, ``append_intervention``, ``write_final``).
    """
    root = init_topic_dir(archive_root, topic_id)
    path = root / "manifest.json"
    if path.exists():
        return path
    manifest = {
        "topic_id": topic_id,
        "problem_statement": problem_statement,
        "scenario": scenario,
        "coordinator": coordinator,
        "providers": providers,
        "started_at": started_at or _now_iso(),
        "ended_at": None,
        "final_status": "in_progress",
        "interruptions": [],
        "round_counts": {role: 0 for role in _ROUND_COUNT_ROLES},
    }
    _write_json_atomic(path, manifest)
    return path


def update_manifest(archive_root: Path | str, topic_id: str, **fields: Any) -> Path:
    """Shallow-merge ``fields`` into manifest.json. Lists are replaced, not appended.

    Single-threaded: call this from the coordinator main thread only. For
    ``interruptions`` append use :func:`append_intervention`.
    """
    path = topic_dir(archive_root, topic_id) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    manifest.update(fields)
    _write_json_atomic(path, manifest)
    return path


# ---------------------------------------------------------------------------
# rounds/round_<role>.<provider>[.v<n>].md
# ---------------------------------------------------------------------------

def write_round_response(
    archive_root: Path | str,
    topic_id: str,
    *,
    role: str,
    provider: str,
    text: str,
    version: int | None = None,
    label: str | None = None,
) -> Path:
    """Write a single provider's raw response for a given role.

    ``label`` lets the skill distinguish e.g. ``critic1`` vs ``critic2`` while
    keeping the role taxonomy intact. ``version`` (when given) yields a
    ``.v{n}.md`` suffix — used for drafter revisions.
    """
    root = init_topic_dir(archive_root, topic_id)
    role_token = label or role
    suffix = f".v{version}" if version is not None else ""
    path = root / "rounds" / f"round_{role_token}.{provider}{suffix}.md"
    _write_text_atomic(path, text)
    return path


# ---------------------------------------------------------------------------
# facts/facts_v<n>.json
# ---------------------------------------------------------------------------

def write_facts(
    archive_root: Path | str,
    topic_id: str,
    *,
    version: int,
    data: dict,
) -> Path:
    root = init_topic_dir(archive_root, topic_id)
    path = root / "facts" / f"facts_v{version}.json"
    _write_json_atomic(path, data)
    return path


# ---------------------------------------------------------------------------
# drafts/draft_v<n>.md
# ---------------------------------------------------------------------------

def write_draft(
    archive_root: Path | str,
    topic_id: str,
    *,
    version: int,
    text: str,
) -> Path:
    root = init_topic_dir(archive_root, topic_id)
    path = root / "drafts" / f"draft_v{version}.md"
    _write_text_atomic(path, text)
    return path


# ---------------------------------------------------------------------------
# final.md
# ---------------------------------------------------------------------------

def write_final(archive_root: Path | str, topic_id: str, text: str) -> Path:
    root = init_topic_dir(archive_root, topic_id)
    path = root / "final.md"
    _write_text_atomic(path, text)
    update_manifest(
        archive_root,
        topic_id,
        ended_at=_now_iso(),
        final_status="completed_with_draft",
    )
    return path


# ---------------------------------------------------------------------------
# user_interventions.md + manifest interruptions append
# ---------------------------------------------------------------------------

def append_intervention(
    archive_root: Path | str,
    topic_id: str,
    *,
    category: str,
    user_response: str,
    elapsed_to_response_sec: int | None = None,
    at: str | None = None,
    body_markdown: str | None = None,
) -> Path:
    """Append one intervention to ``user_interventions.md`` and manifest.json.

    Single-threaded: call from coordinator main thread only.
    """
    root = init_topic_dir(archive_root, topic_id)
    ts = at or _now_iso()
    md_path = root / "user_interventions.md"
    section = body_markdown or (
        f"\n## {ts} — {category}\n\n"
        f"**User response**: {user_response}\n"
    )
    if elapsed_to_response_sec is not None:
        section += f"\n_Elapsed to response: {elapsed_to_response_sec}s_\n"
    manifest_path = root / "manifest.json"
    with md_path.open("a", encoding="utf-8") as fh:
        fh.write(section)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest.setdefault("interruptions", []).append({
        "at": ts,
        "category": category,
        "user_response": user_response,
        "elapsed_to_response_sec": elapsed_to_response_sec,
    })
    _write_json_atomic(manifest_path, manifest)
    return md_path


# ---------------------------------------------------------------------------
# conversation.md
# ---------------------------------------------------------------------------

def write_conversation_turn(
    archive_root: Path | str,
    topic_id: str,
    *,
    speaker: str,
    text: str,
    at: str | None = None,
) -> Path:
    """Append one turn (``user`` / ``coordinator`` / etc.) to conversation.md.

    Single-threaded: call from coordinator main thread only.
    """
    root = init_topic_dir(archive_root, topic_id)
    path = root / "conversation.md"
    ts = at or _now_iso()
    block = f"\n### {ts} — {speaker}\n\n{text}\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return path


# ---------------------------------------------------------------------------
# revisit.md (derived from SessionRegistry)
# ---------------------------------------------------------------------------

def generate_revisit(
    archive_root: Path | str,
    topic_id: str,
    registry,  # SessionRegistry — loose-typed
    *,
    title: str | None = None,
) -> Path:
    """Generate human-readable revisit.md listing every session + its URL.

    Sessions without a captured URL get a ``(no URL yet)`` placeholder so
    the user can see which roles started but haven't sent a first message.
    """
    root = init_topic_dir(archive_root, topic_id)
    path = root / "revisit.md"

    header = title or f"# {topic_id} — 回看入口"
    lines: list[str] = [header, ""]
    lines.append("点对应链接继续跟当时的 AI 聊（需登录态）：\n")

    # Group by role for readability.
    by_role: dict[str, list[Any]] = {}
    for rec in registry.list_all():
        by_role.setdefault(rec.role, []).append(rec)

    for role, recs in by_role.items():
        lines.append(f"## {role}")
        for rec in recs:
            tag = "" if rec.status == "active" else f"  _({rec.status})_"
            if rec.url:
                lines.append(f"- [{rec.provider} {rec.role}]({rec.url}){tag}")
            else:
                lines.append(f"- {rec.provider} {rec.role} — (no URL yet, page_id `{rec.page_id}`){tag}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"恢复完整现场： `/resume-topic {topic_id}`")
    _write_text_atomic(path, "\n".join(lines))
    return path


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_dataclass(data) and not isinstance(data, type):
        data = asdict(data)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
