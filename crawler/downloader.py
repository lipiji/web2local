"""
Binary file downloader:
- curl_cffi AsyncSession pool (one per impersonation target) for connection reuse
- Realistic browser headers via HeaderBuilder
- Per-domain rate limiting + adaptive backoff
- cloudscraper fallback on Cloudflare 403/503
"""

import logging
from typing import Optional
from urllib.parse import urlparse

from config import EXT_CATEGORY
from crawler.stealth import (
    AdaptiveDomainRateLimiter,
    HeaderBuilder,
    cloudscraper_get,
    with_retry,
)

log = logging.getLogger(__name__)

_MIME_TO_EXT: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/epub+zip": ".epub",
    "text/plain": ".txt",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}

# Rotation pool — each entry keeps its own connection pool alive
_IMPERSONATE_POOL = ["chrome124", "chrome123", "chrome110", "edge99"]

_header_builder = HeaderBuilder()

# Shared sessions: one per impersonation target, created lazily
_sessions: dict[str, "AsyncSession"] = {}  # type: ignore[type-arg]
_sessions_lock: "asyncio.Lock | None" = None


def _get_sessions_lock():
    import asyncio
    global _sessions_lock
    if _sessions_lock is None:
        _sessions_lock = asyncio.Lock()
    return _sessions_lock


async def _get_session(impersonate: str):
    """Return a shared AsyncSession for the given impersonation target."""
    if impersonate not in _sessions:
        async with _get_sessions_lock():
            if impersonate not in _sessions:
                from curl_cffi.requests import AsyncSession  # type: ignore[import]
                _sessions[impersonate] = AsyncSession(impersonate=impersonate)
    return _sessions[impersonate]


def url_ext(url: str) -> str:
    path = urlparse(url).path
    last = path.rstrip("/").split("/")[-1]
    return ("." + last.rsplit(".", 1)[-1].lower()) if "." in last else ""


def classify_url(url: str) -> tuple[Optional[str], Optional[str]]:
    ext = url_ext(url)
    cat = EXT_CATEGORY.get(ext)
    return (ext, cat) if cat else (None, None)


def classify_mime(content_type: str) -> tuple[Optional[str], Optional[str]]:
    mime = content_type.split(";")[0].strip().lower()
    ext = _MIME_TO_EXT.get(mime)
    if not ext:
        return None, None
    cat = EXT_CATEGORY.get(ext)
    return (ext, cat) if cat else (None, None)


def _is_cloudflare(status: int, body: bytes) -> bool:
    snippet = body[:2048].lower()
    return status in {403, 503} and (
        b"cloudflare" in snippet or b"cf-ray" in snippet or b"just a moment" in snippet
    )


import random as _random


async def download_binary(
    url: str,
    timeout: int = 30,
    rate_limiter: Optional[AdaptiveDomainRateLimiter] = None,
    referer: Optional[str] = None,
) -> tuple[bytes, str, str]:
    """
    Download a binary file.
    Strategy: shared curl_cffi session (TLS impersonation) → cloudscraper fallback.
    Returns (content_bytes, resolved_ext, content_type_header).
    """
    if rate_limiter:
        await rate_limiter.wait(url)

    impersonate = _random.choice(_IMPERSONATE_POOL)
    headers = _header_builder.build(binary=True, referer=referer)

    async def _fetch() -> tuple[bytes, str, str]:
        session = await _get_session(impersonate)
        resp = await session.get(
            url, headers=headers, timeout=timeout, allow_redirects=True
        )
        body = resp.content
        status = resp.status_code

        if _is_cloudflare(status, body):
            log.debug("Cloudflare block on %s — cloudscraper fallback", url)
            body = await cloudscraper_get(url, headers, timeout)
            ct = ""
        else:
            if status >= 400:
                raise Exception(f"HTTP {status} from {url}")
            ct = resp.headers.get("content-type", "")

        ext_from_mime, _ = classify_mime(ct)
        ext = ext_from_mime or url_ext(url) or ".bin"
        log.debug("Downloaded %d bytes from %s (ext=%s)", len(body), url, ext)
        return body, ext, ct

    return await with_retry(_fetch)
