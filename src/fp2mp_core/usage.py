"""Run-scoped LLM token usage accounting."""

from __future__ import annotations

from typing import Any

import tiktoken
from langchain_core.callbacks import BaseCallbackHandler

_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: Any) -> int:
    """Best-effort cl100k token count for fallback accounting."""
    try:
        return len(_enc.encode(_content_to_text(text)))
    except Exception:
        return 0


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


class TokenUsageAccumulator(BaseCallbackHandler):
    """Collect real token usage when available, otherwise estimate it from prompts."""

    def __init__(self) -> None:
        self.real_input = 0
        self.real_output = 0
        self.real_total = 0
        self.est_input = 0
        self.est_output = 0
        self.calls = 0

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        **kwargs: Any,
    ) -> None:
        self.calls += 1
        for batch in messages:
            for message in batch:
                self.est_input += count_tokens(getattr(message, "content", message))

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        self.calls += 1
        self.est_input += sum(count_tokens(prompt) for prompt in prompts)

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        if not self._add_real_usage_from_generations(response):
            self._add_real_usage_from_llm_output(response)
        self.est_output += self._estimate_output(response)

    def totals(self) -> dict[str, int | str]:
        if self.real_total > 0:
            return {
                "input_tokens": self.real_input,
                "output_tokens": self.real_output,
                "total_tokens": self.real_total,
                "source": "real",
            }

        total = self.est_input + self.est_output
        return {
            "input_tokens": self.est_input,
            "output_tokens": self.est_output,
            "total_tokens": total,
            "source": "estimate",
        }

    def _add_real_usage_from_generations(self, response: Any) -> bool:
        found = False
        for batch in getattr(response, "generations", []) or []:
            for generation in batch or []:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None) if message is not None else None
                found = self._add_usage_dict(usage) or found
        return found

    def _add_real_usage_from_llm_output(self, response: Any) -> None:
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        self._add_usage_dict(usage)

    def _add_usage_dict(self, usage: Any) -> bool:
        if not isinstance(usage, dict):
            return False

        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
        total_tokens = usage.get("total_tokens", 0) or 0
        if not total_tokens:
            total_tokens = input_tokens + output_tokens
        if not total_tokens:
            return False

        self.real_input += int(input_tokens)
        self.real_output += int(output_tokens)
        self.real_total += int(total_tokens)
        return True

    def _estimate_output(self, response: Any) -> int:
        total = 0
        for batch in getattr(response, "generations", []) or []:
            for generation in batch or []:
                message = getattr(generation, "message", None)
                if message is not None:
                    total += count_tokens(getattr(message, "content", ""))
                else:
                    total += count_tokens(getattr(generation, "text", ""))
        return total
