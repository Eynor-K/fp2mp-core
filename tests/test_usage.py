"""Tests for run-scoped token usage accounting."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, Generation, LLMResult

from fp2mp_core.usage import TokenUsageAccumulator


def test_build_log_estimates_tokens_per_message():
    from fp2mp_core.graph import _build_log

    log = _build_log(
        "question",
        [{"agent": "Agent", "iteration": 1, "entry_id": "e1", "content": "agent output"}],
        "final answer",
    )

    # Final message is the synthesized answer.
    assert log[-1].name == "FinalSynthesis"

    # Every AIMessage carries a non-null per-message estimate with a positive
    # output count and a non-negative input count (no more null usage_metadata).
    ai_messages = [m for m in log if isinstance(m, AIMessage)]
    assert ai_messages, "expected at least one AIMessage in the reconstructed log"
    for message in ai_messages:
        usage = message.usage_metadata
        assert usage is not None
        assert usage["output_tokens"] > 0
        assert usage["input_tokens"] >= 0
        assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]

    # The final answer's input is attributed from the preceding prompt turn.
    assert log[-1].usage_metadata["input_tokens"] > 0


def test_run_reports_usage_separately_from_log(monkeypatch):
    """run() surfaces the authoritative run total in BaseState['usage'],
    while the log carries only per-message estimates (no run-level overlay)."""
    import fp2mp_core.graph as graph_module

    def fake_invoke(initial, config=None):
        # Emulate the accumulator capturing real backend usage during the run.
        for handler in (config or {}).get("callbacks", []):
            handler.real_input += 100
            handler.real_output += 40
            handler.real_total += 140
            handler.calls += 1
        return {"final_answer": "the answer", "raw_data": []}

    class FakeCompiled:
        invoke = staticmethod(fake_invoke)

    monkeypatch.setattr(graph_module, "build_graph", lambda: FakeCompiled())
    monkeypatch.setattr(graph_module, "set_active_model", lambda model: None)

    result = graph_module.run("a question", model="test-model")

    assert result["usage"] == {
        "input_tokens": 100,
        "output_tokens": 40,
        "total_tokens": 140,
        "source": "real",
    }
    # The log does not carry the run-level total — only per-message estimates.
    assert all(
        m.usage_metadata.get("source") is None
        for m in result["log"]
        if isinstance(m, AIMessage)
    )


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
