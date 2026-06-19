"""Parallel execution framework keyed on (provider, role).

Lifted from ``coordinate_round_invisible.py`` and generalized:
  * The 0.1.0 skill keyed tasks by ``provider`` only (one runner per
    provider, one round at a time).
  * The new design needs (provider, role) because the same provider can
    appear in multiple roles for one topic (e.g. Claude as both
    ``researcher`` and ``critic``).

The runner per task is supplied by the caller as ``run_task(task, session)``.
Why: the library should not bake in skill-specific prompt-injection or
extraction logic — different roles may want different driver settings
(e.g. critic continues an existing chat without prepare_clean_session,
researcher always starts clean). The skill provides the callable; the
library handles concurrency, session lookup/creation, URL capture, and
error containment.

No default runner is provided — every downstream skill currently needs its
own driver dispatch anyway, and a default would either bake in researcher
semantics (wrong for critic) or be too thin to be useful. If OQ-1 / OQ-3
real-world data shows the skills converging on identical dispatch code,
revisit and lift a default into the library.
"""
from __future__ import annotations

from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    as_completed,
)
from dataclasses import dataclass, field
from typing import Any, Callable

from lib.invisible_chrome_cdp_client import ChromeCDPClient
from lib.session_registry import SessionRegistry
from lib.url_capture import capture_url_after_send


@dataclass(slots=True)
class RoleTask:
    provider: str                  # "gemini" | "gpt" | "claude"
    role: str                      # "researcher" | "fact_checker" | "drafter" | "critic"
    prompt: str
    # ADVISORY HINT, not a hard switch. The library does NOT enforce that an
    # existing session actually exists when this is True. The runner reads
    # the flag and decides whether to skip prepare_clean_session, etc. If
    # you need a hard "fail when no prior session" guarantee, check
    # ``registry.get(provider, role)`` yourself before calling
    # ``execute_parallel``.
    continue_existing: bool = False
    timeout_seconds: int = 300
    landing_url: str | None = None   # override registry default landing URL
    metadata: dict[str, Any] = field(default_factory=dict)  # opaque, passed to runner


@dataclass(slots=True)
class TaskResult:
    provider: str
    role: str
    ok: bool
    response_text: str | None = None
    page_id: str | None = None
    url: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


TaskRunner = Callable[[RoleTask, Any], TaskResult]
"""Caller-supplied runner. Receives (task, page_session) and returns a TaskResult.

The runner is responsible for: prompt injection, waiting, response extraction,
and any provider/role-specific bookkeeping. URL capture is handled by
``execute_parallel`` after the runner returns ok=True (so the runner does NOT
need to read ``window.location.href`` itself).
"""


def execute_parallel(
    tasks: list[RoleTask],
    *,
    cdp_client: ChromeCDPClient,
    registry: SessionRegistry,
    run_task: TaskRunner,
    max_workers: int | None = None,
    thread_timeout_slack_seconds: int = 60,
) -> list[TaskResult]:
    """Run tasks concurrently. One thread per task by default.

    For each task:
      1. ``registry.get_or_create(provider, role, cdp_client, landing_url=...)``
         → returns a SessionRecord (opens a new tab if needed).
      2. ``cdp_client.attach_to_page(record.page_id)`` → PageSession.
      3. ``run_task(task, session)`` runs to completion.
      4. On success, ``capture_url_after_send`` updates the registry's URL.
      5. Exceptions in run_task become ``TaskResult(ok=False, error=...)``.

    Per-task timeout is enforced by the runner (it sees ``task.timeout_seconds``).
    The thread-pool wait adds ``thread_timeout_slack_seconds`` for
    setup/teardown overhead before declaring a hard thread timeout.
    """
    if not tasks:
        return []
    workers = max_workers or len(tasks)
    max_task_timeout = max(t.timeout_seconds for t in tasks)
    pool_timeout = max_task_timeout + thread_timeout_slack_seconds
    debug_endpoint = cdp_client.endpoint

    completed: dict[tuple[str, str], TaskResult] = {}

    # We deliberately do NOT use ``with ThreadPoolExecutor(...) as ex`` here:
    # the context manager's ``__exit__`` calls ``shutdown(wait=True)``, which
    # would block on any straggler that ignored the timeout, defeating the
    # whole point of pool_timeout. Use explicit shutdown with
    # ``cancel_futures=True`` so queued-but-not-started futures get dropped.
    # Caveat: a Python thread that is mid-``run_task`` cannot be killed —
    # ``cancel_futures`` only cancels work that hasn't started yet, and the
    # process exits with those daemon threads still attached to whatever
    # blocking IO they were doing. Runners SHOULD honor
    # ``task.timeout_seconds`` cooperatively (e.g. via driver-level WS read
    # timeouts) so they can return on their own.
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        future_map = {
            executor.submit(
                _run_one,
                task,
                debug_endpoint,
                registry,
                run_task,
            ): task
            for task in tasks
        }
        try:
            for future in as_completed(future_map, timeout=pool_timeout):
                task = future_map[future]
                key = (task.provider, task.role)
                try:
                    completed[key] = future.result()
                except Exception as exc:  # noqa: BLE001
                    completed[key] = TaskResult(
                        provider=task.provider,
                        role=task.role,
                        ok=False,
                        error=f"runner raised: {exc}",
                    )
        except FutureTimeoutError:
            pass
        # Synthesize timeout errors for stragglers.
        for future, task in future_map.items():
            key = (task.provider, task.role)
            if key in completed:
                continue
            future.cancel()
            completed[key] = TaskResult(
                provider=task.provider,
                role=task.role,
                ok=False,
                error=f"thread timeout after {pool_timeout}s",
            )
    finally:
        # Don't wait for stuck threads; drop anything still queued.
        executor.shutdown(wait=False, cancel_futures=True)

    # Return in input order
    return [completed[(t.provider, t.role)] for t in tasks]


def _run_one(
    task: RoleTask,
    debug_endpoint: str,
    registry: SessionRegistry,
    run_task: TaskRunner,
) -> TaskResult:
    """Thread body: each worker owns its own CDP client (HTTP+WS aren't shared)."""
    client = ChromeCDPClient(debug_endpoint)
    try:
        record = registry.get_or_create(
            task.provider,
            task.role,
            client,
            landing_url=task.landing_url,
        )
    except Exception as exc:  # noqa: BLE001
        return TaskResult(
            provider=task.provider,
            role=task.role,
            ok=False,
            error=f"registry.get_or_create failed: {exc}",
        )

    session = None
    try:
        session = client.attach_to_page(record.page_id)
        result = run_task(task, session)
        # Ensure the runner filled in the identifiers (best-effort defaults)
        if not result.page_id:
            result.page_id = record.page_id
        if result.ok:
            try:
                captured = capture_url_after_send(
                    session, registry, task.provider, task.role
                )
                if captured and not result.url:
                    result.url = captured
            except Exception as exc:  # noqa: BLE001
                result.notes.append(f"url_capture failed: {exc}")
        return result
    except Exception as exc:  # noqa: BLE001
        return TaskResult(
            provider=task.provider,
            role=task.role,
            ok=False,
            page_id=record.page_id,
            error=f"attach/run failed: {exc}",
        )
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
