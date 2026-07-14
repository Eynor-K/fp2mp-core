"""Tests for the shared no-tools LLM helper."""

from unittest.mock import MagicMock, patch

from fp2mp_core.nodes.agents.toolless import run_toolless


def test_run_toolless_invokes_chat_model_once_with_system_and_question():
    response = MagicMock(content="ANSWER: direct answer")
    model = MagicMock()
    model.invoke.return_value = response

    with patch("fp2mp_core.nodes.agents.toolless.get_chat_model", return_value=model):
        result = run_toolless("system instruction", "user question", temperature=0.2)

    assert result == "ANSWER: direct answer"
    model.invoke.assert_called_once_with([
        {"role": "system", "content": "system instruction"},
        {"role": "user", "content": "Question: user question"},
    ])
