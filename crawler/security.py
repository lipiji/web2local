"""
SSRF protection: reject crawl targets that resolve to private, loopback,
link-local, or otherwise non-routable addresses (e.g. cloud metadata
endpoints, internal services). A search keyword should never be able to
make this crawler reach into the host's local network.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal"})


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_and_check(hostname: str) -> bool:
    """Sync resolver check — run in a thread since getaddrinfo blocks."""
    try:
        return _is_public_ip(ipaddress.ip_address(hostname))
    except ValueError:
        pass  # not an IP literal, fall through to DNS resolution
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    return all(_is_public_ip(ipaddress.ip_address(info[4][0])) for info in infos)


async def is_safe_url(url: str) -> bool:
    """
    Return True only if `url` uses http(s) and every address it resolves to
    is a public, routable IP. Blocks 127.0.0.1, 169.254.169.254 (cloud
    metadata), 10.0.0.0/8, 192.168.0.0/16, ::1, fe80::/10, etc.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False
    hostname = parsed.hostname
    if not hostname or hostname.lower() in _BLOCKED_HOSTNAMES:
        return False
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _resolve_and_check, hostname)
