"""
Binary file downloader with anti-detection measures:
- curl_cffi for TLS/JA3 fingerprint impersonation (mimics real Chrome)
- Realistic browser headers via HeaderBuilder
- Per-domain rate limiting (injected as optional parameter)
- Retry with exponential backoff + jitter
- cloudscraper fallback on Cloudflare 403 responses
"""

import logging
from typing import Optional
from urllib.parse import urlparse

from config import EXT_CATEGORY
from crawler.stealth import DomainRateLimiter, HeaderBuilder, cloudscraper_get, with_retry

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

# Chromium impersonation targets supported by curl_cffi
_IMPERSONATE_TARGETS = ["chrome124", "chrome123", "chrome110", "edge99"]

_header_builder = HeaderBuilder()


def url_ext(url: str) -> str:
    """Return lowercase file extension from URL path, e.g. '.pdf'."""
    path = urlparse(url).path
    last = path.rstrip("/").split("/")[-1]
    return ("." + last.rsplit(".", 1)[-1].lower()) if "." in last else ""


def classify_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """Return (ext, category) if URL points to a downloadable binary, else (None, None)."""
    ext = url_ext(url)
    cat = EXT_CATEGORY.get(ext)
    return (ext, cat) if cat else (None, None)


def classify_mime(content_type: str) -> tuple[Optional[str], Optional[str]]:
    """Return (ext, category) from a Content-Type header value."""
    mime = content_type.split(";")[0].strip().lower()
    ext = _MIME_TO_EXT.get(mime)
    if not ext:
        return None, None
    cat = EXT_CATEGORY.get(ext)
    return (ext, cat) if cat else (None, None)


def _is_cloudflare_block(status: int, body: bytes) -> bool:
    return status in {403, 503} and (
        b"cloudflare" in body[:2048].lower()
        or b"cf-ray" in body[:2048].lower()
        or b"just a moment" in body[:2048].lower()
    )


async def download_binary(
    url: str,
    timeout: int = 30,
    rate_limiter: Optional[DomainRateLimiter] = None,
    referer: Optional[str] = None,
) -> tuple[bytes, str, str]:
    """
    Download a binary file.
    Strategy: curl_cffi (TLS impersonation) → cloudscraper (Cloudflare bypass).
    Returns (content_bytes, resolved_ext, content_type_header).
    """
    if rate_limiter:
        await rate_limiter.wait(url)

    impersonate = _IMPERSONATE_TARGETS[0]
    headers = _header_builder.build(binary=True, referer=referer)

    async def _curl_fetch() -> tuple[bytes, str, str]:
        from curl_cffi.requests import AsyncSession  # type: ignore[import]

        async with AsyncSession(impersonate=impersonate) as session:
            resp = await session.get(
                url, headers=headers, timeout=timeout, allow_redirects=True
            )
            body = resp.content
            status = resp.status_code

            if _is_cloudflare_block(status, body):
                log.debug("Cloudflare block on %s — trying cloudscraper", url)
                body = await cloudscraper_get(url, headers, timeout)
                ct = ""
            else:
                if status >= 400:
                    raise Exception(f"HTTP {status}")
                ct = resp.headers.get("content-type", "")

            ext_from_mime, _ = classify_mime(ct)
            ext = ext_from_mime or url_ext(url) or ".bin"
            log.debug("Downloaded %d bytes from %s (ext=%s)", len(body), url, ext)
            return body, ext, ct

    return await with_retry(_curl_fetch)
