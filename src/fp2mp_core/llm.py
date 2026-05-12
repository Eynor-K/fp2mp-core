"""LLM factory — all model instantiation goes through here."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import anthropic
import httpx
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from fp2mp_core.config import get_settings


@lru_cache(maxsize=1)
def _openrouter_http_client() -> httpx.Client:
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
    mid = model_id or settings.model_default
    if settings.llm_provider == "openrouter":
        return ChatOpenAI(
            model=mid,
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            http_client=_openrouter_http_client(),
            disable_streaming=True,
        )

    return ChatAnthropic(
        model=mid,
        temperature=temperature,
        max_tokens=max_tokens,
        anthropic_api_key=settings.anthropic_api_key,
    )


@lru_cache(maxsize=1)
def get_anthropic_client() -> anthropic.Anthropic:
    """Raw Anthropic SDK client for extended thinking (Mediator, Critic)."""
    settings = get_settings()
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def get_thinking_model_id() -> str:
    return get_settings().model_thinking


def get_default_model_id() -> str:
    return get_settings().model_default


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


def _thinking_model_is_claude() -> bool:
    """Check if the thinking model is a Claude model (supports Anthropic extended thinking)."""
    mid = get_thinking_model_id().lower()
    return mid.startswith("claude-") or mid.startswith("anthropic/claude-")


def call_with_thinking(
    prompt: str,
    system: str = "",
    budget_tokens: int = 5000,
    max_tokens: int = 8000,
    extra_messages: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """
    Call the thinking model and return (thinking_text, answer_text).

    Priority:
    1. Anthropic SDK with extended thinking — if ANTHROPIC_API_KEY is set AND model is Claude
       (works regardless of whether main LLM_PROVIDER is 'anthropic' or 'openrouter')
    2. OpenRouter / regular chat fallback — when Anthropic key is absent or model is non-Claude
       (no extended thinking, but functionally equivalent prompt-based reasoning)
    """
    settings = get_settings()

    # Use Anthropic SDK directly if possible (extended thinking)
    if _thinking_model_is_claude() and settings.anthropic_api_key:
        client = get_anthropic_client()
        # Strip "anthropic/" prefix if coming from OpenRouter model id convention
        model_id = get_thinking_model_id()
        if model_id.startswith("anthropic/"):
            model_id = model_id[len("anthropic/"):]

        sdk_messages: list[dict[str, Any]] = list(extra_messages or [])
        sdk_messages.append({"role": "user", "content": prompt})
        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "thinking": {"type": "enabled", "budget_tokens": budget_tokens},
            "messages": sdk_messages,
        }
        if system:
            kwargs["system"] = system
        response = client.messages.create(**kwargs)
        thinking_text = ""
        answer_text = ""
        for block in response.content:
            if block.type == "thinking":
                thinking_text = block.thinking
            elif block.type == "text":
                answer_text = block.text
        return thinking_text, answer_text

    # Fallback: regular chat call via configured provider (no extended thinking)
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(extra_messages or [])
    messages.append({"role": "user", "content": prompt})

    llm = get_chat_model(model_id=get_thinking_model_id(), temperature=0.0, max_tokens=max_tokens)
    response = llm.invoke(messages)
    return "", _content_to_text(response.content)
