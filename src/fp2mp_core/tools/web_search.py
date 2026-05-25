"""
Web search tools for WebSearchAgent and NormativeAgent.

Primary: Tavily (if API key configured).
Fallback: DuckDuckGo (no key required).
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from langchain_core.tools import tool

from fp2mp_core.config import get_settings
from fp2mp_core.llm import get_chat_model
from fp2mp_core.runtime import get_run_context, record_file

_FETCH_TIMEOUT = 15
_DISTILL_CHARS = 9000
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_BAD_SEARCH_MARKERS = (
    "Search error",
    "unsupported_country_region_territory",
    "request_forbidden",
    "error code: 403",
    "403",
)


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

    if _looks_like_bad_results(results):
        rewritten = _rewrite_query(query)
        if rewritten != query:
            results = _ddg_search(rewritten, max_results)

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
    if _is_cyrillic_query(query):
        enriched_query = query + " нормативный документ стандарт закон требование"
    else:
        enriched_query = query + " official regulation standard guidance policy requirements"
    return web_search_tool.invoke({"query": enriched_query, "max_results": max_results})  # type: ignore[arg-type]


def _is_cyrillic_query(query: str) -> bool:
    return bool(_CYRILLIC_RE.search(query or ""))


def _looks_like_bad_results(results: list[dict]) -> bool:
    if not results:
        return True
    useful = 0
    for result in results:
        title = str(result.get("title", ""))
        url = str(result.get("url") or result.get("href") or "")
        content = str(result.get("content") or result.get("body") or "")
        haystack = f"{title}\n{url}\n{content}".lower()
        if any(marker.lower() in haystack for marker in _BAD_SEARCH_MARKERS):
            continue
        if url.startswith("http") and (title.strip() or content.strip()):
            useful += 1
    return useful == 0


def _rewrite_query(query: str) -> str:
    """Fallback rewrite that avoids locale drift and adds official-source intent."""
    cleaned = " ".join(str(query or "").split())
    if not cleaned:
        return cleaned
    if _is_cyrillic_query(cleaned):
        return f"{cleaned} официальные источники документы данные"
    return f"{cleaned} official sources data policy"


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
        settings = get_settings()
        if settings.save_web_images:
            save_page_images(html, url, settings.max_web_images_per_page)
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
        settings = get_settings()
        if settings.save_web_images:
            save_page_images(html, normalized_url, settings.max_web_images_per_page)
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


def save_page_images(html: str, base_url: str, max_images: int = 3) -> list[dict]:
    """Save relevant images from a fetched page into the current run images folder."""
    ctx = get_run_context()
    if not ctx or max_images <= 0:
        return []
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-untyped]

        soup = BeautifulSoup(html, "html.parser")
        candidates: list[tuple[str, str]] = []
        for meta in soup.find_all("meta"):
            prop = str(meta.get("property") or meta.get("name") or "").lower()
            if prop in {"og:image", "twitter:image"} and meta.get("content"):
                candidates.append((str(meta.get("content")), ""))
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if src:
                candidates.append((str(src), str(img.get("alt") or "")))

        saved: list[dict] = []
        seen: set[str] = set()
        base_host = urlparse(base_url).netloc
        for src, alt in candidates:
            if len(saved) >= max_images:
                break
            img_url = urljoin(base_url, src)
            if img_url in seen or _skip_image_url(img_url):
                continue
            seen.add(img_url)
            parsed = urlparse(img_url)
            if parsed.scheme in {"http", "https"} and parsed.netloc != base_host:
                continue
            item = _download_or_decode_image(img_url, base_url, alt)
            if item:
                saved.append(item)
        return saved
    except Exception:
        return []


def _download_or_decode_image(img_url: str, page_url: str, alt: str) -> dict | None:
    ctx = get_run_context()
    if not ctx:
        return None
    try:
        if img_url.startswith("data:image/"):
            header, encoded = img_url.split(",", 1)
            ext = "." + header.split("/", 1)[1].split(";", 1)[0]
            content = base64.b64decode(encoded)
            source = "data:image"
        elif img_url.startswith("file://"):
            path = urlparse(img_url).path
            content = Path(path).read_bytes()  # type: ignore[name-defined]
            ext = Path(path).suffix or ".png"  # type: ignore[name-defined]
            source = img_url
        else:
            resp = httpx.get(img_url, timeout=10, follow_redirects=True, headers={"User-Agent": "fp2mp-core/1.0"})
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if not ctype.startswith("image/"):
                return None
            content = resp.content
            if len(content) < 2048:
                return None
            ext = mimetypes.guess_extension(ctype.split(";", 1)[0]) or Path(urlparse(img_url).path).suffix or ".img"  # type: ignore[name-defined]
            source = img_url
        digest = hashlib.sha256(img_url.encode("utf-8")).hexdigest()[:12]
        path = ctx.images_dir / f"{digest}{ext}"
        path.write_bytes(content)
        record_file(path, "web_image", source=source, meta={"page": page_url, "alt": alt})
        return {"path": str(path), "source": source, "page": page_url, "alt": alt}
    except Exception:
        return None


def _skip_image_url(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in ("sprite", "icon", "logo", "placeholder", "spacer"))


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
