import pytest

from briefing_engine.ingest.text import canon_text, strip_html, to_plain_text


def test_strip_html_drops_tags_scripts_and_styles() -> None:
    html = "<p>Hello <b>world</b></p><script>alert(1)</script><style>p{}</style><p>bye</p>"
    assert to_plain_text(html) == "Hello world bye"


def test_entities_are_decoded_exactly_once() -> None:
    # A double-encoded feed: decoding once leaves the literal "&amp;" the author wrote.
    assert to_plain_text("Tom &amp;amp; Jerry") == "Tom &amp; Jerry"
    assert to_plain_text("AT&amp;T &#x27;quoted&#x27;") == "AT&T 'quoted'"


def test_plain_text_fields_are_not_decoded_again() -> None:
    # A plain-text title about HTML keeps its literal entity text.
    assert to_plain_text("Escaping &amp; in HTML", "text/plain") == "Escaping &amp; in HTML"


def test_bare_ampersand_survives() -> None:
    assert to_plain_text("AT&T and R&D") == "AT&T and R&D"


def test_block_elements_become_spaces() -> None:
    assert strip_html("<li>one</li><li>two</li>").split() == ["one", "two"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ℌello", "hello"),  # NFKC first turns ℌ into H, then casefold lowers it
        ("Straße", "strasse"),  # casefold, not lower
        ("ＡＩ　news", "ai news"),  # full-width letters and an ideographic space
        ("  many   spaces\n\there ", "many spaces here"),
    ],
)
def test_canon_text(raw: str, expected: str) -> None:
    assert canon_text(raw) == expected
