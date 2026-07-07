"""DNS fallback for the ISP resolver block on polymarket.com.

Since 2026-07-04 the router/ISP DNS (fritz.box) returns NXDOMAIN for
*.polymarket.com (Polish gambling-registry DNS block); public resolvers still
answer. When normal resolution fails, resolve via Cloudflare DNS-over-HTTPS --
reached by literal IP, so it works with no functioning system DNS -- and retry.
TLS certificate validation is unaffected: it checks the hostname, not the IP.

Imported for its side effect from config.py so every job (logger, resolver,
watchdog, dashboard) gets it with zero per-script changes.

    python dnsfix.py    # self-check: resolves gamma-api.polymarket.com
"""

import json
import socket
import urllib.request

_orig_getaddrinfo = socket.getaddrinfo
_cache = {}  # hostname -> IP, per-process (jobs are short-lived, no TTL needed)


def _doh_lookup(host):
    if host in _cache:
        return _cache[host]
    req = urllib.request.Request(
        f"https://1.1.1.1/dns-query?name={host}&type=A",
        headers={"Accept": "application/dns-json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        answers = json.load(resp).get("Answer", [])
    ips = [a["data"] for a in answers if a.get("type") == 1]  # type 1 = A record
    if not ips:
        raise socket.gaierror(f"DoH fallback: no A record for {host}")
    _cache[host] = ips[0]
    return ips[0]


def _getaddrinfo(host, *args, **kwargs):
    try:
        return _orig_getaddrinfo(host, *args, **kwargs)
    except socket.gaierror:
        # "1.1.1.1" inside _doh_lookup is a literal IP: the original getaddrinfo
        # handles it without DNS, so this cannot recurse.
        return _orig_getaddrinfo(_doh_lookup(host), *args, **kwargs)


socket.getaddrinfo = _getaddrinfo


if __name__ == "__main__":
    infos = socket.getaddrinfo("gamma-api.polymarket.com", 443, socket.AF_INET)
    assert infos, "no addrinfo returned"
    print("ok:", infos[0][4][0])
