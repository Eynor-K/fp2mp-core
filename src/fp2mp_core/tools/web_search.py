"""
Web search tools for WebSearchAgent and NormativeAgent.

Primary: Tavily (if API key configured).
Fallback: DuckDuckGo (no key required).
"""

from __future__ import annotations

import httpx
from langchain_core.tools import tool

from fp2mp_core.config import get_settings

_FETCH_TIMEOUT = 15


def _get_tavily_client():
    try:
        from tavily import TavilyClient  # type: ignore[import-untyped]
        settings = get_settings()
        if settings.tavily_api_key:
            return TavilyClient(api_key=settings.tavily_api_key)
    except ImportError:
        pass
    return None


def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    try:
        try:
            from ddgs import DDGS  # type: ignore[import-untyped]
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore[import-untyped]
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return [
            {"title": r.get("title", ""), "url": r.get("href", ""), "content": r.get("body", "")}
            for r in results
        ]
    except Exception as exc:
        return [{"title": "Search error", "url": "", "content": str(exc)}]


@tool
def web_search_tool(query: str, max_results: int = 5) -> str:
    """
    Search the internet for information about the given query.
    Returns a formatted string with titles, URLs, and snippets.
    """
    client = _get_tavily_client()
    if client:
        try:
            response = client.search(query, max_results=max_results, search_depth="advanced")
            results = response.get("results", [])
        except Exception:
            results = _ddg_search(query, max_results)
    else:
        results = _ddg_search(query, max_results)

    if not results:
        return "No results found."

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        url = r.get("url", "")
        snippet = r.get("content", r.get("body", ""))[:400]
        lines.append(f"{i}. **{title}**\n   URL: {url}\n   {snippet}\n")
    return "\n".join(lines)


@tool
def normative_web_search_tool(query: str, max_results: int = 5) -> str:
    """
    Search for regulatory documents, standards, laws, and normative literature.
    Automatically appends domain hints to improve precision.
    """
    enriched_query = query + " нормативный документ стандарт закон требование"
    return web_search_tool.invoke({"query": enriched_query, "max_results": max_results})  # type: ignore[arg-type]


@tool
def fetch_url_tool(url: str) -> str:
    """
    Fetch and return the text content of a web page (first 3000 characters).
    Use to verify quotes or get full document context after a search.
    """
    try:
        resp = httpx.get(url, timeout=_FETCH_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        # Simple HTML stripping
        from bs4 import BeautifulSoup  # type: ignore[import-untyped]
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(separator="\n", strip=True)
        return text[:3000]
    except Exception as exc:
        return f"Failed to fetch {url}: {exc}"
