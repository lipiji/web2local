import hashlib
import logging
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import aiosqlite

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT    NOT NULL,
    url_hash     TEXT    NOT NULL UNIQUE,
    topic        TEXT    NOT NULL,
    depth        INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'pending',
    added_at     TEXT    NOT NULL,
    crawled_at   TEXT,
    file_path    TEXT,
    content_hash TEXT,
    error_msg    TEXT
);
CREATE INDEX IF NOT EXISTS idx_status   ON urls(status);
CREATE INDEX IF NOT EXISTS idx_topic    ON urls(topic);
CREATE INDEX IF NOT EXISTS idx_url_hash ON urls(url_hash);
"""


class QueueItem(NamedTuple):
    url: str
    topic: str
    depth: int


def _sha256(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class URLQueue:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        # WAL mode: allows concurrent reads while writes happen; dramatically
        # reduces write contention under high concurrency.
        await self._db.execute("PRAGMA journal_mode=WAL")
        # NORMAL sync is safe with WAL and avoids per-commit fsync() overhead.
        await self._db.execute("PRAGMA synchronous=NORMAL")
        # 40 MB in-memory page cache to avoid repeated disk reads.
        await self._db.execute("PRAGMA cache_size=-40000")
        await self._db.execute("PRAGMA temp_store=MEMORY")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def add(self, url: str, topic: str, depth: int = 0) -> bool:
        """Insert a URL. Returns True if newly added, False if already seen."""
        h = _sha256(url)
        await self._db.execute(
            "INSERT OR IGNORE INTO urls (url, url_hash, topic, depth, status, added_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (url, h, topic, depth, _now()),
        )
        await self._db.commit()
        return self._db.total_changes > 0

    async def add_many(self, urls: list[str], topic: str, depth: int = 0) -> int:
        """Batch-insert URLs. Returns number of newly added rows."""
        now = _now()
        rows = [(u, _sha256(u), topic, depth, now) for u in urls]
        await self._db.executemany(
            "INSERT OR IGNORE INTO urls (url, url_hash, topic, depth, status, added_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            rows,
        )
        await self._db.commit()
        return self._db.total_changes

    async def mark_success(
        self,
        url: str,
        file_path: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        await self._db.execute(
            "UPDATE urls SET status='success', crawled_at=?, file_path=?, content_hash=? "
            "WHERE url=?",
            (_now(), file_path, content_hash, url),
        )
        await self._db.commit()

    async def mark_failed(self, url: str, error: str) -> None:
        await self._db.execute(
            "UPDATE urls SET status='failed', crawled_at=?, error_msg=? WHERE url=?",
            (_now(), error[:500], url),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_batch(self, n: int = 10) -> list[QueueItem]:
        """Claim up to n pending URLs in ROWID order (fast, sequential)."""
        async with self._db.execute(
            "SELECT url, topic, depth FROM urls WHERE status='pending' LIMIT ?", (n,)
        ) as cur:
            rows = await cur.fetchall()
        return await self._mark_in_progress(rows)

    async def get_batch_diverse(self, n: int) -> list[QueueItem]:
        """
        Claim n pending URLs with domain diversity.

        Fetches 3× oversample then Python-shuffles before marking in_progress.
        This spreads workers across different domains without expensive SQL sorting,
        so the rate limiter has less contention and concurrency is maximised.
        """
        oversample = min(n * 3, 300)
        async with self._db.execute(
            "SELECT url, topic, depth FROM urls WHERE status='pending' LIMIT ?",
            (oversample,),
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            return []
        rows_list = list(rows)
        random.shuffle(rows_list)
        selected = rows_list[:n]
        return await self._mark_in_progress(selected)

    async def _mark_in_progress(self, rows) -> list[QueueItem]:
        if not rows:
            return []
        await self._db.executemany(
            "UPDATE urls SET status='in_progress' WHERE url=?",
            [(r["url"],) for r in rows],
        )
        await self._db.commit()
        return [QueueItem(url=r["url"], topic=r["topic"], depth=r["depth"]) for r in rows]

    async def is_seen(self, url: str) -> bool:
        h = _sha256(url)
        async with self._db.execute(
            "SELECT 1 FROM urls WHERE url_hash=?", (h,)
        ) as cur:
            return await cur.fetchone() is not None

    async def pending_count(self) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM urls WHERE status='pending'"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def stats(self) -> dict[str, int]:
        async with self._db.execute(
            "SELECT status, COUNT(*) AS cnt FROM urls GROUP BY status"
        ) as cur:
            rows = await cur.fetchall()
        return {r["status"]: r["cnt"] for r in rows}
