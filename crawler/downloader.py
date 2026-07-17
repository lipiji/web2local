"""
Binary file downloader:
- curl_cffi AsyncSession pool (one per impersonation target) for connection reuse
- Realistic browser headers via HeaderBuilder
- Per-domain rate limiting + adaptive backoff
- cloudscraper fallback on Cloudflare 403/503
- Streaming download with a hard size cap to avoid unbounded memory use
"""

import asyncio
import logging
import random as _random
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

DEFAULT_MAX_DOWNLOAD_SIZE = 200 * 1024 * 1024  # 200 MB

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
_sessions_lock = asyncio.Lock()


class DownloadError(Exception):
    """Non-2xx HTTP response. Carries status_code for retry/backoff logic."""

    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"HTTP {status_code} from {url}")
        self.status_code = status_code
        self.url = url

    @property
    def response(self) -> "DownloadError":
        # Lets existing retry/backoff code do getattr(exc, "response").status_code
        return self


class DownloadTooLargeError(Exception):
    """Raised when a response body exceeds the configured size limit."""


async def _get_session(impersonate: str):
    """Return a shared AsyncSession for the given impersonation target."""
    if impersonate not in _sessions:
        async with _sessions_lock:
            if impersonate not in _sessions:
                from curl_cffi.requests import AsyncSession  # type: ignore[import]
                _sessions[impersonate] = AsyncSession(impersonate=impersonate)
    return _sessions[impersonate]


async def close_all_sessions() -> None:
    """Close all pooled curl_cffi sessions. Call once at process shutdown."""
    async with _sessions_lock:
        for session in _sessions.values():
            await session.close()
        _sessions.clear()


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


async def download_binary(
    url: str,
    timeout: int = 30,
    rate_limiter: Optional[AdaptiveDomainRateLimiter] = None,
    referer: Optional[str] = None,
    max_size: int = DEFAULT_MAX_DOWNLOAD_SIZE,
) -> tuple[bytes, str, str]:
    """
    Download a binary file.
    Strategy: shared curl_cffi session (TLS impersonation) → cloudscraper fallback.
    Streams the body and aborts once `max_size` bytes have been received.
    Returns (content_bytes, resolved_ext, content_type_header).
    """
    if rate_limiter:
        await rate_limiter.wait(url)

    # TLS-fingerprint rotation, not security-sensitive randomness
    impersonate = _random.choice(_IMPERSONATE_POOL)  # nosec B311
    headers = _header_builder.build(binary=True, referer=referer)

    async def _fetch() -> tuple[bytes, str, str]:
        session = await _get_session(impersonate)
        chunks: list[bytes] = []
        total = 0
        async with session.stream(
            "GET", url, headers=headers, timeout=timeout, allow_redirects=True
        ) as resp:
            status = resp.status_code
            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > max_size:
                raise DownloadTooLargeError(
                    f"Content-Length {content_length} exceeds max_size={max_size} for {url}"
                )
            async for chunk in resp.aiter_content():
                total += len(chunk)
                if total > max_size:
                    raise DownloadTooLargeError(
                        f"Download exceeded max_size={max_size} bytes: {url}"
                    )
                chunks.append(chunk)
            ct = resp.headers.get("content-type", "")
        body = b"".join(chunks)

        if _is_cloudflare(status, body):
            log.debug("Cloudflare block on %s — cloudscraper fallback", url)
            body = await cloudscraper_get(url, headers, timeout)
            ct = ""
        elif status >= 400:
            raise DownloadError(status, url)

        ext_from_mime, _ = classify_mime(ct)
        ext = ext_from_mime or url_ext(url) or ".bin"
        log.debug("Downloaded %d bytes from %s (ext=%s)", len(body), url, ext)
        return body, ext, ct

    return await with_retry(_fetch)
