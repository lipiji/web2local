import pytest

from crawler.downloader import classify_mime, classify_url, url_ext


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
