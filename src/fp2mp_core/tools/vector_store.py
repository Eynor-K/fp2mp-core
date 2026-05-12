"""
Normative document RAG: Chroma vector store + LangChain retriever tool.

Documents in data/normative/ (.pdf, .txt) are ingested lazily on first call.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from langchain_core.tools import tool

from fp2mp_core.config import get_settings

_COLLECTION_NAME = "normative_docs"
_CHUNK_SIZE = 800
_CHUNK_OVERLAP = 100
_TOP_K = 4


def _load_documents(normative_path: Path) -> list:
    """Load .txt and .pdf files from the normative directory."""
    from langchain_community.document_loaders import PyPDFLoader, TextLoader  # type: ignore

    docs = []
    for path in normative_path.rglob("*"):
        if path.suffix.lower() == ".txt":
            try:
                loader = TextLoader(str(path), encoding="utf-8")
                docs.extend(loader.load())
            except Exception:
                pass
        elif path.suffix.lower() == ".pdf":
            try:
                loader = PyPDFLoader(str(path))
                docs.extend(loader.load())
            except Exception:
                pass
    return docs


@lru_cache(maxsize=1)
def _get_vector_store():
    """Build or load the Chroma vector store (cached for process lifetime)."""
    import chromadb  # type: ignore
    from langchain_chroma import Chroma  # type: ignore
    from langchain_community.embeddings import HuggingFaceEmbeddings  # type: ignore
    from langchain_text_splitters import RecursiveCharacterTextSplitter  # type: ignore

    settings = get_settings()
    norm_path = settings.normative_db_path

    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )

    if not norm_path.exists() or not any(norm_path.rglob("*")):
        # Empty normative dir → return empty store that returns nothing
        client = chromadb.EphemeralClient()
        return Chroma(
            client=client,
            collection_name=_COLLECTION_NAME,
            embedding_function=embeddings,
        )

    docs = _load_documents(norm_path)
    if not docs:
        client = chromadb.EphemeralClient()
        return Chroma(
            client=client,
            collection_name=_COLLECTION_NAME,
            embedding_function=embeddings,
        )

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=_CHUNK_SIZE,
        chunk_overlap=_CHUNK_OVERLAP,
    )
    chunks = splitter.split_documents(docs)

    return Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        collection_name=_COLLECTION_NAME,
    )


@tool
def normative_vector_search_tool(query: str, k: int = 4) -> str:
    """
    Search local normative documents (regulations, standards, SNiP, GOST, etc.)
    using semantic similarity. Returns relevant excerpts with source references.
    """
    try:
        store = _get_vector_store()
        results = store.similarity_search_with_relevance_scores(query, k=k)
        if not results:
            return "No relevant normative documents found in local database."

        lines = []
        for i, (doc, score) in enumerate(results, 1):
            source = doc.metadata.get("source", "Unknown source")
            page = doc.metadata.get("page", "")
            page_str = f" (page {page})" if page else ""
            lines.append(
                f"{i}. [Score: {score:.3f}] **{source}{page_str}**\n{doc.page_content[:500]}\n"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Vector search error: {exc}"
