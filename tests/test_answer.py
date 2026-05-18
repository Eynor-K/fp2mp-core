"""Tests for the fail-open committed-answer drafter and synthesis formatter."""

from __future__ import annotations

from unittest.mock import patch

from fp2mp_core.nodes.answer import (
    _fallback_draft,
    _looks_like_refusal,
    answer_commit_node,
)
from fp2mp_core.nodes.synthesis import _COMMON_RULES, _get_synthesis_system


def test_refusal_detector_flags_refusals_not_commitments():
    assert _looks_like_refusal("I'm unable to provide a specific answer")
    assert _looks_like_refusal("Insufficient evidence to answer this question.")
    assert _looks_like_refusal("Не могу дать ответ из-за нехватки данных")
    # A committed concrete answer must NOT be flagged.
    assert not _looks_like_refusal(
        "Build the station at District X (block 1423). Confidence: medium."
    )


def test_fallback_draft_is_never_empty():
    assert _fallback_draft("Where to build X?", []).strip()
    with_facts = _fallback_draft("Q?", [{"claim": "Site A is best", "confidence": 0.8}])
    assert "Site A is best" in with_facts


def test_answer_commit_returns_committed_draft_on_llm_failure():
    with patch("fp2mp_core.nodes.answer.get_chat_model", side_effect=RuntimeError("no api")):
        out = answer_commit_node({"question": "Where to build X?", "output": [], "critique": {}})
    assert out["draft_answer"].strip()
    assert not _looks_like_refusal(out["draft_answer"])


def test_answer_commit_replaces_refusal_with_fallback():
    class _Resp:
        content = "I am unable to answer due to insufficient data."

    class _LLM:
        def invoke(self, *_a, **_k):
            return _Resp()

    with patch("fp2mp_core.nodes.answer.get_chat_model", return_value=_LLM()):
        out = answer_commit_node({"question": "Q?", "output": [], "critique": {}})
    assert not _looks_like_refusal(out["draft_answer"])


def test_synthesis_is_fail_open_not_fail_closed():
    assert "FAIL CLOSED" not in _COMMON_RULES
    assert "FAIL OPEN" in _COMMON_RULES
    for intent in ("planning", "analytical", "regulatory", "factual"):
        sys = _get_synthesis_system(intent)
        assert "Open Questions" not in sys
        assert "Committed draft" in sys or "committed" in sys.lower()
