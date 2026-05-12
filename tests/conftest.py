"""Shared test fixtures."""

from __future__ import annotations

import os

import pytest

# Provide a dummy API key so config loading doesn't fail in tests
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key-for-testing-only")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test")


from fp2mp_core.state import (
    BlackBoard,
    ConfirmedFact,
    CritiqueResult,
    RawEntry,
    SubQuery,
    Task,
    WikiPage,
    board_message,
    create_initial_state,
)


@pytest.fixture
def question():
    return "Можно ли строить жилой дом 9 этажей в 500м от аэропорта?"


@pytest.fixture
def initial_state(question):
    return create_initial_state(question, max_iterations=3)


@pytest.fixture
def sub_queries():
    return [
        SubQuery(
            sub_query_id="sq_001",
            text="Какие нормы регулируют строительство вблизи аэропортов?",
            intent_aspect="regulatory constraint",
            search_modality="normative",
            independence=True,
            enriched_variants=["строительные нормы аэропорт", "СНиП аэропорт ограничения"],
            keywords=["аэропорт", "строительство", "норматив", "ограничение"],
            domain_hints=["СНиП", "ГОСТ", "воздушный кодекс"],
        ),
        SubQuery(
            sub_query_id="sq_002",
            text="Какова типичная высота разрешённых построек в 500м от аэропорта?",
            intent_aspect="empirical data",
            search_modality="web",
            independence=True,
            enriched_variants=["высота зданий ограничение аэропорт"],
            keywords=["высота", "500м", "аэропорт"],
            domain_hints=[],
        ),
    ]


@pytest.fixture
def raw_entries(sub_queries):
    return [
        board_message(
            agent="WebSearchAgent",
            iteration=1,
            msg_type="web_findings",
            content="Согласно общим правилам, вблизи аэропортов действуют ограничения высоты.",
            sub_query_id="sq_002",
            confidence=0.7,
            citations=[{"url": "https://example.com/airport-rules", "title": "Airport rules"}],
        ),
        board_message(
            agent="NormativeAgent",
            iteration=1,
            msg_type="normative_findings",
            content="СНиП 32-01-95 устанавливает ограничительные зоны. Раздел 4.2: высота до 50м.",
            sub_query_id="sq_001",
            confidence=0.85,
            citations=[{"document": "СНиП 32-01-95", "section": "4.2"}],
        ),
    ]


@pytest.fixture
def wiki_pages():
    return {
        "index.md": WikiPage(
            page_id="index.md",
            title="Knowledge Index",
            content="# Knowledge Index\n",
            updated_by="system",
            updated_at_iteration=0,
            confidence=1.0,
            citations=[],
            tags=["system"],
            incoming_cross_refs=[],
            relevance_score=1.0,
        ),
        "log.md": WikiPage(
            page_id="log.md",
            title="Change Log",
            content="# Change Log\n",
            updated_by="system",
            updated_at_iteration=0,
            confidence=1.0,
            citations=[],
            tags=["system"],
            incoming_cross_refs=[],
            relevance_score=1.0,
        ),
    }
