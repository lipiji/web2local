import pytest

from url_queue.url_queue import URLQueue


@pytest.fixture
async def queue(tmp_path):
    q = URLQueue(tmp_path / "test.db")
    await q.initialize()
    yield q
    await q.close()


async def test_add_returns_true_for_new_url(queue):
    added = await queue.add("https://example.com", "test", 0)
    assert added is True


async def test_add_deduplicates(queue):
    await queue.add("https://example.com", "test", 0)
    # Second add with same URL must be silently ignored
    stats_before = await queue.stats()
    await queue.add("https://example.com", "test", 1)
    stats_after = await queue.stats()
    assert stats_before == stats_after


async def test_get_batch_returns_pending_items(queue):
    await queue.add("https://a.com", "test", 0)
    await queue.add("https://b.com", "test", 0)
    batch = await queue.get_batch(5)
    assert len(batch) == 2
    assert {item.url for item in batch} == {"https://a.com", "https://b.com"}


async def test_get_batch_sets_in_progress(queue):
    await queue.add("https://a.com", "test", 0)
    await queue.get_batch(1)
    stats = await queue.stats()
    assert stats.get("in_progress", 0) == 1
    assert stats.get("pending", 0) == 0


async def test_mark_success(queue):
    await queue.add("https://a.com", "test", 0)
    await queue.get_batch(1)
    await queue.mark_success("https://a.com", "/tmp/a.html", "deadbeef")
    stats = await queue.stats()
    assert stats.get("success", 0) == 1


async def test_mark_failed(queue):
    await queue.add("https://fail.com", "test", 0)
    await queue.get_batch(1)
    await queue.mark_failed("https://fail.com", "connection timeout")
    stats = await queue.stats()
    assert stats.get("failed", 0) == 1


async def test_is_seen_true_after_add(queue):
    await queue.add("https://seen.com", "test", 0)
    assert await queue.is_seen("https://seen.com") is True


async def test_is_seen_false_for_unknown(queue):
    assert await queue.is_seen("https://unknown.com") is False


async def test_add_many_deduplicates(queue):
    urls = ["https://a.com", "https://b.com", "https://a.com"]  # one dup
    await queue.add_many(urls, "test", 1)
    stats = await queue.stats()
    assert stats.get("pending", 0) == 2


async def test_pending_count(queue):
    await queue.add("https://x.com", "test", 0)
    await queue.add("https://y.com", "test", 0)
    assert await queue.pending_count() == 2


async def test_queue_item_fields(queue):
    await queue.add("https://deep.com", "ml", 3)
    batch = await queue.get_batch(1)
    item = batch[0]
    assert item.url == "https://deep.com"
    assert item.topic == "ml"
    assert item.depth == 3


async def test_stats_empty_queue(queue):
    stats = await queue.stats()
    assert stats == {}


async def test_is_seen_many_returns_only_known_urls(queue):
    await queue.add("https://known1.com", "test", 0)
    await queue.add("https://known2.com", "test", 0)
    result = await queue.is_seen_many(
        ["https://known1.com", "https://known2.com", "https://unknown.com"]
    )
    assert result == {"https://known1.com", "https://known2.com"}


async def test_is_seen_many_empty_input(queue):
    assert await queue.is_seen_many([]) == set()


async def test_get_batch_diverse_claims_each_url_exactly_once(queue):
    urls = [f"https://site{i}.com" for i in range(30)]
    await queue.add_many(urls, "test", 0)

    first = await queue.get_batch_diverse(20)
    second = await queue.get_batch_diverse(20)

    first_urls = {item.url for item in first}
    second_urls = {item.url for item in second}
    assert first_urls.isdisjoint(second_urls), "Same URL claimed by two batches"
    assert len(first_urls) + len(second_urls) == 30


async def test_get_batch_diverse_marks_claimed_rows_in_progress(queue):
    await queue.add_many([f"https://a{i}.com" for i in range(10)], "test", 0)
    batch = await queue.get_batch_diverse(5)
    stats = await queue.stats()
    assert len(batch) == 5
    assert stats.get("in_progress", 0) == 5
    assert stats.get("pending", 0) == 5


async def test_initialize_resets_stale_in_progress_rows(tmp_path):
    db_path = tmp_path / "resume.db"
    q1 = URLQueue(db_path)
    await q1.initialize()
    await q1.add("https://stuck.com", "test", 0)
    await q1.get_batch(1)  # claims it -> in_progress
    stats = await q1.stats()
    assert stats.get("in_progress", 0) == 1
    await q1.close()  # simulate crash: no mark_success/mark_failed ever called

    q2 = URLQueue(db_path)
    await q2.initialize()  # should reset the stale in_progress row
    stats2 = await q2.stats()
    await q2.close()
    assert stats2.get("in_progress", 0) == 0
    assert stats2.get("pending", 0) == 1
