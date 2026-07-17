from unittest.mock import AsyncMock, patch

import pytest

from crawler.downloader import (
    DownloadError,
    DownloadTooLargeError,
    classify_mime,
    classify_url,
    download_binary,
    url_ext,
)


def test_url_ext_pdf():
    assert url_ext("https://arxiv.org/pdf/2301.00001.pdf") == ".pdf"


def test_url_ext_docx_with_query():
    assert url_ext("https://example.com/report.docx?v=2") == ".docx"


def test_url_ext_no_extension():
    assert url_ext("https://example.com/article") == ""


def test_url_ext_root_path():
    assert url_ext("https://example.com/") == ""


def test_classify_url_pdf():
    ext, cat = classify_url("https://arxiv.org/pdf/2301.00001.pdf")
    assert ext == ".pdf"
    assert cat == "pdf"


def test_classify_url_docx():
    ext, cat = classify_url("https://example.com/report.docx")
    assert ext == ".docx"
    assert cat == "docs"


def test_classify_url_pptx():
    ext, cat = classify_url("https://example.com/slides.pptx")
    assert ext == ".pptx"
    assert cat == "ppt"


def test_classify_url_image():
    ext, cat = classify_url("https://example.com/photo.png")
    assert ext == ".png"
    assert cat == "images"


def test_classify_url_html_extension():
    ext, cat = classify_url("https://example.com/page.html")
    assert ext is None
    assert cat is None


def test_classify_url_no_extension():
    ext, cat = classify_url("https://example.com/page")
    assert ext is None
    assert cat is None


def test_classify_mime_pdf():
    ext, cat = classify_mime("application/pdf")
    assert ext == ".pdf"
    assert cat == "pdf"


def test_classify_mime_with_charset():
    ext, cat = classify_mime("application/pdf; charset=utf-8")
    assert ext == ".pdf"


def test_classify_mime_image():
    ext, cat = classify_mime("image/png")
    assert ext == ".png"
    assert cat == "images"


def test_classify_mime_html_returns_none():
    ext, cat = classify_mime("text/html; charset=utf-8")
    assert ext is None
    assert cat is None


def test_classify_mime_unknown_returns_none():
    ext, cat = classify_mime("application/octet-stream")
    assert ext is None
    assert cat is None


def test_classify_mime_docx():
    ext, cat = classify_mime(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert ext == ".docx"
    assert cat == "docs"


# ---------------------------------------------------------------------------
# DownloadError — carries status_code so retry/backoff logic keeps working
# ---------------------------------------------------------------------------


def test_download_error_exposes_status_code():
    exc = DownloadError(404, "https://example.com/missing")
    assert exc.status_code == 404
    assert exc.response.status_code == 404
    assert "404" in str(exc)


# ---------------------------------------------------------------------------
# download_binary — streaming size limit (fake session, no real network)
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    def __init__(self, status_code: int, headers: dict, chunks: list[bytes]):
        self.status_code = status_code
        self.headers = headers
        self._chunks = chunks

    async def aiter_content(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamCtx:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response

    def stream(self, method, url, **kwargs):
        return _FakeStreamCtx(self._response)


def _patch_session(response: _FakeStreamResponse):
    async def _fake_get_session(impersonate: str):
        return _FakeSession(response)

    return patch("crawler.downloader._get_session", side_effect=_fake_get_session)


async def test_download_binary_rejects_oversized_content_length():
    response = _FakeStreamResponse(
        status_code=200,
        headers={"content-length": str(10 * 1024 * 1024), "content-type": "application/pdf"},
        chunks=[b"x" * 1024],
    )
    with _patch_session(response):
        with pytest.raises(DownloadTooLargeError):
            await download_binary(
                "https://example.com/huge.pdf", max_size=1024 * 1024
            )


async def test_download_binary_aborts_mid_stream_without_content_length():
    # No content-length header: must be caught by the running-total check instead.
    big_chunk = b"x" * (2 * 1024 * 1024)
    response = _FakeStreamResponse(
        status_code=200,
        headers={"content-type": "application/pdf"},
        chunks=[big_chunk, big_chunk],
    )
    with _patch_session(response):
        with pytest.raises(DownloadTooLargeError):
            await download_binary(
                "https://example.com/huge.pdf", max_size=1024 * 1024
            )


async def test_download_binary_succeeds_under_limit():
    response = _FakeStreamResponse(
        status_code=200,
        headers={"content-type": "application/pdf"},
        chunks=[b"%PDF-1.4 ", b"fake content"],
    )
    with _patch_session(response):
        body, ext, ct = await download_binary(
            "https://example.com/small.pdf", max_size=1024 * 1024
        )
    assert body == b"%PDF-1.4 fake content"
    assert ext == ".pdf"
    assert ct == "application/pdf"


async def test_download_binary_raises_download_error_on_http_404():
    response = _FakeStreamResponse(
        status_code=404,
        headers={"content-type": "text/html"},
        chunks=[b"not found"],
    )
    with _patch_session(response):
        with pytest.raises(DownloadError) as exc_info:
            await download_binary("https://example.com/missing.pdf")
    assert exc_info.value.status_code == 404
