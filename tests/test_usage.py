"""Tests for run-scoped token usage accounting."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, Generation, LLMResult

from fp2mp_core.usage import TokenUsageAccumulator


def test_build_log_places_usage_only_on_final_message():
    from fp2mp_core.graph import _build_log

    usage_metadata = {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "source": "real",
    }

    log = _build_log(
        "question",
        [{"agent": "Agent", "iteration": 1, "entry_id": "e1", "content": "agent output"}],
        "final answer",
        usage_metadata=usage_metadata,
    )

    assert log[-1].name == "FinalSynthesis"
    assert log[-1].usage_metadata == usage_metadata
    assert all(getattr(message, "usage_metadata", None) is None for message in log[1:-1])


def test_token_usage_accumulator_prefers_real_usage_metadata():
    usage = TokenUsageAccumulator()
    usage.on_chat_model_start({}, [[HumanMessage(content="input text")]])
    usage.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="output text",
                            usage_metadata={
                                "input_tokens": 11,
                                "output_tokens": 7,
                                "total_tokens": 18,
                            },
                        )
                    )
                ]
            ]
        )
    )

    assert usage.calls == 1
    assert usage.totals() == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "source": "real",
    }


def test_token_usage_accumulator_falls_back_to_estimate():
    usage = TokenUsageAccumulator()
    usage.on_chat_model_start({}, [[HumanMessage(content="input text")]])
    usage.on_llm_end(LLMResult(generations=[[Generation(text="output text")]]))

    totals = usage.totals()

    assert usage.calls == 1
    assert totals["source"] == "estimate"
    assert totals["input_tokens"] > 0
    assert totals["output_tokens"] > 0
    assert totals["total_tokens"] == totals["input_tokens"] + totals["output_tokens"]
