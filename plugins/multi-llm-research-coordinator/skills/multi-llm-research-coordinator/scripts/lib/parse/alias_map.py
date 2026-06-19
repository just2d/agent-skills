"""Candidate-name normalization and alias suggestion.

Lifted from the 0.1.0 skill's ``merge_round.py``. Used when multiple
researchers propose what may be the same option under different names
(e.g. "Apache Kafka" vs "Kafka"). The library only *suggests* possible
aliases; merging is the skill's call.
"""
from __future__ import annotations

import itertools
import json
import re
from pathlib import Path


def normalize_name(name: str) -> str:
    """Lowercase + collapse whitespace. Empty-safe."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


class AliasMap:
    """Reverse alias map (norm → canonical norm key) + display map (norm key → canonical display)."""

    def __init__(self, reverse: dict[str, str], display: dict[str, str]) -> None:
        self.reverse = reverse
        self.display = display

    def key(self, name: str) -> str:
        base = normalize_name(name)
        return self.reverse.get(base, base)

    def display_for(self, key: str, fallback: str) -> str:
        return self.display.get(key, fallback)


def load_aliases(path: str | None) -> AliasMap:
    """Load aliases.json: ``{canonical: [alias, ...]}``. Empty path → empty map."""
    if not path:
        return AliasMap({}, {})
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    reverse: dict[str, str] = {}
    display: dict[str, str] = {}
    for canonical, aliases in raw.items():
        c_norm = normalize_name(canonical)
        reverse[c_norm] = c_norm
        display[c_norm] = canonical
        for alias in aliases or []:
            reverse[normalize_name(alias)] = c_norm
    return AliasMap(reverse, display)


def canon_key(name: str, aliases: AliasMap) -> str:
    return aliases.key(name)


def canon_display(key: str, raw_name: str, aliases: AliasMap) -> str:
    return aliases.display_for(key, raw_name)


def suggest_alias_pairs(names: list[str]) -> list[dict]:
    """Pairs of names that may refer to the same thing — for human review, not auto-merge.

    Heuristic (cheap, no external deps): for each unordered pair (a, b),
    flag if either
      (1) one normalized name contains the other as a substring (e.g. 'Kafka'
          in 'Apache Kafka'), or
      (2) token-set Jaccard similarity >= 0.5 on space/punctuation-split tokens.

    Each suggestion: ``{"a": str, "b": str, "reason": "substring"|"jaccard", "score": float|None}``.
    """
    def tokens(name: str) -> set[str]:
        norm = re.sub(r"[^\w]+", " ", name.lower()).strip()
        return {t for t in norm.split() if t}

    suggestions: list[dict] = []
    for a, b in itertools.combinations(names, 2):
        na = re.sub(r"\s+", " ", a.lower()).strip()
        nb = re.sub(r"\s+", " ", b.lower()).strip()
        if not na or not nb or na == nb:
            continue
        if na in nb or nb in na:
            suggestions.append({"a": a, "b": b, "reason": "substring", "score": None})
            continue
        ta, tb = tokens(a), tokens(b)
        if not ta or not tb:
            continue
        inter = len(ta & tb)
        union = len(ta | tb)
        jaccard = inter / union if union else 0.0
        if jaccard >= 0.5:
            suggestions.append({"a": a, "b": b, "reason": "jaccard", "score": round(jaccard, 3)})
    return suggestions
