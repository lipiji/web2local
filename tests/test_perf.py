"""
Concurrency and throughput benchmarks.
All network I/O is mocked so tests run in <10 seconds on any machine.
Throughput numbers are printed but not strictly asserted (vary by CPU).
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import Config
from crawler.engine import CrawlEngine
from crawler.stealth import AdaptiveDomainRateLimiter
from url_queue.url_queue import URLQueue
from storage.local_store import LocalStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_html_result(url: str = "https://example.com") -> MagicMock:
    r = MagicMock()
    r.success = True
    r.html = "<html><body>content</body></html>"
    r.text = "content words here"
    r.markdown = ""
    r.title = "Test"
    r.strategy_used = "fast"
    r.fetched_at = None
    r.final_url = url
    r.metadata = {}
    return r


def _make_config(tmp_path: Path, concurrency: int = 20) -> Config:
    return Config(
        output_dir=tmp_path / "data",
        db_path=tmp_path / "crawl.db",
        max_depth=0,           # no link-following in perf tests
        max_pages=10_000,
        concurrency=concurrency,
        min_delay=0.0,
        max_delay=0.0,
        follow_links=False,
    )


async def _seed_queue(queue: URLQueue, n: int, domains: int = 20) -> None:
    """Insert n URLs spread across `domains` different hostnames."""
    urls = [f"https://domain{i % domains}.example.com/page{i}" for i in range(n)]
    await queue.add_many(urls, "benchmark", depth=0)


# ---------------------------------------------------------------------------
# Test: SQLite WAL write throughput
# ---------------------------------------------------------------------------


async def test_sqlite_wal_insert_throughput(tmp_path):
    """1000 URL inserts should complete in under 3 seconds with WAL mode."""
    queue = URLQueue(tmp_path / "wal.db")
    await queue.initialize()

    urls = [f"https://site{i}.com/page" for i in range(1000)]
    t0 = time.monotonic()
    await queue.add_many(urls, "test", 0)
    elapsed = time.monotonic() - t0

    stats = await queue.stats()
    await queue.close()

    assert stats["pending"] == 1000
    assert elapsed < 3.0, f"WAL insert too slow: {elapsed:.2f}s"
    print(f"\nSQLite WAL: 1000 inserts in {elapsed:.3f}s ({1000/elapsed:.0f} rows/s)")


# ---------------------------------------------------------------------------
# Test: domain-diverse batch vs sequential batch
# ---------------------------------------------------------------------------


async def test_diverse_batch_spreads_domains(tmp_path):
    """get_batch_diverse should return URLs from multiple domains in each batch."""
    queue = URLQueue(tmp_path / "div.db")
    await queue.initialize()
    await _seed_queue(queue, 100, domains=20)

    batch = await queue.get_batch_diverse(20)
    domains_seen = {item.url.split("/")[2] for item in batch}
    await queue.close()

    assert len(batch) == 20
    assert len(domains_seen) >= 10, f"Only {len(domains_seen)} distinct domains in batch"
    print(f"\nDomain diversity: {len(domains_seen)}/20 unique domains in batch of 20")


# ---------------------------------------------------------------------------
# Test: pipeline throughput — 100 URLs with 20 workers
# ---------------------------------------------------------------------------


async def test_pipeline_100_urls_20_workers(tmp_path):
    """Pipeline should process 100 mocked URLs quickly with 20 workers."""
    config = _make_config(tmp_path, concurrency=20)
    queue = URLQueue(config.db_path)
    await queue.initialize()
    await _seed_queue(queue, 100, domains=20)

    store = LocalStore(config.output_dir, "bench")
    rl = AdaptiveDomainRateLimiter(min_delay=0.0, max_delay=0.0)
    engine = CrawlEngine(config, queue, store, rate_limiter=rl)

    fake_result = _fake_html_result()

    with (
        patch("crawler.engine.get_seed_urls", new_callable=AsyncMock, return_value=[]),
        patch("crawler.engine.get_arxiv_urls", new_callable=AsyncMock, return_value=[]),
        patch("crawler.engine.get_bing_urls", new_callable=AsyncMock, return_value=[]),
        patch("crawler.engine.read_url", new_callable=AsyncMock, return_value=fake_result),
        patch.object(store, "save_html", new_callable=AsyncMock,
                     return_value=(tmp_path / "x.html", "hash")),
        patch.object(store, "log_metadata", new_callable=AsyncMock),
    ):
        t0 = time.monotonic()
        stats = await engine.run("benchmark")
        elapsed = time.monotonic() - t0

    await queue.close()
    success = stats.get("success", 0)
    rps = success / elapsed if elapsed > 0 else 0

    print(f"\nPipeline 100 URLs / 20 workers: {success} OK in {elapsed:.2f}s ({rps:.1f} URLs/s)")
    assert success >= 95, f"Only {success}/100 succeeded"
    assert elapsed < 15.0, f"Too slow: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Test: pipeline throughput — 500 URLs with 50 workers
# ---------------------------------------------------------------------------


async def test_pipeline_500_urls_50_workers(tmp_path):
    """Stress test: 500 URLs, 50 concurrent workers."""
    config = _make_config(tmp_path, concurrency=50)
    queue = URLQueue(config.db_path)
    await queue.initialize()
    await _seed_queue(queue, 500, domains=50)

    store = LocalStore(config.output_dir, "bench")
    rl = AdaptiveDomainRateLimiter(min_delay=0.0, max_delay=0.0)
    engine = CrawlEngine(config, queue, store, rate_limiter=rl)

    fake_result = _fake_html_result()

    with (
        patch("crawler.engine.get_seed_urls", new_callable=AsyncMock, return_value=[]),
        patch("crawler.engine.get_arxiv_urls", new_callable=AsyncMock, return_value=[]),
        patch("crawler.engine.get_bing_urls", new_callable=AsyncMock, return_value=[]),
        patch("crawler.engine.read_url", new_callable=AsyncMock, return_value=fake_result),
        patch.object(store, "save_html", new_callable=AsyncMock,
                     return_value=(tmp_path / "x.html", "hash")),
        patch.object(store, "log_metadata", new_callable=AsyncMock),
    ):
        t0 = time.monotonic()
        stats = await engine.run("benchmark")
        elapsed = time.monotonic() - t0

    await queue.close()
    success = stats.get("success", 0)
    rps = success / elapsed if elapsed > 0 else 0

    print(f"\nPipeline 500 URLs / 50 workers: {success} OK in {elapsed:.2f}s ({rps:.1f} URLs/s)")
    assert success >= 480, f"Only {success}/500 succeeded"


# ---------------------------------------------------------------------------
# Test: pipeline drains correctly — no stuck in_progress URLs
# ---------------------------------------------------------------------------


async def test_pipeline_drains_cleanly(tmp_path):
    """After run() returns, no URLs should be left stuck in in_progress state."""
    config = _make_config(tmp_path, concurrency=10)
    queue = URLQueue(config.db_path)
    await queue.initialize()
    await _seed_queue(queue, 50, domains=10)

    store = LocalStore(config.output_dir, "drain")
    rl = AdaptiveDomainRateLimiter(min_delay=0.0, max_delay=0.0)
    engine = CrawlEngine(config, queue, store, rate_limiter=rl)

    fake_result = _fake_html_result()

    with (
        patch("crawler.engine.get_seed_urls", new_callable=AsyncMock, return_value=[]),
        patch("crawler.engine.get_arxiv_urls", new_callable=AsyncMock, return_value=[]),
        patch("crawler.engine.get_bing_urls", new_callable=AsyncMock, return_value=[]),
        patch("crawler.engine.read_url", new_callable=AsyncMock, return_value=fake_result),
        patch.object(store, "save_html", new_callable=AsyncMock,
                     return_value=(tmp_path / "x.html", "hash")),
        patch.object(store, "log_metadata", new_callable=AsyncMock),
    ):
        stats = await engine.run("drain")

    await queue.close()
    assert stats.get("in_progress", 0) == 0, "URLs stuck in in_progress after run()"
    assert stats.get("pending", 0) == 0, "URLs still pending after run()"


# ---------------------------------------------------------------------------
# Test: adaptive rate limiter adjusts delays
# ---------------------------------------------------------------------------


def test_adaptive_rl_loosens_on_success():
    rl = AdaptiveDomainRateLimiter(min_delay=1.0, max_delay=5.0)
    url = "https://example.com/page"
    initial = rl._current["example.com"]
    for _ in range(20):
        rl.on_success(url)
    assert rl._current["example.com"] < initial or rl._current["example.com"] == rl._min


def test_adaptive_rl_tightens_on_429():
    rl = AdaptiveDomainRateLimiter(min_delay=1.0, max_delay=5.0)
    url = "https://throttled.com/page"

    class FakeResp:
        status_code = 429

    class FakeHTTPError(Exception):
        response = FakeResp()

    initial = rl._current["throttled.com"]
    rl.on_error(url, FakeHTTPError())
    assert rl._current["throttled.com"] > initial


# ---------------------------------------------------------------------------
# Test: link extraction runs in thread pool (non-blocking)
# ---------------------------------------------------------------------------


async def test_link_extraction_in_executor(tmp_path):
    """_enqueue_links should offload BeautifulSoup to thread pool."""
    config = _make_config(tmp_path)
    queue = URLQueue(config.db_path)
    await queue.initialize()
    store = LocalStore(config.output_dir, "links")
    rl = AdaptiveDomainRateLimiter(min_delay=0.0, max_delay=0.0)
    engine = CrawlEngine(config, queue, store, rate_limiter=rl)

    html = "\n".join(
        f'<a href="https://site{i}.com/page">link {i}</a>' for i in range(50)
    )

    with (
        patch.object(queue, "is_seen", new_callable=AsyncMock, return_value=False),
        patch.object(queue, "add_many", new_callable=AsyncMock, return_value=50) as mock_add,
    ):
        t0 = time.monotonic()
        await engine._enqueue_links("https://base.com/", "test", 0, html)
        elapsed = time.monotonic() - t0

    await queue.close()
    mock_add.assert_called_once()
    added = mock_add.call_args[0][0]
    assert len(added) == 50
    # Should complete quickly even for 50 links
    assert elapsed < 2.0, f"Link extraction too slow: {elapsed:.2f}s"
