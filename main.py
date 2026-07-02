import asyncio
import logging
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from config import Config
from crawler.engine import CrawlEngine
from url_queue.url_queue import URLQueue
from storage.local_store import LocalStore

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("web2local")


@click.group()
def cli() -> None:
    """web2local — crawl the web and save content for LLM training."""


@cli.command()
@click.argument("keyword")
@click.option("--output", "-o", default="./data", show_default=True, help="Output directory")
@click.option("--depth", "-d", default=3, show_default=True, help="Max BFS depth")
@click.option("--max-pages", "-n", default=1000, show_default=True, help="Max pages to save")
@click.option("--concurrency", "-c", default=10, show_default=True, help="Concurrent requests")
@click.option("--search-results", default=20, show_default=True, help="Seed URLs from search")
@click.option("--db", default="./crawl.db", show_default=True, help="SQLite queue database")
@click.option("--follow-links/--no-follow-links", default=True, show_default=True)
@click.option("--same-domain/--all-domains", default=False, show_default=True)
def crawl(
    keyword: str,
    output: str,
    depth: int,
    max_pages: int,
    concurrency: int,
    search_results: int,
    db: str,
    follow_links: bool,
    same_domain: bool,
) -> None:
    """Crawl the web for KEYWORD and save all relevant files locally."""
    config = Config(
        output_dir=Path(output),
        db_path=Path(db),
        max_depth=depth,
        max_pages=max_pages,
        concurrency=concurrency,
        search_max_results=search_results,
        follow_links=follow_links,
        same_domain_only=same_domain,
    )
    asyncio.run(_run(keyword, config))


@cli.command()
@click.option("--db", default="./crawl.db", show_default=True, help="SQLite queue database")
def status(db: str) -> None:
    """Show statistics for the current crawl queue."""
    asyncio.run(_show_status(Path(db)))


@cli.command()
@click.argument("keyword")
@click.option("--max-results", default=20, show_default=True)
def arxiv(keyword: str, max_results: int) -> None:
    """Download PDFs from arXiv for KEYWORD."""

    async def _run() -> None:
        from sources.search import get_arxiv_urls

        urls = await get_arxiv_urls(keyword, max_results)
        for u in urls:
            click.echo(u)

    asyncio.run(_run())


# ------------------------------------------------------------------
# Async helpers
# ------------------------------------------------------------------


async def _run(keyword: str, config: Config) -> None:
    queue = URLQueue(config.db_path)
    await queue.initialize()
    store = LocalStore(config.output_dir, keyword)
    engine = CrawlEngine(config, queue, store)
    try:
        stats = await engine.run(keyword)
        out_path = config.output_dir / keyword.replace(" ", "-")
        click.echo(f"\nDone! Files saved to: {out_path}")
        for s, n in sorted(stats.items()):
            click.echo(f"  {s:<15} {n}")
    finally:
        await queue.close()


async def _show_status(db_path: Path) -> None:
    queue = URLQueue(db_path)
    await queue.initialize()
    stats = await queue.stats()
    await queue.close()
    if not stats:
        click.echo("No crawl data found.")
        return
    for s, n in sorted(stats.items()):
        click.echo(f"{s:<15} {n}")


if __name__ == "__main__":
    cli()
