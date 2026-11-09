"""URL normalisation: one article, one URL, one hash (§6.5).

`normalise_url` is a pure function. A property test checks it is idempotent:
normalising a normalised URL changes nothing.
"""

from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from briefing_engine.hashing import sha256

# Parameters that only track where a click came from. Kept here as configuration.
TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid", "si"}
)
TRACKING_PREFIXES = ("utm_",)
# Substack adds `r` (referrer) and `s` (share source). Elsewhere `s` is often a real
# parameter (a search query), so these two are removed on Substack hosts only.
SUBSTACK_PARAMS = frozenset({"r", "s"})

# `http` is upgraded to `https` only for hosts known to serve the same resource on both.
# Upgrading blindly could merge different resources.
KNOWN_HTTPS_HOSTS = frozenset({"simonwillison.net"})

DEFAULT_PORTS = {"http": 80, "https": 443}


def remove_dot_segments(path: str) -> str:
    """RFC 3986 §5.2.4. Unlike posixpath.normpath, it keeps trailing and double slashes."""
    output: list[str] = []
    while path:
        if path.startswith("../"):
            path = path[3:]
        elif path.startswith("./"):
            path = path[2:]
        elif path.startswith("/./"):
            path = "/" + path[3:]
        elif path == "/.":
            path = "/"
        elif path.startswith("/../"):
            path = "/" + path[4:]
            if output:
                output.pop()
        elif path == "/..":
            path = "/"
            if output:
                output.pop()
        elif path in (".", ".."):
            path = ""
        else:
            start = 1 if path.startswith("/") else 0
            end = path.find("/", start)
            if end == -1:
                end = len(path)
            output.append(path[:end])
            path = path[end:]
    return "".join(output)


def _is_tracking(name: str, host: str) -> bool:
    lowered = name.lower()
    if lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES):
        return True
    return host.endswith("substack.com") and lowered in SUBSTACK_PARAMS


def normalise_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise ValueError(f"not an http(s) URL: {url!r}")
    host = parts.hostname or ""
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    if ":" in host:  # IPv6 literal
        netloc_host = f"[{host}]"
    else:
        host = host.encode("idna").decode("ascii")
        netloc_host = host

    if scheme == "http" and host in KNOWN_HTTPS_HOSTS:
        scheme = "https"
    port = parts.port
    netloc = netloc_host if port in (None, DEFAULT_PORTS[scheme]) else f"{netloc_host}:{port}"

    path = remove_dot_segments(parts.path) or "/"
    pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(k, host)
    ]
    query = urlencode(sorted(pairs), quote_via=quote)
    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(normalised_url: str) -> bytes:
    return sha256(normalised_url.encode("utf-8"))
