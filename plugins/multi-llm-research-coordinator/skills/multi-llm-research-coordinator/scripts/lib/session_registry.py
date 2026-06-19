"""SessionRegistry: maps (topic_id, provider, role) → page_id + URL + status.

Persisted at ``{archive_root}/{topic_id}/sessions.json``. One registry instance
per topic. Skill code calls ``get_or_create`` to obtain a chat tab for a given
``(provider, role)``; if absent, the registry opens a new tab via the CDP
client and records it. URL is filled in later by ``url_capture`` after the
first message is sent (the navigation completes async on each provider's site).

The registry stores ``coordinator_identity`` for archive provenance but does
NOT enforce same-source veto rules — that policy belongs in the skill that
constructs the role assignments.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

# Provider landing URLs used when opening a fresh chat tab. Kept here (rather
# than per-driver) because the registry is the single point that opens tabs.
PROVIDER_LANDING_URLS: dict[str, str] = {
    "gemini": "https://gemini.google.com/app",
    "gpt": "https://chatgpt.com/",
    "claude": "https://claude.ai/new",
}

SessionStatus = Literal["active", "dead", "archived"]


@dataclass(slots=True)
class SessionRecord:
    topic_id: str
    provider: str
    role: str
    page_id: str
    url: str | None = None
    model: str | None = None
    status: SessionStatus = "active"
    coordinator_identity: str = ""
    created_at: str = ""
    last_used_at: str = ""
    history: list[dict] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.provider}.{self.role}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class SessionRegistry:
    """One registry instance per topic. Not safe across processes — only one
    coordinator should hold a registry for a given topic at a time."""

    def __init__(
        self,
        archive_root: Path | str,
        topic_id: str,
        coordinator_identity: str,
    ) -> None:
        self.archive_root = Path(archive_root)
        self.topic_id = topic_id
        self.coordinator_identity = coordinator_identity
        self._records: dict[str, SessionRecord] = {}
        # Single coarse lock. The whole get_or_create (including the slow
        # open_new_tab call) runs under it. With 3 workers max, this
        # serializes ~3 tab opens per topic — acceptable; lets us keep the
        # locking story trivial. If we ever need real parallel tab opens,
        # switch to per-key in-flight dedupe; until then, simpler is better.
        self._lock = threading.Lock()
        self._dir = self.archive_root / topic_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "sessions.json"

    # ----- persistence --------------------------------------------------

    @classmethod
    def load(
        cls,
        archive_root: Path | str,
        topic_id: str,
        coordinator_identity: str = "",
    ) -> "SessionRegistry":
        reg = cls(archive_root, topic_id, coordinator_identity)
        if not reg._path.exists():
            return reg
        data = json.loads(reg._path.read_text(encoding="utf-8"))
        # coordinator_identity from disk wins if caller passed empty
        if not coordinator_identity:
            reg.coordinator_identity = data.get("coordinator_identity", "")
        for key, raw in (data.get("sessions") or {}).items():
            # Per 04-archive-format.md §iv: the key encodes "<provider>.<role>",
            # and the example sessions.json does NOT necessarily duplicate them
            # inside each record. Tolerate either shape: prefer the record's
            # explicit fields when present, fall back to key-derived values.
            provider = raw.get("provider")
            role = raw.get("role")
            if not provider or not role:
                if "." not in key:
                    raise ValueError(
                        f"sessions.json record key {key!r} not in '<provider>.<role>' form "
                        "and record lacks explicit provider/role fields"
                    )
                key_provider, _, key_role = key.partition(".")
                provider = provider or key_provider
                role = role or key_role
            reg._records[key] = SessionRecord(
                topic_id=topic_id,
                provider=provider,
                role=role,
                page_id=raw["page_id"],
                url=raw.get("url"),
                model=raw.get("model"),
                status=raw.get("status", "active"),
                coordinator_identity=raw.get("coordinator_identity", reg.coordinator_identity),
                created_at=raw.get("created_at", ""),
                last_used_at=raw.get("last_used_at", ""),
                history=list(raw.get("history") or []),
            )
        return reg

    def save(self) -> None:
        """Serialize and atomically replace sessions.json.

        Single lock; tmp file + replace gives crash-safety (no half-written
        sessions.json if the process dies mid-write). The atomic rename is
        for *crash* safety, not concurrency — concurrent callers serialize
        on the lock.
        """
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        """Save without acquiring the lock; only call when self._lock is held."""
        payload = {
            "topic_id": self.topic_id,
            "coordinator_identity": self.coordinator_identity,
            "sessions": {k: asdict(v) for k, v in self._records.items()},
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ----- core ops -----------------------------------------------------

    def get_or_create(
        self,
        provider: str,
        role: str,
        cdp_client,  # ChromeCDPClient — typed loosely to avoid hard import cycle
        *,
        landing_url: str | None = None,
    ) -> SessionRecord:
        """Return active session for (provider, role); open a new tab if none.

        ``landing_url`` overrides the default :data:`PROVIDER_LANDING_URLS`
        entry — useful when the skill wants to land on a model-specific path
        (e.g. Claude with a model query string).

        Holds the registry lock for the entire call (including the slow
        ``open_new_tab``). At 3 workers max this is fine.
        """
        if provider not in PROVIDER_LANDING_URLS and landing_url is None:
            raise ValueError(f"unknown provider {provider!r}; pass landing_url")
        key = f"{provider}.{role}"

        with self._lock:
            existing = self._records.get(key)
            if existing is not None and existing.status == "active":
                existing.last_used_at = _now_iso()
                existing.history.append({"at": existing.last_used_at, "action": "reused"})
                self._save_locked()
                return existing

            url = landing_url or PROVIDER_LANDING_URLS[provider]
            page = cdp_client.open_new_tab(url)
            now = _now_iso()
            record = SessionRecord(
                topic_id=self.topic_id,
                provider=provider,
                role=role,
                page_id=page.id,
                url=None,  # filled in by url_capture after first send
                model=None,
                status="active",
                coordinator_identity=self.coordinator_identity,
                created_at=now,
                last_used_at=now,
                history=[{"at": now, "action": "created", "landing_url": url, "page_id": page.id}],
            )
            self._records[key] = record
            self._save_locked()
            return record

    def mark_dead(self, provider: str, role: str, reason: str) -> None:
        key = f"{provider}.{role}"
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            record.status = "dead"
            record.history.append({"at": _now_iso(), "action": "marked_dead", "reason": reason})
        self.save()

    def update_url(self, provider: str, role: str, url: str) -> None:
        key = f"{provider}.{role}"
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            if record.url == url:
                return  # idempotent
            record.url = url
            record.history.append({"at": _now_iso(), "action": "url_captured", "url": url})
        self.save()

    def update_model(self, provider: str, role: str, model: str) -> None:
        key = f"{provider}.{role}"
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            if record.model == model:
                return
            record.model = model
            record.history.append({"at": _now_iso(), "action": "model_recorded", "model": model})
        self.save()

    def replace_page_id(self, provider: str, role: str, new_page_id: str, *, reason: str) -> None:
        """Used by resume_topic when a session URL is re-opened in a new tab."""
        key = f"{provider}.{role}"
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            old = record.page_id
            record.page_id = new_page_id
            record.last_used_at = _now_iso()
            record.history.append({
                "at": record.last_used_at,
                "action": "page_id_replaced",
                "old_page_id": old,
                "new_page_id": new_page_id,
                "reason": reason,
            })
        self.save()

    def get(self, provider: str, role: str) -> SessionRecord | None:
        return self._records.get(f"{provider}.{role}")

    def list_active(self) -> list[SessionRecord]:
        return [r for r in self._records.values() if r.status == "active"]

    def list_all(self) -> list[SessionRecord]:
        return list(self._records.values())
