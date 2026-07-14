"""Shared one-shot LLM execution for agent ablations without tools."""

from __future__ import annotations

from fp2mp_core.llm import get_chat_model


def run_toolless(system_prompt: str, question: str, temperature: float) -> str:
    """Answer one directive using only the configured chat model."""
    response = get_chat_model(temperature=temperature).invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Question: {question}"},
    ])
    return response.content if hasattr(response, "content") else str(response)
