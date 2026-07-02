import logging
from typing import Any

log = logging.getLogger(__name__)


async def get_seed_urls(keyword: str, max_results: int = 20) -> list[dict[str, Any]]:
    """
    Search the web for the keyword and return a list of result dicts.
    Each dict has at minimum: url, title, content, success.
    Falls back to an empty list on any error.
    """
    try:
        from web4agent import agent_search

        result = await agent_search(keyword, max_results=max_results)
        if isinstance(result, dict):
            return result.get("results", [])
        if isinstance(result, list):
            return result
        return []
    except Exception as exc:
        log.warning("Search failed for '%s': %s", keyword, exc)
        return []


async def get_arxiv_urls(keyword: str, max_results: int = 20) -> list[str]:
    """
    Query the arXiv Atom API and return PDF download URLs.
    No API key required.
    """
    import urllib.parse

    import httpx

    query = urllib.parse.quote(keyword)
    api_url = (
        f"http://export.arxiv.org/api/query"
        f"?search_query=all:{query}&max_results={max_results}&sortBy=relevance"
    )
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(api_url)
            resp.raise_for_status()
        # Extract PDF links from Atom XML
        import re

        pdf_urls = re.findall(
            r'<link[^>]+title="pdf"[^>]+href="([^"]+)"', resp.text
        )
        # arXiv returns abstract links; convert to PDF
        converted = []
        for u in pdf_urls:
            converted.append(u.replace("/abs/", "/pdf/") + ".pdf" if "/abs/" in u else u)
        log.info("arXiv: found %d PDFs for '%s'", len(converted), keyword)
        return converted
    except Exception as exc:
        log.warning("arXiv search failed for '%s': %s", keyword, exc)
        return []
