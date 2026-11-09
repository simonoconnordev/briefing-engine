"""The SSRF guard (§6.1): a URL a user gives us must point at the public internet.

Without it, a subscription to `http://169.254.169.254/` or `http://localhost:5432/` would
have the worker fetch from inside our own network. The check resolves the host and
refuses the URL if any address it resolves to isn't globally routable. The same check runs
on every redirect target at fetch time, so a public feed can't bounce us inward.
"""

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

Resolver = Callable[[str, int], Iterable[str]]


class UrlNotAllowed(ValueError):
    pass


def system_resolver(host: str, port: int) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


def check_url(
    url: str, resolve: Resolver = system_resolver, *, allow_unresolved: bool = False
) -> None:
    """Raises UrlNotAllowed unless the URL is http(s) on a host with only public addresses.

    `allow_unresolved` is for fetch time: a host that doesn't resolve can't be reached, and
    the fetch itself then fails as a temporary DNS error.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UrlNotAllowed("only http and https URLs with a host are allowed")
    if parts.username or parts.password:
        raise UrlNotAllowed("URLs with credentials are not allowed")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UrlNotAllowed("the port is not valid") from exc
    try:
        addresses = list(resolve(parts.hostname, port))
    except OSError as exc:
        if allow_unresolved:
            return
        raise UrlNotAllowed("the host does not resolve") from exc
    if not addresses:
        if allow_unresolved:
            return
        raise UrlNotAllowed("the host does not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise UrlNotAllowed("resolves to a non-public address")
