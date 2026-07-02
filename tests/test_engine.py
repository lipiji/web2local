from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import Config
from crawler.engine import CrawlEngine
from crawler.stealth import DomainRateLimiter
from url_queue.url_queue import QueueItem, URLQueue
from storage.local_store import LocalStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    return Config(
        output_dir=tmp_path / "data",
        db_path=tmp_path / "test.db",
        max_depth=1,
        max_pages=5,
        concurrency=2,
        search_max_results=3,
        follow_links=False,
        same_domain_only=False,
        min_delay=0.0,   # disable delays in tests
        max_delay=0.0,
    )


@pytest.fixture
async def queue(config):
    q = URLQueue(config.db_path)
    await q.initialize()
    yield q
    await q.close()


@pytest.fixture
def store(config):
    return LocalStore(config.output_dir, "test")


@pytest.fixture
def noop_rl():
    """Rate limiter whose wait() is instant (avoids test slowdowns)."""
    rl = DomainRateLimiter(min_delay=0.0, max_delay=0.0)
    return rl


def make_engine(config, queue, store, rl):
    return CrawlEngine(config, queue, store, rate_limiter=rl)


# ---------------------------------------------------------------------------
# _download_binary
# ---------------------------------------------------------------------------


async def test_download_binary_saves_file(config, queue, store, noop_rl, tmp_path):
    engine = make_engine(config, queue, store, noop_rl)
    fake_path = tmp_path / "test.pdf"
    fake_path.write_bytes(b"")

    with (
        patch("crawler.engine.download_binary", new_callable=AsyncMock) as mock_dl,
        patch.object(store, "save_binary", new_callable=AsyncMock) as mock_save,
        patch.object(store, "log_metadata", new_callable=AsyncMock),
        patch.object(queue, "mark_success", new_callable=AsyncMock) as mock_ok,
    ):
        mock_dl.return_value = (b"fake pdf", ".pdf", "application/pdf")
        mock_save.return_value = (fake_path, "abc123")

        await engine._download_binary(
            "https://arxiv.org/pdf/test.pdf", ".pdf", "pdf", "ai", None
        )

        mock_dl.assert_called_once()
        mock_save.assert_called_once()
        mock_ok.assert_called_once()


async def test_download_binary_marks_failed_on_error(config, queue, store, noop_rl):
    engine = make_engine(config, queue, store, noop_rl)

    with (
        patch("crawler.engine.download_binary", new_callable=AsyncMock) as mock_dl,
        patch.object(queue, "mark_failed", new_callable=AsyncMock) as mock_fail,
    ):
        mock_dl.side_effect = Exception("network error")
        await engine._process(QueueItem("https://arxiv.org/pdf/err.pdf", "ai", 0))
        mock_fail.assert_called_once()
        assert "network error" in mock_fail.call_args[0][1]


# ---------------------------------------------------------------------------
# _fetch_html
# ---------------------------------------------------------------------------


async def test_fetch_html_saves_html_file(config, queue, store, noop_rl, tmp_path):
    engine = make_engine(config, queue, store, noop_rl)
    fake_path = tmp_path / "page.html"
    fake_path.write_text("")

    fake_result = MagicMock()
    fake_result.success = True
    fake_result.html = "<html><body>Hello</body></html>"
    fake_result.text = "Hello"
    fake_result.markdown = ""
    fake_result.title = "Test Page"
    fake_result.strategy_used = "fast"
    fake_result.fetched_at = "2024-01-01T00:00:00"
    fake_result.final_url = "https://example.com/page"
    fake_result.metadata = {}

    with (
        patch("crawler.engine.read_url", new_callable=AsyncMock) as mock_read,
        patch.object(store, "save_html", new_callable=AsyncMock) as mock_save,
        patch.object(store, "log_metadata", new_callable=AsyncMock),
        patch.object(queue, "mark_success", new_callable=AsyncMock) as mock_ok,
    ):
        mock_read.return_value = fake_result
        mock_save.return_value = (fake_path, "hashval")

        await engine._fetch_html("https://example.com/page", "test", 0, None)

        mock_read.assert_called_once_with("https://example.com/page")
        mock_save.assert_called_once()
        mock_ok.assert_called_once()


async def test_fetch_html_marks_failed_on_empty_content(config, queue, store, noop_rl):
    engine = make_engine(config, queue, store, noop_rl)

    fake_result = MagicMock()
    fake_result.success = True
    fake_result.html = ""
    fake_result.text = ""
    fake_result.markdown = ""
    fake_result.metadata = {}

    with (
        patch("crawler.engine.read_url", new_callable=AsyncMock) as mock_read,
        patch.object(queue, "mark_failed", new_callable=AsyncMock) as mock_fail,
    ):
        mock_read.return_value = fake_result
        await engine._fetch_html("https://empty.com/", "test", 0, None)
        mock_fail.assert_called_once()


async def test_fetch_html_marks_failed_on_web4agent_failure(config, queue, store, noop_rl):
    engine = make_engine(config, queue, store, noop_rl)

    fake_result = MagicMock()
    fake_result.success = False
    fake_result.error = "403 Forbidden"
    fake_result.metadata = {}

    with (
        patch("crawler.engine.read_url", new_callable=AsyncMock) as mock_read,
        patch.object(queue, "mark_failed", new_callable=AsyncMock) as mock_fail,
    ):
        mock_read.return_value = fake_result
        await engine._fetch_html("https://blocked.com/", "test", 0, None)
        mock_fail.assert_called_once()


# ---------------------------------------------------------------------------
# _enqueue_links (referer tracking)
# ---------------------------------------------------------------------------


async def test_enqueue_links_discovers_hrefs_and_tracks_referer(
    config, queue, store, noop_rl
):
    engine = make_engine(config, queue, store, noop_rl)
    html = """
    <html><body>
      <a href="https://example.com/a">A</a>
      <a href="/b">B</a>
      <a href="javascript:void(0)">JS</a>
    </body></html>
    """
    with (
        patch.object(queue, "is_seen", new_callable=AsyncMock, return_value=False),
        patch.object(queue, "add_many", new_callable=AsyncMock, return_value=2) as mock_add,
    ):
        await engine._enqueue_links("https://example.com/", "test", 0, html)
        added_urls = mock_add.call_args[0][0]
        assert "https://example.com/a" in added_urls
        assert "https://example.com/b" in added_urls
        assert not any("javascript" in u for u in added_urls)
        # Referer should be set for discovered links
        assert engine._referers.get("https://example.com/a") == "https://example.com/"


# ---------------------------------------------------------------------------
# run() integration (mocked)
# ---------------------------------------------------------------------------


async def test_run_with_empty_search_exits_cleanly(config, tmp_path, noop_rl):
    q = URLQueue(tmp_path / "empty.db")
    await q.initialize()
    s = LocalStore(config.output_dir, "test")
    engine = CrawlEngine(config, q, s, rate_limiter=noop_rl)

    with (
        patch("crawler.engine.get_seed_urls", new_callable=AsyncMock, return_value=[]),
        patch("crawler.engine.get_arxiv_urls", new_callable=AsyncMock, return_value=[]),
    ):
        stats = await engine.run("nonexistent keyword xyz")

    assert stats.get("pending", 0) == 0
    await q.close()
