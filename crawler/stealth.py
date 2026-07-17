"""
Anti-detection layer:
- AdaptiveDomainRateLimiter: loosens on success, tightens on 429/error
- HeaderBuilder: realistic Chrome headers with UA rotation
- with_retry: exponential backoff + jitter
- cloudscraper_get: Cloudflare bypass in thread executor
"""

import asyncio
import logging
import random
import time
from collections import defaultdict
from typing import Any, Optional
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


def _gauss_clamp(min_s: float, max_s: float) -> float:
    if max_s <= min_s:
        return min_s
    mean = (min_s + max_s) / 2
    std = (max_s - min_s) / 4
    # Request pacing jitter, not security-sensitive randomness
    return max(min_s, min(max_s, random.gauss(mean, std)))  # nosec B311


# ---------------------------------------------------------------------------
# Adaptive per-domain rate limiter
# ---------------------------------------------------------------------------

class AdaptiveDomainRateLimiter:
    """
    Per-domain rate limiter that self-adjusts based on outcomes:
    - Success → gradually decrease delay toward min_delay
    - 429 / connection error → double delay, up to max_delay × 4
    - Different domains never block each other
    """

    def __init__(self, min_delay: float = 1.5, max_delay: float = 5.0) -> None:
        self._min = min_delay
        self._max = max_delay
        self._current: dict[str, float] = defaultdict(lambda: min_delay)
        self._last: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _domain(self, url: str) -> str:
        return urlparse(url).netloc

    async def wait(self, url: str) -> None:
        domain = self._domain(url)
        lock = self._locks[domain]
        async with lock:
            elapsed = time.monotonic() - self._last[domain]
            delay = _gauss_clamp(self._current[domain] * 0.9, self._current[domain] * 1.1)
            gap = delay - elapsed
            if gap > 0:
                log.debug("Rate-limit %s: %.2fs", domain, gap)
                await asyncio.sleep(gap)
            self._last[domain] = time.monotonic()

    def on_success(self, url: str) -> None:
        """Nudge delay 5% down on each success (floor: min_delay)."""
        d = self._domain(url)
        self._current[d] = max(self._min, self._current[d] * 0.95)

    def on_error(self, url: str, exc: Exception) -> None:
        """
        Double delay on retriable errors (429, connection issues).
        Caps at max_delay × 4 so we don't wait forever.
        """
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429 or "ConnectionError" in type(exc).__name__ or "Timeout" in type(exc).__name__:
            d = self._domain(url)
            self._current[d] = min(self._max * 4, self._current[d] * 2.0)
            log.debug("Backed off %s to %.1fs", d, self._current[d])


# Keep the original name as an alias for backward compatibility with tests
DomainRateLimiter = AdaptiveDomainRateLimiter


# ---------------------------------------------------------------------------
# Realistic header builder
# ---------------------------------------------------------------------------

class HeaderBuilder:
    def __init__(self) -> None:
        self._ua_gen = None
        try:
            from fake_useragent import UserAgent
            self._ua_gen = UserAgent(browsers=["chrome", "edge"])
        except Exception as exc:
            log.warning("fake-useragent unavailable: %s", exc)

    def random_ua(self) -> str:
        if self._ua_gen:
            try:
                return self._ua_gen.random
            except Exception as exc:
                log.debug("fake-useragent lookup failed, using fallback UA: %s", exc)
        return _FALLBACK_UA

    def build(
        self,
        *,
        binary: bool = False,
        referer: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> dict[str, str]:
        ua = ua or self.random_ua()
        # Header rotation, not security-sensitive randomness
        brand, _ = random.choice(_CHROME_BRANDS)  # nosec B311
        headers: dict[str, str] = {
            "User-Agent": ua,
            "Accept": "*/*" if binary else _ACCEPT_HTML,
            "Accept-Language": random.choice(_ACCEPT_LANGUAGES),  # nosec B311
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
        if not binary:
            headers["Upgrade-Insecure-Requests"] = "1"
            headers["Cache-Control"] = "max-age=0"

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

_RETRIABLE_NAMES = frozenset({
    "ConnectionError", "ConnectError", "TimeoutError",
    "RemoteProtocolError", "ReadTimeout", "ConnectTimeout",
})
_RETRIABLE_HTTP = frozenset({429, 500, 502, 503, 504})


async def with_retry(
    coro_factory,
    *,
    max_retries: int = 3,
    base_delay: float = 2.0,
) -> Any:
    """Retry with exponential backoff + ±50 % random jitter."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            name = type(exc).__name__
            retriable = status in _RETRIABLE_HTTP or any(n in name for n in _RETRIABLE_NAMES)
            if not retriable or attempt == max_retries:
                raise
            last_exc = exc
            # Backoff jitter, not security-sensitive randomness
            delay = min(base_delay * (2 ** attempt) + random.uniform(0.5, 2.0), 60.0)  # nosec B311
            log.debug("Retry %d/%d in %.1fs (%s)", attempt + 1, max_retries, delay, name)
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Cloudflare bypass via cloudscraper (sync, wrapped in thread executor)
# ---------------------------------------------------------------------------

async def cloudscraper_get(url: str, headers: dict[str, str], timeout: int = 30) -> bytes:
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
