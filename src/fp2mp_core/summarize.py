"""Small text compression helper for prompt context and promoted claims."""

from __future__ import annotations

import hashlib
from functools import lru_cache

from fp2mp_core.llm import get_chat_model


def compress_text(
    text: str,
    max_chars: int = 600,
    focus: str = "preserve all numbers, IDs, hypotheses, and caveats",
) -> str:
    """Return text as-is when short, otherwise summarize while preserving evidence."""
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return _compress_cached(value, max_chars, focus)


@lru_cache(maxsize=256)
def _compress_cached(text: str, max_chars: int, focus: str) -> str:
    digest = hashlib.sha1(f"{max_chars}\0{focus}\0{text}".encode("utf-8")).hexdigest()
    prompt = f"""\
Compress the text to no more than {max_chars} characters.

Focus: {focus}

Rules:
- Preserve exact numbers, units, IDs, block_id values, named places, and source-agent labels.
- Preserve development hypotheses and follow-up questions if present.
- Prefer dense factual bullets over prose.
- Do not add facts that are not in the text.
- Return only the compressed text.

Cache key: {digest}

Text:
{text[:12000]}
"""
    try:
        response = get_chat_model(temperature=0.0).invoke(prompt)
        compressed = response.content if hasattr(response, "content") else str(response)
        compressed = compressed.strip()
        if compressed:
            return compressed[:max_chars]
    except Exception:
        pass
    return _soft_truncate(text, max_chars)


def _soft_truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head_budget = max(0, int(max_chars * 0.75) - 20)
    tail_budget = max(0, max_chars - head_budget - 20)
    return f"{text[:head_budget].rstrip()} ... {text[-tail_budget:].lstrip()}"
