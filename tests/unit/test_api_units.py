import pytest

from briefing_engine.api import cursor
from briefing_engine.api.keys import _KEY, generate
from briefing_engine.api.ssrf import UrlNotAllowed, check_url
from briefing_engine.ingest.feeds import make_http_client

KEY = b"k" * 32


def test_a_cursor_round_trips_and_refuses_edits() -> None:
    c = cursor.encode(KEY, "articles", ["2026-11-08T06:30:00Z", 12345], {"min_score": 6})
    assert cursor.decode(KEY, c, "articles", {"min_score": 6}) == ["2026-11-08T06:30:00Z", 12345]
    for bad, resource, filters in [
        (c, "briefings", {"min_score": 6}),  # another list
        (c, "articles", {"min_score": 7}),  # other filters
        (c.replace(".", ".x", 1), "articles", {"min_score": 6}),  # signature edited
        ("not-a-cursor", "articles", {}),
        ("é.é", "articles", {}),
    ]:
        with pytest.raises(cursor.InvalidCursor):
            cursor.decode(KEY, bad, resource, filters)
    with pytest.raises(cursor.InvalidCursor):
        cursor.decode(b"another key" * 3, c, "articles", {"min_score": 6})


def test_filters_that_are_absent_or_none_hash_the_same() -> None:
    assert cursor.filter_hash({"a": None, "b": 1}) == cursor.filter_hash({"b": 1})


def test_keys_have_the_documented_shape_and_are_unique() -> None:
    seen = set()
    for _ in range(50):
        prefix, token, secret_hash = generate()
        assert _KEY.match(token) and token.startswith(f"bek_{prefix}_")
        assert len(secret_hash) == 32
        seen.add(token)
    assert len(seen) == 50


def test_a_redirect_into_the_private_network_is_refused() -> None:
    import httpx

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example":
            return httpx.Response(301, headers={"Location": "http://10.1.2.3/admin"})
        return httpx.Response(200, content=b"secret")

    def guard(url: str) -> None:
        resolver = {"public.example": ["93.184.216.34"], "10.1.2.3": ["10.1.2.3"]}
        check_url(url, lambda host, _port: resolver[host])

    client = make_http_client(httpx.MockTransport(transport), url_guard=guard)
    with client, pytest.raises(UrlNotAllowed):
        client.get("http://public.example/feed")


def test_an_unresolvable_host_is_left_to_fail_as_a_fetch_error() -> None:
    def nowhere(_host: str, _port: int) -> list[str]:
        raise OSError("no such host")

    check_url("https://gone.example/feed", nowhere, allow_unresolved=True)
    with pytest.raises(UrlNotAllowed):
        check_url("https://gone.example/feed", nowhere)
