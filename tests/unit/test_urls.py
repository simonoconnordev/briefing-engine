import pytest
from hypothesis import given
from hypothesis import strategies as st

from briefing_engine.ingest.urls import normalise_url, remove_dot_segments


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM/a", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/a/", "https://example.com/a/"),  # trailing slash kept
        ("https://example.com/a/./b/../c", "https://example.com/a/c"),
        ("https://example.com/p?utm_source=x&b=2&a=1&fbclid=y", "https://example.com/p?a=1&b=2"),
        ("https://example.com/p?empty=&a=1", "https://example.com/p?a=1&empty="),
        ("https://example.com/search?s=llm", "https://example.com/search?s=llm"),
        ("https://x.substack.com/p/post?r=abc&s=r", "https://x.substack.com/p/post"),
        ("http://simonwillison.net/2026/Nov/9/", "https://simonwillison.net/2026/Nov/9/"),
        ("http://other.example/a", "http://other.example/a"),  # not upgraded blindly
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
    ],
)
def test_normalise_url(raw: str, expected: str) -> None:
    assert normalise_url(raw) == expected


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://example.com/x", "https:///path"])
def test_rejects_non_http_urls(bad: str) -> None:
    with pytest.raises(ValueError):
        normalise_url(bad)


def test_remove_dot_segments_rfc_examples() -> None:
    assert remove_dot_segments("/a/b/c/./../../g") == "/a/g"
    assert remove_dot_segments("mid/content=5/../6") == "mid/6"


segment = st.text(alphabet="abcXYZ019-._~%", min_size=0, max_size=6)
query_part = st.tuples(st.sampled_from(["a", "b", "utm_x", "ref", "q"]), segment)


@given(
    scheme=st.sampled_from(["http", "https", "HTTP"]),
    host=st.sampled_from(["example.com", "Example.COM", "simonwillison.net", "x.substack.com"]),
    port=st.sampled_from(["", ":80", ":443", ":8080"]),
    path=st.lists(st.sampled_from(["a", "b", ".", "..", ""]) | segment, max_size=5),
    query=st.lists(query_part, max_size=4),
    fragment=segment,
)
def test_normalise_is_idempotent(
    scheme: str,
    host: str,
    port: str,
    path: list[str],
    query: list[tuple[str, str]],
    fragment: str,
) -> None:
    qs = "&".join(f"{k}={v}" for k, v in query)
    url = f"{scheme}://{host}{port}/{'/'.join(path)}?{qs}#{fragment}"
    once = normalise_url(url)
    assert normalise_url(once) == once
