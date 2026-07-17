import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import aiofiles


def _slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_-]+", "-", text).strip("-")[:60]


def _url_to_filename(url: str, ext: str) -> str:
    parsed = urlparse(url)
    # Use the last path segment, or the hostname if path is empty
    segment = parsed.path.rstrip("/").split("/")[-1] or parsed.netloc
    # Strip any existing extension so we can attach ext cleanly
    segment = segment.rsplit(".", 1)[0] if "." in segment else segment
    safe = re.sub(r"[^\w.-]", "_", segment)[:80]
    return safe + ext


def _content_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()


class LocalStore:
    def __init__(self, base_dir: Path, topic: str) -> None:
        self._base = base_dir / _slugify(topic)
        self._date = datetime.now(UTC).strftime("%Y%m%d")
        self._meta_file = self._base / "metadata.jsonl"
        # Guards metadata.jsonl appends: concurrent workers writing the same
        # file with plain aiofiles "a" mode can interleave partial lines.
        self._meta_lock = asyncio.Lock()

    def _category_dir(self, category: str) -> Path:
        d = self._base / self._date / category
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _unique_path(self, directory: Path, filename: str, content_hash: str) -> Path:
        path = directory / filename
        if path.exists():
            stem = path.stem
            path = directory / f"{stem}_{content_hash[:8]}{path.suffix}"
        return path

    async def save_html(
        self, url: str, html: str, text: str = ""
    ) -> tuple[Path, str]:
        """Save HTML + optional extracted text. Returns (path, content_hash)."""
        chash = _content_hash(html)
        d = self._category_dir("html")
        path = self._unique_path(d, _url_to_filename(url, ".html"), chash)
        async with aiofiles.open(path, "w", encoding="utf-8", errors="replace") as f:
            await f.write(html)
        if text.strip():
            txt_path = path.with_suffix(".txt")
            async with aiofiles.open(txt_path, "w", encoding="utf-8", errors="replace") as f:
                await f.write(text)
        return path, chash

    async def save_binary(
        self, url: str, content: bytes, ext: str, category: str
    ) -> tuple[Path, str]:
        """Save a binary file (PDF, DOCX, image, …). Returns (path, content_hash)."""
        chash = _content_hash(content)
        d = self._category_dir(category)
        path = self._unique_path(d, _url_to_filename(url, ext), chash)
        async with aiofiles.open(path, "wb") as f:
            await f.write(content)
        return path, chash

    async def save_text(self, url: str, content: str) -> tuple[Path, str]:
        """Save plain text content. Returns (path, content_hash)."""
        chash = _content_hash(content)
        d = self._category_dir("txt")
        path = self._unique_path(d, _url_to_filename(url, ".txt"), chash)
        async with aiofiles.open(path, "w", encoding="utf-8", errors="replace") as f:
            await f.write(content)
        return path, chash

    async def log_metadata(self, record: dict) -> None:
        """Append one JSON record to the topic's metadata.jsonl."""
        self._meta_file.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        async with self._meta_lock:
            async with aiofiles.open(self._meta_file, "a", encoding="utf-8") as f:
                await f.write(line)
