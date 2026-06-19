"""Shared helpers for the Claude and GPT invisible CDP drivers.

Kept deliberately tiny and dependency-free so each driver remains
self-contained (per SKILL.md's "no cross-project imports at runtime" rule —
this module ships alongside the drivers in scripts/).

Note: the Gemini driver has a distinct ``_split_into_safe_chunks``
implementation (it raises on target_size<1 and returns [] for empty input);
do not unify it without re-validating against the Gemini DOM behavior
documented in invisible_gemini_web_driver.py.
"""
from __future__ import annotations


def split_into_safe_chunks(text: str, target_size: int) -> list[str]:
    """Split text into ~target_size chunks, never seaming on a trailing newline.

    Shared by Claude and GPT drivers. See invisible_gemini_web_driver.py for
    Gemini's variant and the rationale behind newline-aware seams.
    """
    if target_size < 1 or not text:
        return [text] if text else []
    chunks: list[str] = []
    n = len(text)
    pos = 0
    while pos < n:
        end = pos + target_size
        if end >= n:
            chunks.append(text[pos:])
            break
        while end > pos + 1 and text[end - 1] == "\n":
            end -= 1
        if end <= pos + 1:
            end = pos + target_size
            while end < n and text[end - 1] == "\n":
                end += 1
            if end >= n:
                chunks.append(text[pos:])
                break
        chunks.append(text[pos:end])
        pos = end
    return chunks


def extract_value(runtime_result: dict):
    """Pull the plain Python value out of a PageSession.runtime_evaluate result.

    runtime_evaluate returns the CDP ``result`` envelope:
        {"result": {"type": "string", "value": "hello"}}
    With returnByValue=True, dicts come through directly in ``value``.
    """
    inner = runtime_result.get("result", {})
    return inner.get("value")
