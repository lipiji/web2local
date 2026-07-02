"""
Anti-detection layer for human-like web crawling.

Key techniques:
- Per-domain rate limiting with Gaussian-distributed delays
- Realistic Chrome request headers (Sec-Fetch-*, Sec-CH-UA, etc.)
- User-agent rotation via fake-useragent
- Referer chain tracking per domain
- Retry with exponential backoff + jitter
- Cloudflare bypass via cloudscraper (sync, run in thread executor)
"""

import asyncio
import logging
import random
import time
from collections import defaultdict
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_ACCEPT_HTML = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.7"
)

_ACCEPT_LANGUAGES = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "en-US,en;q=0.9",
    "en-GB,en-US;q=0.9,en;q=0.8",
    "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
]

_CHROME_BRANDS = [
    ('"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"', "124"),
    ('"Chromium";v="123", "Google Chrome";v="123", "Not-A.Brand";v="99"', "123"),
    ('"Chromium";v="122", "Google Chrome";v="122", "Not-A.Brand";v="99"', "122"),
]

_FALLBACK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Gaussian delay helper
# ---------------------------------------------------------------------------

def _gauss_clamp(min_s: float, max_s: float) -> float:
    mean = (min_s + max_s) / 2
    std = (max_s - min_s) / 4
    return max(min_s, min(max_s, random.gauss(mean, std)))


# ---------------------------------------------------------------------------
# Per-domain rate limiter
# ---------------------------------------------------------------------------

class DomainRateLimiter:
    """
    Enforces a human-like minimum gap between successive requests to the
    same domain, drawn from a Gaussian distribution so timing is not
    mechanically regular.
    """

    def __init__(self, min_delay: float = 1.5, max_delay: float = 5.0) -> None:
        self._min = min_delay
        self._max = max_delay
        self._last: dict[str, float] = defaultdict(float)
        # One lock per domain prevents concurrent bursts
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def wait(self, url: str) -> None:
        domain = urlparse(url).netloc
        lock = self._locks[domain]
        async with lock:
            elapsed = time.monotonic() - self._last[domain]
            needed = _gauss_clamp(self._min, self._max)
            gap = needed - elapsed
            if gap > 0:
                log.debug("Rate-limit %s: sleep %.2fs", domain, gap)
                await asyncio.sleep(gap)
            self._last[domain] = time.monotonic()


# ---------------------------------------------------------------------------
# Realistic header builder
# ---------------------------------------------------------------------------

class HeaderBuilder:
    """
    Generates browser-authentic request headers including Sec-Fetch-* and
    Sec-CH-UA Client Hints that modern Chromium sends on every navigation.
    Uses fake-useragent for UA rotation; falls back to a hardcoded string.
    """

    def __init__(self) -> None:
        self._ua_gen = None
        try:
            from fake_useragent import UserAgent
            self._ua_gen = UserAgent(browsers=["chrome", "edge"])
        except Exception as exc:
            log.warning("fake-useragent unavailable, using fallback UA: %s", exc)

    def random_ua(self) -> str:
        if self._ua_gen:
            try:
                return self._ua_gen.random
            except Exception:
                pass
        return _FALLBACK_UA

    def build(
        self,
        *,
        binary: bool = False,
        referer: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> dict[str, str]:
        ua = ua or self.random_ua()
        brand, ver = random.choice(_CHROME_BRANDS)
        headers: dict[str, str] = {
            "User-Agent": ua,
            "Accept": "*/*" if binary else _ACCEPT_HTML,
            "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
        if not binary:
            headers["Upgrade-Insecure-Requests"] = "1"
            headers["Cache-Control"] = "max-age=0"

        # Client Hints + Sec-Fetch-* for Chromium UAs
        if "Chrome" in ua or "Edg" in ua:
            headers.update({
                "Sec-Fetch-Dest": "empty" if binary else "document",
                "Sec-Fetch-Mode": "no-cors" if binary else "navigate",
                "Sec-Fetch-Site": "cross-site" if referer else "none",
                "Sec-CH-UA": brand,
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
            })
            if not binary:
                headers["Sec-Fetch-User"] = "?1"

        if referer:
            headers["Referer"] = referer
        return headers


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

_RETRIABLE_CURL_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


async def with_retry(
    coro_factory,
    *,
    max_retries: int = 3,
    base_delay: float = 2.0,
    retriable_http: frozenset[int] = _RETRIABLE_CURL_CODES,
) -> any:
    """
    Retry `await coro_factory()` up to max_retries times on transient errors.
    Uses exponential backoff with random jitter to avoid thundering herd.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            # Check for HTTP status errors from curl_cffi or httpx
            status = getattr(getattr(exc, "response", None), "status_code", None)
            is_retriable = (
                status in retriable_http
                or "ConnectionError" in type(exc).__name__
                or "TimeoutError" in type(exc).__name__
                or "RemoteProtocol" in type(exc).__name__
                or "ConnectError" in type(exc).__name__
            )
            if not is_retriable or attempt == max_retries:
                raise
            last_exc = exc
            delay = min(base_delay * (2 ** attempt) + random.uniform(0.5, 2.0), 60.0)
            log.debug(
                "Retry %d/%d in %.1fs (%s)",
                attempt + 1, max_retries, delay, type(exc).__name__,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Cloudflare bypass (sync cloudscraper, run in executor)
# ---------------------------------------------------------------------------

async def cloudscraper_get(url: str, headers: dict[str, str], timeout: int = 30) -> bytes:
    """
    Fetch `url` via cloudscraper (synchronous Cloudflare JS-challenge solver)
    wrapped in a thread executor so it doesn't block the event loop.
    Returns raw response bytes.
    """
    def _sync() -> bytes:
        import cloudscraper  # type: ignore[import]
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        scraper.headers.update(headers)
        resp = scraper.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)
