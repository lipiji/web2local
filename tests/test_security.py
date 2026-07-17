import pytest

from crawler.security import is_safe_url


# ---------------------------------------------------------------------------
# IP-literal checks (no DNS involved — fast and network-independent)
# ---------------------------------------------------------------------------


async def test_blocks_loopback_ip():
    assert await is_safe_url("http://127.0.0.1/") is False


async def test_blocks_cloud_metadata_ip():
    assert await is_safe_url("http://169.254.169.254/latest/meta-data/") is False


async def test_blocks_private_10_range():
    assert await is_safe_url("http://10.0.0.5/") is False


async def test_blocks_private_192_168_range():
    assert await is_safe_url("http://192.168.1.1/") is False


async def test_blocks_ipv6_loopback():
    assert await is_safe_url("http://[::1]/") is False


async def test_allows_public_ip_literal():
    assert await is_safe_url("http://8.8.8.8/") is True


async def test_blocks_localhost_hostname():
    assert await is_safe_url("http://localhost/") is False


# ---------------------------------------------------------------------------
# Scheme checks
# ---------------------------------------------------------------------------


async def test_blocks_non_http_scheme():
    assert await is_safe_url("ftp://example.com/") is False


async def test_blocks_file_scheme():
    assert await is_safe_url("file:///etc/passwd") is False


async def test_blocks_empty_hostname():
    assert await is_safe_url("http:///no-host") is False


# ---------------------------------------------------------------------------
# DNS-resolution path (mocked — no real network access needed)
# ---------------------------------------------------------------------------


async def test_blocks_hostname_resolving_to_private_ip(monkeypatch):
    def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr("crawler.security.socket.getaddrinfo", fake_getaddrinfo)
    assert await is_safe_url("http://internal.example.test/") is False


async def test_allows_hostname_resolving_to_public_ip(monkeypatch):
    def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("crawler.security.socket.getaddrinfo", fake_getaddrinfo)
    assert await is_safe_url("http://public.example.test/") is True


async def test_blocks_unresolvable_hostname(monkeypatch):
    import socket

    def fake_getaddrinfo(host, port):
        raise socket.gaierror("name not known")

    monkeypatch.setattr("crawler.security.socket.getaddrinfo", fake_getaddrinfo)
    assert await is_safe_url("http://does-not-resolve.example.test/") is False
