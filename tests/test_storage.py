import json

import pytest

from storage.local_store import LocalStore, _md5, _slugify, _url_to_filename


# ------------------------------------------------------------------
# Unit tests for helper functions
# ------------------------------------------------------------------


def test_slugify_basic():
    assert _slugify("Hello World!") == "hello-world"


def test_slugify_chinese_drops_to_empty_but_doesnt_crash():
    result = _slugify("人工智能")
    assert isinstance(result, str)


def test_slugify_long_input_truncated():
    result = _slugify("a" * 200)
    assert len(result) <= 60


def test_url_to_filename_pdf():
    name = _url_to_filename("https://arxiv.org/pdf/2301.00001.pdf", ".pdf")
    assert name.endswith(".pdf")


def test_url_to_filename_no_path():
    name = _url_to_filename("https://example.com", ".html")
    assert name.endswith(".html")
    assert len(name) < 120


def test_url_to_filename_long_path_truncated():
    long_url = "https://example.com/" + "x" * 200 + ".html"
    name = _url_to_filename(long_url, ".html")
    assert len(name) <= 100


def test_md5_consistency():
    assert _md5("hello") == _md5("hello")
    assert _md5("hello") != _md5("world")


# ------------------------------------------------------------------
# Async storage tests
# ------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path, "test-topic")


async def test_save_html_creates_file(store):
    path, chash = await store.save_html("https://example.com/page", "<html>hi</html>", "hi")
    assert path.exists()
    assert path.suffix == ".html"
    assert chash


async def test_save_html_writes_companion_txt(store):
    path, _ = await store.save_html("https://example.com/p", "<html>hi</html>", "plain text here")
    txt = path.with_suffix(".txt")
    assert txt.exists()
    assert "plain text" in txt.read_text()


async def test_save_html_no_collision(store):
    path1, _ = await store.save_html("https://example.com/same", "<html>v1</html>")
    path2, _ = await store.save_html("https://example.com/same", "<html>v2</html>")
    assert path1 != path2
    assert path1.exists() and path2.exists()


async def test_save_binary_pdf(store, tmp_path):
    content = b"%PDF-1.4 fake"
    path, chash = await store.save_binary(
        "https://arxiv.org/pdf/test.pdf", content, ".pdf", "pdf"
    )
    assert path.exists()
    assert path.suffix == ".pdf"
    assert path.read_bytes() == content


async def test_save_binary_no_collision(store):
    path1, _ = await store.save_binary("https://a.com/f.pdf", b"data1", ".pdf", "pdf")
    path2, _ = await store.save_binary("https://a.com/f.pdf", b"data2", ".pdf", "pdf")
    assert path1 != path2


async def test_save_text(store):
    path, chash = await store.save_text("https://example.com/doc", "hello world")
    assert path.exists()
    assert path.read_text() == "hello world"


async def test_log_metadata_creates_jsonl(store):
    await store.log_metadata({"url": "https://x.com", "title": "X"})
    assert store._meta_file.exists()
    line = store._meta_file.read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["url"] == "https://x.com"


async def test_log_metadata_appends(store):
    await store.log_metadata({"url": "https://a.com"})
    await store.log_metadata({"url": "https://b.com"})
    lines = store._meta_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
