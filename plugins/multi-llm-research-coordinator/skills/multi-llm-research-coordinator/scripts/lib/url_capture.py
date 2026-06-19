"""Capture the final chat URL after the first message is sent.

Each of the three chat providers navigates from a landing URL (e.g.
``https://gemini.google.com/app``) to a session-specific URL
(``https://gemini.google.com/app/abc123``) once the first prompt is
submitted. We read ``window.location.href`` post-send and write it back
to the SessionRegistry so resume_topic / revisit.md have the real link.

The driver layer does NOT call this — the skill / parallel_exec wraps
the driver call. Keeps drivers ignorant of the registry (single
responsibility).
"""
from __future__ import annotations

from typing import Any


def read_location_href(session: Any, *, timeout: float = 5.0) -> str | None:
    """Read ``window.location.href`` from a CDP PageSession. None on failure."""
    try:
        result = session.runtime_evaluate("window.location.href", timeout=timeout)
    except Exception:
        return None
    value = (result.get("result", {}) or {}).get("value")
    if isinstance(value, str) and value:
        return value
    return None


def capture_url_after_send(
    session: Any,
    registry,  # SessionRegistry — loose-typed to avoid import cycle
    provider: str,
    role: str,
    *,
    timeout: float = 5.0,
) -> str | None:
    """Read location.href and write it to registry. Idempotent.

    Returns the captured URL or None if read failed. Safe to call on every
    turn — the registry deduplicates.
    """
    url = read_location_href(session, timeout=timeout)
    if url is None:
        return None
    registry.update_url(provider, role, url)
    return url
