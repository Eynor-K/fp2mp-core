"""
Web search tools for WebSearchAgent and NormativeAgent.

Primary: Tavily (if API key configured).
Fallback: DuckDuckGo (no key required).
"""

from __future__ import annotations

from functools import lru_cache

import httpx
from langchain_core.tools import tool

from fp2mp_core.config import get_settings
from fp2mp_core.llm import get_chat_model

_FETCH_TIMEOUT = 15
_DISTILL_CHARS = 9000


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
        lines.append(
            f"{i}. **{title}**\n"
            f"   URL: {url}\n"
            f"   Snippet: {snippet}\n"
            "   Deep-read: call research_url_tool on this URL only if it is among "
            "the top 1-3 most relevant results.\n"
        )
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
    original_url = url
    url = _normalize_url(url)
    if not url:
        return f"Failed to fetch {original_url}: empty or invalid URL."

    try:
        html = _fetch_html(url)
        text = _extract_main_text(html, url)
        return text[:3000]
    except Exception as exc:
        return f"Failed to fetch {url}: {exc}"


@tool
def research_url_tool(url: str, focus: str = "") -> str:
    """
    Fetch a URL, extract main content, and distill it for the given focus/sub-query.
    Use this on only the top 1-3 search results. Returns claims, numbers, quotes,
    source_url, and limitations instead of raw page text.
    """
    normalized_url = _normalize_url(url)
    if not normalized_url:
        return f"Failed to research {url}: empty or invalid URL."

    try:
        html = _fetch_html(normalized_url)
        text = _extract_main_text(html, normalized_url)
    except Exception as exc:
        return (
            f"Research fetch failed for {normalized_url}: {exc}\n"
            "Fallback: use the search result snippet only and mark confidence <= 0.5."
        )

    if not text.strip():
        return (
            f"Research extraction returned no text for {normalized_url}.\n"
            "Fallback: use the search result snippet only and mark confidence <= 0.5."
        )

    return _distill_text_for_focus(normalized_url, text[:_DISTILL_CHARS], focus)


def _normalize_url(url: str) -> str:
    url = str(url or "").strip().strip("'\"").strip()
    if url.startswith("[") and url.endswith("]"):
        url = url[1:-1].strip().strip("'\"").strip()
    if url.startswith("<") and url.endswith(">"):
        url = url[1:-1].strip()
    if url.startswith("`") and url.endswith("`"):
        url = url.strip("`").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if "://" in url:
        return ""
    return "https://" + url


@lru_cache(maxsize=128)
def _fetch_html(url: str) -> str:
    resp = httpx.get(url, timeout=_FETCH_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _extract_main_text(html: str, url: str = "") -> str:
    try:
        import trafilatura  # type: ignore[import-untyped]

        extracted = trafilatura.extract(html, url=url, include_comments=False, include_tables=True)
        if extracted:
            return extracted
    except Exception:
        pass

    try:
        from readability import Document  # type: ignore[import-untyped]

        summary_html = Document(html).summary()
        return _bs4_text(summary_html)
    except Exception:
        return _bs4_text(html)


def _bs4_text(html: str) -> str:
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


@lru_cache(maxsize=256)
def _distill_text_for_focus(url: str, text: str, focus: str) -> str:
    focus_text = focus or "the current research question"
    prompt = f"""\
Focus/sub-question:
{focus_text}

Source URL:
{url}

Extracted page text:
{text}

Distill only information relevant to the focus. Return concise markdown with:
- Key claims relevant to the focus
- Numbers / dates / named entities, if present
- 1-3 short direct quotes with quotation marks
- source_url: {url}
- Limitations / uncertainty

Keep the answer under 800 tokens. Do not include unrelated navigation or boilerplate.
"""
    try:
        llm = get_chat_model(temperature=0.0, max_tokens=1200)
        response = llm.invoke([
            {
                "role": "system",
                "content": "You distill fetched web pages into evidence notes for research.",
            },
            {"role": "user", "content": prompt},
        ])
        content = response.content if hasattr(response, "content") else str(response)
        return str(content)[:4000]
    except Exception:
        return _extractive_distill(url, text, focus_text)


def _extractive_distill(url: str, text: str, focus: str) -> str:
    terms = {term.lower() for term in focus.split() if len(term) > 3}
    sentences = [s.strip() for s in text.replace("\n", " ").split(".") if s.strip()]
    ranked = sorted(
        sentences,
        key=lambda s: sum(1 for term in terms if term in s.lower()),
        reverse=True,
    )
    excerpts = ranked[:5] or sentences[:5]
    lines = ["Key claims relevant to the focus:"]
    lines.extend(f"- {excerpt[:300]}" for excerpt in excerpts)
    lines.append(f"source_url: {url}")
    lines.append("Limitations / uncertainty: extractive fallback; verify against full source.")
    return "\n".join(lines)[:4000]
