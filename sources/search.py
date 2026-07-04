"""
Seed URL discovery from multiple sources:
  - DuckDuckGo via web4agent (English + Chinese)
  - Bing HTML scraper (good for Chinese-language content)
  - arXiv Atom API (academic PDFs, English + translated fallback)
"""

import logging
import re
import unicodedata
import urllib.parse
from typing import Any

import httpx

log = logging.getLogger(__name__)

_SEARCH_TIMEOUT = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ---------------------------------------------------------------------------
# DuckDuckGo — via web4agent (handles JS challenge automatically)
# ---------------------------------------------------------------------------

async def get_seed_urls(keyword: str, max_results: int = 20) -> list[dict[str, Any]]:
    """Search DuckDuckGo; each result dict has url / title / content / success."""
    try:
        from web4agent import agent_search
        result = await agent_search(keyword, max_results=max_results)
        if isinstance(result, dict):
            return result.get("results", [])
        if isinstance(result, list):
            return result
        return []
    except Exception as exc:
        log.warning("DuckDuckGo search failed for '%s': %s", keyword, exc)
        return []


# ---------------------------------------------------------------------------
# Bing — HTML scraper (good coverage, less strict than Google)
# ---------------------------------------------------------------------------

async def get_bing_urls(keyword: str, max_results: int = 20) -> list[str]:
    """
    Scrape Bing search results for `keyword`.
    Returns a list of result page URLs (not Bing redirect URLs).
    """
    query = urllib.parse.quote(keyword)
    search_url = f"https://www.bing.com/search?q={query}&count={max_results}&setlang=zh-CN"
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=_SEARCH_TIMEOUT,
            headers=_HEADERS,
        ) as client:
            resp = await client.get(search_url)
            resp.raise_for_status()

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        urls: list[str] = []
        for a in soup.select("li.b_algo h2 a, .b_title a"):
            href = a.get("href", "")
            if href.startswith("http") and "bing.com" not in href and "microsoft.com" not in href:
                urls.append(href)
                if len(urls) >= max_results:
                    break
        log.info("Bing: found %d URLs for '%s'", len(urls), keyword)
        return urls
    except Exception as exc:
        log.warning("Bing search failed for '%s': %s", keyword, exc)
        return []


# ---------------------------------------------------------------------------
# arXiv Atom API — academic PDFs, no auth required
# ---------------------------------------------------------------------------

_CHINESE_KEYWORD_TRANSLATIONS: dict[str, list[str]] = {
    "航空发动机": ["aero engine", "aircraft engine", "turbofan engine", "jet engine"],
    "涡扇发动机": ["turbofan engine", "turbofan"],
    "涡轮发动机": ["turbine engine", "gas turbine"],
    "火箭发动机": ["rocket engine", "rocket propulsion"],
    "发动机": ["engine", "propulsion"],
}


def _is_chinese(text: str) -> bool:
    return any(unicodedata.category(c) == "Lo" and "一" <= c <= "鿿" for c in text)


def _english_equivalents(keyword: str) -> list[str]:
    """Return English search terms for a Chinese keyword, using a lookup table."""
    for cn, translations in _CHINESE_KEYWORD_TRANSLATIONS.items():
        if cn in keyword:
            return translations
    return []


async def _fetch_arxiv(query: str, max_results: int) -> list[str]:
    """Fetch PDFs from arXiv for a single query string."""
    encoded = urllib.parse.quote(query)
    api_url = (
        f"https://export.arxiv.org/api/query"
        f"?search_query=all:{encoded}&max_results={max_results}&sortBy=relevance"
    )
    async with httpx.AsyncClient(timeout=_SEARCH_TIMEOUT) as client:
        resp = await client.get(api_url)
        resp.raise_for_status()

    pdf_urls = re.findall(
        r'<link[^>]+title=["\']pdf["\'][^>]+href=["\']([^"\']+)["\']',
        resp.text,
    )
    pdf_urls += re.findall(
        r'<link[^>]+href=["\']([^"\']+)["\'][^>]+title=["\']pdf["\']',
        resp.text,
    )
    seen: set[str] = set()
    result: list[str] = []
    for u in pdf_urls:
        u = u.replace("/abs/", "/pdf/")
        if not u.endswith(".pdf"):
            u += ".pdf"
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


async def get_arxiv_urls(keyword: str, max_results: int = 20) -> list[str]:
    """
    Query arXiv API and return direct PDF download URLs.
    For Chinese keywords, also searches English equivalents as fallback.
    """
    try:
        result = await _fetch_arxiv(keyword, max_results)
        log.info("arXiv: found %d PDFs for '%s'", len(result), keyword)

        if not result and _is_chinese(keyword):
            english_terms = _english_equivalents(keyword)
            if not english_terms:
                english_terms = [keyword]
            for en_term in english_terms:
                try:
                    en_results = await _fetch_arxiv(en_term, max_results)
                    if en_results:
                        log.info(
                            "arXiv fallback '%s': found %d PDFs", en_term, len(en_results)
                        )
                        result.extend(en_results)
                        if len(result) >= max_results:
                            break
                except Exception as exc:
                    log.warning("arXiv fallback '%s' failed: %s", en_term, exc)

        seen: set[str] = set()
        deduped: list[str] = []
        for u in result:
            if u not in seen:
                seen.add(u)
                deduped.append(u)
        return deduped[:max_results]
    except Exception as exc:
        log.warning("arXiv search failed for '%s': %s", keyword, exc)
        return []
