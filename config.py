import os
from dataclasses import dataclass, field
from pathlib import Path

# File extensions treated as binary downloads (not crawled as HTML)
BINARY_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf",
    ".doc", ".docx",
    ".ppt", ".pptx",
    ".xls", ".xlsx",
    ".epub",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg",
    ".zip", ".tar", ".gz", ".7z",
    ".mp4", ".mp3", ".wav", ".avi",
})

EXT_CATEGORY: dict[str, str] = {
    ".pdf": "pdf",
    ".doc": "docs", ".docx": "docs",
    ".ppt": "ppt", ".pptx": "ppt",
    ".xls": "xls", ".xlsx": "xls",
    ".epub": "ebook",
    ".jpg": "images", ".jpeg": "images", ".png": "images",
    ".gif": "images", ".webp": "images", ".bmp": "images",
    ".tiff": "images", ".svg": "images",
}


@dataclass
class Config:
    output_dir: Path = field(default_factory=lambda: Path("./data"))
    db_path: Path = field(default_factory=lambda: Path("./crawl.db"))
    max_depth: int = 3
    max_pages: int = 1000
    concurrency: int = 10
    timeout: int = 30
    search_max_results: int = 20
    follow_links: bool = True
    same_domain_only: bool = False
    # Human-like rate limiting: random delay between requests to same domain
    min_delay: float = 1.5   # seconds (Gaussian lower bound)
    max_delay: float = 5.0   # seconds (Gaussian upper bound)
    blocked_domains: tuple[str, ...] = (
        "facebook.com",
        "twitter.com",
        "instagram.com",
        "doubleclick.net",
        "ads.google.com",
        "googleadservices.com",
        # Consistently blocks scrapers with 403
        "baike.baidu.com",
        "tieba.baidu.com",
        "wenku.baidu.com",
        # Ad/tracking noise
        "adservice.google.com",
        "pagead2.googlesyndication.com",
    )

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            output_dir=Path(os.getenv("W2L_OUTPUT_DIR", "./data")),
            db_path=Path(os.getenv("W2L_DB_PATH", "./crawl.db")),
            max_depth=int(os.getenv("W2L_MAX_DEPTH", "3")),
            max_pages=int(os.getenv("W2L_MAX_PAGES", "1000")),
            concurrency=int(os.getenv("W2L_CONCURRENCY", "10")),
            timeout=int(os.getenv("W2L_TIMEOUT", "30")),
            search_max_results=int(os.getenv("W2L_SEARCH_MAX_RESULTS", "20")),
            follow_links=os.getenv("W2L_FOLLOW_LINKS", "true").lower() == "true",
            same_domain_only=os.getenv("W2L_SAME_DOMAIN_ONLY", "false").lower() == "true",
            min_delay=float(os.getenv("W2L_MIN_DELAY", "1.5")),
            max_delay=float(os.getenv("W2L_MAX_DELAY", "5.0")),
        )
