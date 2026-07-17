"""
CrawlEngine — producer-consumer pipeline for maximum throughput.

Architecture:
  _producer  →  asyncio.Queue  →  N × _worker
                                        ↓
                              _enqueue_links (thread pool)
                                        ↓
                              URLQueue (SQLite) ← producer polls
"""

import asyncio
import logging
from datetime import UTC, datetime
from functools import partial
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from web4agent import read_url

from config import Config
from crawler.downloader import classify_mime, classify_url, download_binary
from crawler.security import is_safe_url
from crawler.stealth import AdaptiveDomainRateLimiter, HeaderBuilder, with_retry
from url_queue.url_queue import QueueItem, URLQueue
from sources.search import get_arxiv_urls, get_seed_urls, get_bing_urls
from storage.local_store import LocalStore

log = logging.getLogger(__name__)

_headers = HeaderBuilder()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _extract_links_sync(
    base_url: str,
    html: str,
    blocked_domains: tuple[str, ...],
    same_domain_only: bool,
) -> list[str]:
    """CPU-bound link extraction — runs in thread pool executor."""
    soup = BeautifulSoup(html, "html.parser")
    base_netloc = urlparse(base_url).netloc
    urls: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href.startswith(("javascript:", "mailto:", "#", "tel:")):
            continue
        abs_url = urljoin(base_url, href).split("#")[0]
        if not abs_url.startswith(("http://", "https://")):
            continue
        domain = urlparse(abs_url).netloc
        if any(b in domain for b in blocked_domains):
            continue
        if same_domain_only and domain != base_netloc:
            continue
        urls.append(abs_url)
    return urls


