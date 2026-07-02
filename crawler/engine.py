import asyncio
import logging
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from web4agent import read_url

from config import Config
from crawler.downloader import classify_mime, classify_url, download_binary
from crawler.stealth import DomainRateLimiter, HeaderBuilder, with_retry
from url_queue.url_queue import QueueItem, URLQueue
from sources.search import get_arxiv_urls, get_seed_urls
from storage.local_store import LocalStore

log = logging.getLogger(__name__)

_headers = HeaderBuilder()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CrawlEngine:
    def __init__(
        self,
        config: Config,
        queue: URLQueue,
        store: LocalStore,
        rate_limiter: DomainRateLimiter | None = None,
    ) -> None:
        self._cfg = config
        self._queue = queue
        self._store = store
        # Shared rate limiter: one per-domain slot ensures human-like pacing
        self._rl = rate_limiter or DomainRateLimiter(
            min_delay=config.min_delay,
            max_delay=config.max_delay,
        )
        # Referer tracking: url → the page that linked to it
        self._referers: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, keyword: str) -> dict[str, int]:
        log.info("Starting crawl for: %s", keyword)
        await self._seed(keyword)

        while True:
            stats = await self._queue.stats()
            if stats.get("pending", 0) == 0:
                log.info("Queue exhausted.")
                break
            if stats.get("success", 0) >= self._cfg.max_pages:
                log.info("Reached max_pages limit (%d).", self._cfg.max_pages)
                break

            batch = await self._queue.get_batch(self._cfg.concurrency)
            if not batch:
                break

            await asyncio.gather(
                *[self._process(item) for item in batch],
                return_exceptions=True,
            )

        final = await self._queue.stats()
        log.info("Crawl complete. Stats: %s", final)
        return final

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    async def _seed(self, keyword: str) -> None:
        log.info("Searching for seed URLs…")
        results = await get_seed_urls(keyword, self._cfg.search_max_results)

        for r in results:
            url = r.get("url", "").strip()
            if not url or await self._queue.is_seen(url):
                continue
            await self._queue.add(url, keyword, depth=0)

            if r.get("success") and r.get("content"):
                path, chash = await self._store.save_text(url, r["content"])
                await self._queue.mark_success(url, str(path), chash)
                await self._store.log_metadata({
                    "url": url,
                    "title": r.get("title", ""),
                    "topic": keyword,
                    "type": "search_result",
                    "file_path": str(path),
                    "content_hash": chash,
                    "timestamp": _now(),
                })

        arxiv_urls = await get_arxiv_urls(keyword, max_results=10)
        if arxiv_urls:
            added = await self._queue.add_many(arxiv_urls, keyword, depth=0)
            log.info("Added %d arXiv PDF URLs.", added)

        log.info("Seed phase complete — %d URLs pending.", await self._queue.pending_count())

    # ------------------------------------------------------------------
    # Per-URL processing
    # ------------------------------------------------------------------

    async def _process(self, item: QueueItem) -> None:
        url, topic, depth = item
        ext, cat = classify_url(url)
        referer = self._referers.get(url)
        try:
            if cat:
                await self._download_binary(url, ext, cat, topic, referer)
            else:
                await self._fetch_html(url, topic, depth, referer)
        except Exception as exc:
            log.warning("Unhandled error processing %s: %s", url, exc)
            await self._queue.mark_failed(url, str(exc))

    async def _download_binary(
        self, url: str, ext: str, cat: str, topic: str, referer: str | None
    ) -> None:
        content, actual_ext, _ct = await download_binary(
            url, self._cfg.timeout, rate_limiter=self._rl, referer=referer
        )
        path, chash = await self._store.save_binary(url, content, actual_ext, cat)
        await self._queue.mark_success(url, str(path), chash)
        await self._store.log_metadata({
            "url": url,
            "topic": topic,
            "type": cat,
            "file_path": str(path),
            "content_hash": chash,
            "size_bytes": len(content),
            "timestamp": _now(),
        })
        log.info("[%s] %s  →  %s", cat, url[:70], path.name)

    async def _fetch_html(
        self, url: str, topic: str, depth: int, referer: str | None
    ) -> None:
        # Apply rate limiting before handing off to web4agent
        await self._rl.wait(url)

        # Build a fresh realistic UA for this request; web4agent's "fast"
        # strategy (curl_cffi) picks up WRT_USER_AGENT env var, so we rotate it.
        import os
        os.environ["WRT_USER_AGENT"] = _headers.random_ua()

        async def _do_read():
            return await read_url(url)

        result = await with_retry(_do_read)

        if not result.success:
            log.debug("Fetch failed %s: %s", url, result.error)
            await self._queue.mark_failed(url, result.error or "fetch failed")
            return

        # Detect binary content returned without a file extension
        ct = (result.metadata or {}).get("content_type", "") if result.metadata else ""
        if ct:
            ext, cat = classify_mime(ct)
            if cat:
                await self._queue.mark_failed(url, "binary content detected via MIME")
                await self._queue.add(url, topic, depth)
                return

        html = result.html or ""
        text = result.text or result.markdown or ""
        if not html and not text:
            await self._queue.mark_failed(url, "empty content")
            return

        path, chash = await self._store.save_html(url, html, text)
        await self._queue.mark_success(url, str(path), chash)
        await self._store.log_metadata({
            "url": url,
            "final_url": result.final_url or url,
            "title": result.title or "",
            "topic": topic,
            "type": "html",
            "file_path": str(path),
            "content_hash": chash,
            "strategy": result.strategy_used,
            "word_count": len(text.split()),
            "timestamp": result.fetched_at or _now(),
        })
        log.info(
            "[html] %s  (%d words, via %s)",
            (result.title or url)[:70],
            len(text.split()),
            result.strategy_used,
        )

        if self._cfg.follow_links and depth < self._cfg.max_depth:
            await self._enqueue_links(url, topic, depth, html)

    # ------------------------------------------------------------------
    # Link extraction with referer tracking
    # ------------------------------------------------------------------

    async def _enqueue_links(
        self, base_url: str, topic: str, depth: int, html: str
    ) -> None:
        try:
            soup = BeautifulSoup(html, "html.parser")
            base_netloc = urlparse(base_url).netloc
            new_urls: list[str] = []

            for tag in soup.find_all("a", href=True):
                href = tag["href"].strip()
                if href.startswith(("javascript:", "mailto:", "#", "tel:")):
                    continue
                abs_url = urljoin(base_url, href).split("#")[0]
                if not abs_url.startswith(("http://", "https://")):
                    continue

                domain = urlparse(abs_url).netloc
                if any(b in domain for b in self._cfg.blocked_domains):
                    continue
                if self._cfg.same_domain_only and domain != base_netloc:
                    continue
                if not await self._queue.is_seen(abs_url):
                    new_urls.append(abs_url)
                    # Record referer so child pages look like natural navigation
                    self._referers[abs_url] = base_url

            if new_urls:
                added = await self._queue.add_many(new_urls, topic, depth + 1)
                log.debug("Discovered %d new links from %s", added, base_url[:60])
        except Exception as exc:
            log.debug("Link extraction error for %s: %s", base_url, exc)
