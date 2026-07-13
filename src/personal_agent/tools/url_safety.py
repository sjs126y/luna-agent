"""URL safety — SSRF prevention for web_fetch and web_search.

Blocks requests to private/internal IPs, cloud metadata endpoints,
and other SSRF targets. Modeled after Hermes url_safety.py.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# ── Cloud metadata endpoints (always blocked) ─────────

_CLOUD_METADATA_HOSTS: set[str] = {
    "metadata.google.internal",
    "metadata.goog",
    "169.254.169.254",
    "169.254.170.2",
    "169.254.169.253",
    "100.100.100.200",
}

_CLOUD_METADATA_IPS: set[str] = {
    "169.254.169.254",
    "169.254.170.2",
    "169.254.169.253",
    "fd00:ec2::254",
    "100.100.100.200",
    "259.254.169.254",  # older Azure
}


def check_url(url: str, *, allow_private: bool = False) -> str | None:
    """Validate a URL for SSRF safety. Returns error string or None if safe.

    Known limitation: DNS rebinding TOCTOU attacks (TTL=0 DNS
    returns public IP during check, private IP during connection).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "Error: invalid URL"

    hostname = parsed.hostname
    if not hostname:
        return "Error: URL has no hostname"
    if parsed.scheme not in {"http", "https"}:
        return "Error: URL scheme must be http or https"

    # ── 1. Cloud metadata hostnames (always blocked) ──
    if hostname.lower() in _CLOUD_METADATA_HOSTS:
        return f"Error: access to cloud metadata endpoint '{hostname}' is blocked"

    # ── 2. Resolve hostname → IP ──────────────────────
    try:
        ip_strings = sorted({item[4][0] for item in socket.getaddrinfo(hostname, None)})
    except socket.gaierror:
        return f"Error: cannot resolve hostname '{hostname}'"

    for ip_str in ip_strings:
        # ── 3. Cloud metadata IPs ─────────────────────
        if ip_str in _CLOUD_METADATA_IPS:
            return f"Error: access to cloud metadata IP '{ip_str}' is blocked"

        # ── 4. Private / internal IP ranges ───────────
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return f"Error: cannot parse IP address '{ip_str}'"

        if ip.is_link_local:
            return f"Error: access to link-local '{ip_str}' is blocked"
        if ip.is_multicast:
            return f"Error: access to multicast '{ip_str}' is blocked"
        if ip.is_unspecified:
            return f"Error: access to unspecified address is blocked"
        if not allow_private and ip.is_private:
            return f"Error: access to private IP '{ip_str}' is blocked (SSRF prevention)"
        if not allow_private and ip.is_loopback:
            return f"Error: access to loopback '{ip_str}' is blocked"

        # ── 5. IPv4-mapped IPv6 ───────────────────────
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            v4 = ip.ipv4_mapped
            if not allow_private and (v4.is_private or v4.is_loopback):
                return f"Error: access to IPv4-mapped private address '{ip_str}' is blocked"

    return None
