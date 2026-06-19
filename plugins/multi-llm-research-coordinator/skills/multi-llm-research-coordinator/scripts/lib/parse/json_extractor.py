"""Fenced JSON extraction from free-text chat responses.

Lifted from the 0.1.0 skill's ``merge_round.py``. Strict path only:
last fenced ```json``` block wins; if none, fall back to the
outermost ``{...}`` slice. No repair, no retry — those policies belong
in the skill layer once OQ-1 (JSON stability) has real data.
"""
from __future__ import annotations

import json
import re

FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_block(text: str) -> tuple[dict | None, str | None]:
    """Return ``(parsed_dict, error)``.

    Strategy:
      1. Last fenced ```json``` (or unlabeled fence containing ``{...}``) wins.
      2. Otherwise: trim a leading bare ``json`` token, then try the outermost
         ``{...}`` slice, then the whole stripped text.
    """
    if not text:
        return None, "empty response_text"
    matches = FENCED_JSON_RE.findall(text)
    candidate = matches[-1] if matches else text.strip()
    try:
        return json.loads(candidate), None
    except json.JSONDecodeError:
        pass

    stripped = text.strip()
    if stripped.lower().startswith("json"):
        stripped = stripped[4:].lstrip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(stripped[start:end + 1]), None
        except json.JSONDecodeError as exc:
            return None, f"json decode failed: {exc}"
    try:
        return json.loads(stripped), None
    except json.JSONDecodeError as exc:
        return None, f"json decode failed: {exc}"
