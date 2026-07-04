import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from crawler.stealth import AdaptiveDomainRateLimiter as DomainRateLimiter, HeaderBuilder, with_retry


# ---------------------------------------------------------------------------
# DomainRateLimiter
# ---------------------------------------------------------------------------


async def test_rate_limiter_waits_between_same_domain():
    rl = DomainRateLimiter(min_delay=0.1, max_delay=0.2)
    await rl.wait("https://example.com/a")
    t0 = time.monotonic()
    await rl.wait("https://example.com/b")
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.08  # at least min_delay minus float noise


async def test_rate_limiter_different_domains_not_blocked():
    rl = DomainRateLimiter(min_delay=2.0, max_delay=3.0)
    # Two different domains — should run near-instantly
    t0 = time.monotonic()
    await asyncio.gather(
        rl.wait("https://a.com/page"),
        rl.wait("https://b.com/page"),
    )
    elapsed = time.monotonic() - t0
    # If domains were serialised, elapsed would be ≥ 4s; separate domains are independent
    assert elapsed < 1.0


async def test_rate_limiter_zero_delay_on_fresh_domain():
    rl = DomainRateLimiter(min_delay=0.5, max_delay=1.0)
    t0 = time.monotonic()
    await rl.wait("https://never-seen.com/")
    elapsed = time.monotonic() - t0
    # First request needs no gap sleep; only Gaussian delay itself matters
    # but since last=0, elapsed=huge, so gap should be ~0
    assert elapsed < 0.05


# ---------------------------------------------------------------------------
# HeaderBuilder
# ---------------------------------------------------------------------------


def test_header_builder_returns_user_agent():
    hb = HeaderBuilder()
    ua = hb.random_ua()
    assert isinstance(ua, str) and len(ua) > 10


def test_header_builder_html_headers_contain_sec_fetch():
    hb = HeaderBuilder()
    # Override UA to guarantee Chrome
    with patch.object(hb, "random_ua", return_value=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )):
        h = hb.build(binary=False)
    assert "Sec-Fetch-Dest" in h
    assert "Sec-CH-UA" in h
    assert h["Sec-Fetch-Dest"] == "document"


def test_header_builder_binary_headers_no_upgrade():
    hb = HeaderBuilder()
    h = hb.build(binary=True)
    assert "Upgrade-Insecure-Requests" not in h
    assert h["Accept"] == "*/*"


def test_header_builder_referer_included():
    hb = HeaderBuilder()
    h = hb.build(referer="https://example.com/source")
    assert h.get("Referer") == "https://example.com/source"


def test_header_builder_no_referer_by_default():
    hb = HeaderBuilder()
    h = hb.build()
    assert "Referer" not in h


# ---------------------------------------------------------------------------
# with_retry
# ---------------------------------------------------------------------------


async def test_with_retry_succeeds_first_try():
    calls = []

    async def _ok():
        calls.append(1)
        return "done"

    result = await with_retry(_ok, max_retries=3, base_delay=0.01)
    assert result == "done"
    assert len(calls) == 1


async def test_with_retry_retries_on_transient_error():
    calls = []

    class FakeTimeoutError(Exception):
        pass

    # Rename so with_retry recognises it as retriable
    FakeTimeoutError.__name__ = "TimeoutError"

    async def _flaky():
        calls.append(1)
        if len(calls) < 3:
            raise FakeTimeoutError("timeout")
        return "ok"

    result = await with_retry(_flaky, max_retries=3, base_delay=0.01)
    assert result == "ok"
    assert len(calls) == 3


async def test_with_retry_raises_after_max_retries():
    class FakeConnectError(Exception):
        pass

    FakeConnectError.__name__ = "ConnectError"

    async def _always_fail():
        raise FakeConnectError("refused")

    with pytest.raises(FakeConnectError):
        await with_retry(_always_fail, max_retries=2, base_delay=0.01)


async def test_with_retry_does_not_retry_non_retriable():
    calls = []

    async def _non_retriable():
        calls.append(1)
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        await with_retry(_non_retriable, max_retries=3, base_delay=0.01)
    assert len(calls) == 1