class CrawlEngine:
    def __init__(
        self,
        config: Config,
        queue: URLQueue,
        store: LocalStore,
        rate_limiter: AdaptiveDomainRateLimiter | None = None,
    ) -> None:
        self._cfg = config
        self._queue = queue
        self._store = store
        self._rl = rate_limiter or AdaptiveDomainRateLimiter(
            min_delay=config.min_delay,
            max_delay=config.max_delay,
        )
        # Referer chain: child URL → parent URL that linked to it
        self._referers: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, keyword: str) -> dict[str, int]:
        log.info("Starting crawl for: %s", keyword)
        await self._seed(keyword)

        # Bounded pipeline queue — backpressure prevents memory explosion
        q_size = max(self._cfg.concurrency * 4, 64)
        work_q: asyncio.Queue[QueueItem | None] = asyncio.Queue(maxsize=q_size)

        prod = asyncio.create_task(self._producer(work_q))
        workers = [
            asyncio.create_task(self._worker(work_q))
            for _ in range(self._cfg.concurrency)
        ]
        reporter = asyncio.create_task(self._report_loop())

        await prod
        # Drain workers then stop reporter
        await asyncio.gather(*workers, return_exceptions=True)
        reporter.cancel()

        final = await self._queue.stats()
        log.info("Crawl complete. Stats: %s", final)
        return final

    # ------------------------------------------------------------------
    # Producer: continuously feeds work_q from SQLite
    # ------------------------------------------------------------------

    async def _producer(self, work_q: asyncio.Queue) -> None:
        idle_rounds = 0
        while True:
            stats = await self._queue.stats()
            if stats.get("success", 0) >= self._cfg.max_pages:
                log.info("max_pages limit reached (%d).", self._cfg.max_pages)
                break

            batch = await self._queue.get_batch_diverse(self._cfg.concurrency * 2)
            if not batch:
                idle_rounds += 1
                if idle_rounds >= 6:  # ~1.8 s with 0.3 s sleep
                    # Double-check: only quit when nothing is left anywhere
                    stats = await self._queue.stats()
                    if stats.get("pending", 0) == 0 and stats.get("in_progress", 0) == 0:
                        log.info("Queue exhausted.")
                        break
                    idle_rounds = 0  # workers may still be adding links
                await asyncio.sleep(0.3)
                continue

            idle_rounds = 0
            for item in batch:
                await work_q.put(item)  # blocks on backpressure

        # Sentinel per worker to drain the queue gracefully
        for _ in range(self._cfg.concurrency):
            await work_q.put(None)

    # ------------------------------------------------------------------
    # Worker: processes one URL at a time
    # ------------------------------------------------------------------

    async def _worker(self, work_q: asyncio.Queue) -> None:
        while True:
            item = await work_q.get()
            if item is None:
                break
            await self._process(item)

    # ------------------------------------------------------------------
    # Seeding: run all sources in parallel
    # ------------------------------------------------------------------

    async def _seed(self, keyword: str) -> None:
        log.info("Seeding from multiple sources in parallel…")

        ddg_results, arxiv_urls, bing_urls = await asyncio.gather(
            get_seed_urls(keyword, self._cfg.search_max_results),
            get_arxiv_urls(keyword, max_results=self._cfg.arxiv_max_results),
            get_bing_urls(keyword, max_results=self._cfg.search_max_results),
            return_exceptions=True,
        )

        # DuckDuckGo results (may already carry content)
        if isinstance(ddg_results, list):
            for r in ddg_results:
                url = r.get("url", "").strip()
                if not url or await self._queue.is_seen(url):
                    continue
                await self._queue.add(url, keyword, depth=0)
                if r.get("success") and r.get("content"):
                    path, chash = await self._store.save_text(url, r["content"])
                    await self._queue.mark_success(url, str(path), chash)
                    await self._store.log_metadata({
                        "url": url, "title": r.get("title", ""), "topic": keyword,
                        "type": "search_result", "file_path": str(path),
                        "content_hash": chash, "timestamp": _now(),
                    })

        # arXiv + Bing URLs: batch-add (dedup in DB)
        extra_urls: list[str] = []
        if isinstance(arxiv_urls, list):
            extra_urls.extend(arxiv_urls)
        if isinstance(bing_urls, list):
            extra_urls.extend(bing_urls)
        if extra_urls:
            added = await self._queue.add_many(extra_urls, keyword, depth=0)
            log.info("Added %d extra seed URLs (arXiv + Bing).", added)

        log.info("Seed complete — %d URLs pending.", await self._queue.pending_count())

    # ------------------------------------------------------------------
    # URL processing
    # ------------------------------------------------------------------

    async def _process(self, item: QueueItem) -> None:
        url, topic, depth = item
        if not await is_safe_url(url):
            log.warning("Blocked unsafe URL (SSRF guard): %s", url)
            await self._queue.mark_failed(url, "blocked: unsafe target address")
            return

        ext, cat = classify_url(url)
        referer = self._referers.get(url)
        try:
            if cat:
                await self._download_binary(url, ext, cat, topic, referer)
            else:
                await self._fetch_html(url, topic, depth, referer)
        except Exception as exc:
            log.warning("Error processing %s: %s", url, exc)
            await self._queue.mark_failed(url, str(exc))

    async def _download_binary(
        self, url: str, ext: str, cat: str, topic: str, referer: str | None
    ) -> None:
        try:
            content, actual_ext, _ct = await download_binary(
                url,
                self._cfg.timeout,
                rate_limiter=self._rl,
                referer=referer,
                max_size=self._cfg.max_file_size,
            )
            self._rl.on_success(url)
            path, chash = await self._store.save_binary(url, content, actual_ext, cat)
            await self._queue.mark_success(url, str(path), chash)
            await self._store.log_metadata({
                "url": url, "topic": topic, "type": cat,
                "file_path": str(path), "content_hash": chash,
                "size_bytes": len(content), "timestamp": _now(),
            })
            log.info("[%s] %s  →  %s", cat, url[:70], path.name)
        except Exception as exc:
            self._rl.on_error(url, exc)
            raise

    async def _fetch_html(
        self, url: str, topic: str, depth: int, referer: str | None
    ) -> None:
        await self._rl.wait(url)

        async def _do_read():
            return await read_url(url)

        try:
            result = await with_retry(_do_read)
            self._rl.on_success(url)
        except Exception as exc:
            self._rl.on_error(url, exc)
            raise

        if not result.success:
            log.debug("Fetch failed %s: %s", url, result.error)
            await self._queue.mark_failed(url, result.error or "fetch failed")
            return

        # If server returned binary content without a recognizable URL extension,
        # switch to the binary download path directly (re-queueing would be a
        # no-op: the URL is already in the DB and INSERT OR IGNORE silently drops it).
        ct = (result.metadata or {}).get("content_type", "") if result.metadata else ""
        if ct:
            mime_ext, cat = classify_mime(ct)
            if cat:
                await self._download_binary(url, mime_ext, cat, topic, referer)
                return

        html = result.html or ""
        text = result.text or result.markdown or ""
        if not html and not text:
            await self._queue.mark_failed(url, "empty content")
            return

        path, chash = await self._store.save_html(url, html, text)
        await self._queue.mark_success(url, str(path), chash)
        await self._store.log_metadata({
            "url": url, "final_url": result.final_url or url,
            "title": result.title or "", "topic": topic, "type": "html",
            "file_path": str(path), "content_hash": chash,
            "strategy": result.strategy_used, "word_count": len(text.split()),
            "timestamp": result.fetched_at or _now(),
        })
        log.info(
            "[html] %s  (%d words, %s)",
            (result.title or url)[:70], len(text.split()), result.strategy_used,
        )

        if self._cfg.follow_links and depth < self._cfg.max_depth:
            await self._enqueue_links(url, topic, depth, html)

    # ------------------------------------------------------------------
    # Link extraction (CPU-heavy part offloaded to thread pool)
    # ------------------------------------------------------------------

    async def _enqueue_links(
        self, base_url: str, topic: str, depth: int, html: str
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
            raw_links = await loop.run_in_executor(
                None,
                partial(
                    _extract_links_sync,
                    base_url, html,
                    self._cfg.blocked_domains,
                    self._cfg.same_domain_only,
                ),
            )

            seen = await self._queue.is_seen_many(raw_links)
            new_urls = [u for u in raw_links if u not in seen]
            for abs_url in new_urls:
                self._referers[abs_url] = base_url  # track navigation chain

            if new_urls:
                added = await self._queue.add_many(new_urls, topic, depth + 1)
                log.debug("Discovered %d new links from %s", added, base_url[:60])
        except Exception as exc:
            log.debug("Link extraction error %s: %s", base_url, exc)

    # ------------------------------------------------------------------
    # Periodic progress reporter
    # ------------------------------------------------------------------

    async def _report_loop(self, interval: float = 30.0) -> None:
        while True:
            await asyncio.sleep(interval)
            stats = await self._queue.stats()
            total = sum(stats.values())
            success = stats.get("success", 0)
            log.info(
                "Progress — success: %d  pending: %d  in_progress: %d  failed: %d  total: %d",
                success,
                stats.get("pending", 0),
                stats.get("in_progress", 0),
                stats.get("failed", 0),
                total,
            )
