"""Capability-card registry for BlocksNetAgent."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from langchain_core.tools import tool

from fp2mp_core.config import BASE_DIR

_KB_DIR = BASE_DIR / "data" / "blocksnet_kb"
_COLLECTION_NAME = "blocksnet_capabilities"


def _card_text(card: dict[str, Any]) -> str:
    parts = [
        card.get("id", ""),
        card.get("name", ""),
        card.get("when_to_use", ""),
        " ".join(card.get("tools", [])),
        card.get("prerequisites", ""),
        card.get("io_contract", ""),
        card.get("interpretation", ""),
        " ".join(card.get("pitfalls", [])),
    ]
    return "\n".join(str(part) for part in parts if part)


def _load_cards() -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    if not _KB_DIR.exists():
        return cards
    for path in sorted(_KB_DIR.glob("*.json")):
        try:
            cards.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return cards


@lru_cache(maxsize=1)
def _cards() -> tuple[dict[str, Any], ...]:
    return tuple(_load_cards())


@lru_cache(maxsize=1)
def _get_vector_store():
    import chromadb  # type: ignore
    from langchain_chroma import Chroma  # type: ignore
    from langchain_community.embeddings import HuggingFaceEmbeddings  # type: ignore
    from langchain_core.documents import Document

    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    docs = [
        Document(page_content=_card_text(card), metadata={"id": card.get("id", "")})
        for card in _cards()
    ]
    client = chromadb.EphemeralClient()
    if not docs:
        return Chroma(client=client, collection_name=_COLLECTION_NAME, embedding_function=embeddings)
    return Chroma.from_documents(
        documents=docs,
        embedding=embeddings,
        client=client,
        collection_name=_COLLECTION_NAME,
    )


def _keyword_retrieve(query: str, k: int) -> list[dict[str, Any]]:
    terms = {term.lower() for term in query.replace("_", " ").split() if len(term) > 2}
    scored = []
    for card in _cards():
        text = _card_text(card).lower()
        score = sum(1 for term in terms if term in text)
        scored.append((score, card))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [card for score, card in scored[:k] if score > 0] or list(_cards()[:k])


def retrieve_blocksnet_capabilities(query: str, k: int = 4) -> list[dict[str, Any]]:
    card_by_id = {card.get("id", ""): card for card in _cards()}
    try:
        store = _get_vector_store()
        docs = store.similarity_search(query, k=k)
        results = [card_by_id.get(doc.metadata.get("id", "")) for doc in docs]
        cards = [card for card in results if card]
        if cards:
            return cards
    except Exception:
        pass
    return _keyword_retrieve(query, k)


def format_blocksnet_cards(cards: list[dict[str, Any]]) -> str:
    blocks = []
    for card in cards:
        blocks.append(
            "\n".join([
                f"CARD {_escape_prompt_braces(card.get('id', ''))}: {_escape_prompt_braces(card.get('name', ''))}",
                f"When: {_escape_prompt_braces(card.get('when_to_use', ''))}",
                f"Tools: {_escape_prompt_braces(', '.join(card.get('tools', [])))}",
                f"Prerequisites: {_escape_prompt_braces(card.get('prerequisites', ''))}",
                f"I/O: {_escape_prompt_braces(card.get('io_contract', ''))}",
                f"Interpretation: {_escape_prompt_braces(card.get('interpretation', ''))}",
                f"Pitfalls: {_escape_prompt_braces('; '.join(card.get('pitfalls', [])))}",
            ])
        )
    return "\n\n".join(blocks)


def _escape_prompt_braces(value: Any) -> str:
    return str(value).replace("{", "{{").replace("}", "}}")


@tool
def find_blocksnet_capability(query: str) -> str:
    """Search BlocksNet workflow/tool capability cards when injected cards do not match."""
    return format_blocksnet_cards(retrieve_blocksnet_capabilities(query, k=3))
