import pytest

from config import BINARY_EXTENSIONS, EXT_CATEGORY, Config


def test_every_binary_extension_has_a_category():
    """classify_url() falls back to the HTML path for any extension missing here."""
    missing = BINARY_EXTENSIONS - EXT_CATEGORY.keys()
    assert missing == set()


def test_archive_extensions_map_to_archive_category():
    for ext in (".zip", ".tar", ".gz", ".7z"):
        assert EXT_CATEGORY[ext] == "archive"


def test_media_extensions_map_to_media_category():
    for ext in (".mp4", ".mp3", ".wav", ".avi"):
        assert EXT_CATEGORY[ext] == "media"


def test_from_env_defaults_without_env_vars(monkeypatch):
    for key in (
        "W2L_OUTPUT_DIR", "W2L_DB_PATH", "W2L_MAX_DEPTH", "W2L_MAX_PAGES",
        "W2L_CONCURRENCY", "W2L_TIMEOUT", "W2L_SEARCH_MAX_RESULTS",
        "W2L_ARXIV_MAX_RESULTS", "W2L_MAX_FILE_SIZE", "W2L_FOLLOW_LINKS",
        "W2L_SAME_DOMAIN_ONLY", "W2L_MIN_DELAY", "W2L_MAX_DELAY",
    ):
        monkeypatch.delenv(key, raising=False)

    config = Config.from_env()
    assert config.max_depth == 3
    assert config.max_pages == 1000
    assert config.max_file_size == 200 * 1024 * 1024
    assert config.arxiv_max_results == 20
    assert config.follow_links is True
    assert config.same_domain_only is False


def test_from_env_reads_overrides(monkeypatch):
    monkeypatch.setenv("W2L_MAX_DEPTH", "7")
    monkeypatch.setenv("W2L_CONCURRENCY", "50")
    monkeypatch.setenv("W2L_MAX_FILE_SIZE", "1048576")
    monkeypatch.setenv("W2L_FOLLOW_LINKS", "false")
    monkeypatch.setenv("W2L_SAME_DOMAIN_ONLY", "true")

    config = Config.from_env()
    assert config.max_depth == 7
    assert config.concurrency == 50
    assert config.max_file_size == 1048576
    assert config.follow_links is False
    assert config.same_domain_only is True
