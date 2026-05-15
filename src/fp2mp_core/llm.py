"""LLM factory — all model instantiation goes through here."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from fp2mp_core.config import get_settings


_active_model: str | None = None


def set_active_model(model: str) -> None:
    global _active_model
    _active_model = model
    get_chat_model.cache_clear()


@lru_cache(maxsize=1)
def _http_client() -> httpx.Client:
    return httpx.Client(
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        timeout=httpx.Timeout(60.0),
    )


@lru_cache(maxsize=4)
def get_chat_model(
    model_id: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 4096,
) -> BaseChatModel:
    """Return a LangChain chat model. Cached by (model_id, temperature, max_tokens)."""
    settings = get_settings()
    mid = model_id or _active_model or "gpt-4o-mini"
    return ChatOpenAI(
        model=mid,
        temperature=temperature,
        max_tokens=max_tokens,
        api_key=settings.api_key,
        base_url=settings.chat_url,
        http_client=_http_client(),
        disable_streaming=True,
    )


def get_default_model_id() -> str:
    return _active_model or "gpt-4o-mini"


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


def call_with_thinking(
    prompt: str,
    system: str = "",
    budget_tokens: int = 5000,
    max_tokens: int = 8000,
    extra_messages: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Call the active chat model and return (thinking_text, answer_text)."""
    _ = budget_tokens
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(extra_messages or [])
    messages.append({"role": "user", "content": prompt})

    llm = get_chat_model(temperature=0.0, max_tokens=max_tokens)
    response = llm.invoke(messages)
    return "", _content_to_text(response.content)
