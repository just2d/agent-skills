"""Resume a previously archived topic: re-open every active session URL.

Flow per ``03-common-lib.md`` §六:
  1. Load sessions.json via :class:`SessionRegistry`.
  2. For each active session with a captured URL, retry ``open_new_tab`` up
     to ``max_retries`` times with exponential backoff.
  3. Success → ``replace_page_id`` (new tab, same logical session).
  4. All retries fail → ``mark_dead`` with the accumulated error.
  5. Sessions with no captured URL (e.g. crashed before first send) are
     skipped — there is nothing to land on. The caller can inspect
     ``registry.list_active()`` afterwards to find them.

Distinguishing infra failure (transient) from business state (session no
longer reachable) is why we retry: a single ``open_new_tab`` failure is
not enough evidence to mark the chat as dead.
"""
from __future__ import annotations

import time
from pathlib import Path

from lib.session_registry import SessionRegistry


def resume_topic(
    archive_root: Path | str,
    topic_id: str,
    cdp_client,  # ChromeCDPClient — loose-typed
    *,
    coordinator_identity: str = "",
    max_retries: int = 3,
    retry_backoff_seconds: float = 1.0,
) -> SessionRegistry:
    """Re-open all active session URLs in fresh tabs. Returns the registry.

    Each session gets up to ``max_retries`` attempts at ``open_new_tab``.
    Backoff is linear (``attempt * retry_backoff_seconds``) to avoid stampeding
    a flaky Chrome. Only after every retry exhausts do we ``mark_dead``;
    transient network blips don't bury a session.
    """
    registry = SessionRegistry.load(archive_root, topic_id, coordinator_identity)

    for record in list(registry.list_active()):
        if not record.url:
            continue
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                page = cdp_client.open_new_tab(record.url)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < max_retries:
                    time.sleep(attempt * retry_backoff_seconds)
                continue
            registry.replace_page_id(
                record.provider,
                record.role,
                page.id,
                reason=f"resumed (attempt {attempt}/{max_retries})",
            )
            break
        else:
            registry.mark_dead(
                record.provider,
                record.role,
                reason=f"resume failed after {max_retries} retries: {last_exc}",
            )

    return registry
