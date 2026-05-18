"""Shared text utilities — tokenization and similarity metrics."""

from __future__ import annotations

import re


def tokenize(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def jaccard(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def keyword_overlap(content: str, keywords: list[str]) -> float:
    if not keywords:
        return 0.5
    tokens = tokenize(content)
    hits = sum(1 for kw in keywords if kw.lower() in tokens)
    return hits / len(keywords)
