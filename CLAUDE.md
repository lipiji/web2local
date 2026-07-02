# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Goal

A Python async web crawler that, given a keyword/topic, downloads all relevant content from the web for use as **LLM training corpus**. Targets HTML, PDF, DOCX, PPTX, XLS, images, and plain text — in Chinese and English.

## Commands

```bash
# Install
pip install -r requirements.txt
pip install "web4agent[all]"  # stealth + browser + crawl4ai + server

# Run a crawl
python main.py crawl "transformer architecture" --output ./data --depth 3

# Resume: re-run the same command; the SQLite queue skips already-seen URLs
python main.py crawl "transformer architecture" --resume

# Check queue statistics
python main.py status

# Get arXiv PDF links only
python main.py arxiv "LLM scaling laws" --max-results 50

# Run tests
python -m pytest tests/ -v

# Lint / format
ruff check .
ruff format .
```

## Architecture

```
web2local/
├── main.py              # click CLI (crawl / status / arxiv sub-commands)
├── config.py            # Config dataclass + EXT_CATEGORY / BINARY_EXTENSIONS maps
├── url_queue/
│   └── url_queue.py     # SQLite-backed async URL queue (dedup via SHA-256 hash)
├── storage/
│   └── local_store.py   # File layout: data/<topic>/<YYYYMMDD>/<category>/ + metadata.jsonl
├── crawler/
│   ├── downloader.py    # classify_url/classify_mime + download_binary (httpx)
│   └── engine.py        # CrawlEngine: BFS loop, seed + link-discovery, web4agent integration
└── sources/
    └── search.py        # get_seed_urls (web4agent DuckDuckGo) + get_arxiv_urls (Atom API)
```

## Key Design Decisions

- **`web4agent`** drives HTML fetching with auto-degrading strategy chain: `fast → crawl4ai → browser → wayback → ddg`. Only binary files use direct `httpx` download.
- **URL queue** (`url_queue/url_queue.py`) is SQLite + aiosqlite; statuses are `pending → in_progress → success/failed`. Re-runs skip seen URLs via SHA-256 dedup.
- **Binary detection** is by URL extension first (`classify_url`), MIME header second (`classify_mime`). Unknown URLs are treated as HTML.
- **`sources/search.py`** provides two seed sources: DuckDuckGo via web4agent's `agent_search`, and arXiv Atom API for academic PDFs.
- **Storage layout**: `data/<topic-slug>/<YYYYMMDD>/<category>/` where category is `html | pdf | docs | ppt | xls | images | txt | ebook`. Each HTML file gets a companion `.txt` with extracted text. A `metadata.jsonl` tracks all saved items.
- **`queue/` was renamed to `url_queue/`** to avoid shadowing Python's stdlib `queue` module.

## File Type Handling

| Extension | Category | How saved |
|-----------|----------|-----------|
| `.html`, no ext | html | web4agent fetch → `.html` + `.txt` companion |
| `.pdf` | pdf | httpx binary download |
| `.doc`, `.docx` | docs | httpx binary download |
| `.ppt`, `.pptx` | ppt | httpx binary download |
| `.xls`, `.xlsx` | xls | httpx binary download |
| `.jpg`, `.png`, etc. | images | httpx binary download |
| `.txt`, `.md`, `.csv` | txt | httpx text download |

## Environment Variables

Copy `.env.example` to `.env`. Key variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `W2L_OUTPUT_DIR` | `./data` | Root output directory |
| `W2L_DB_PATH` | `./crawl.db` | SQLite queue database |
| `W2L_MAX_DEPTH` | `3` | BFS depth limit |
| `W2L_MAX_PAGES` | `1000` | Stop after N saved pages |
| `W2L_CONCURRENCY` | `10` | Concurrent requests per batch |
| `W2L_FOLLOW_LINKS` | `true` | Discover links from crawled HTML |
| `W2L_SAME_DOMAIN_ONLY` | `false` | Restrict crawl to seed domains |
