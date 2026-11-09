"""Turning feed text into plain text once, and canonicalising it for hashing (§6.5).

Feed HTML is converted to plain text at parse time, and only plain text is stored.
Nothing downstream strips tags or decodes entities again. Decoding twice is what turned
apostrophes into `&#x27;` in Horizon's email (bug #3), from the other direction.
"""

import unicodedata
from html.parser import HTMLParser

_SKIP = frozenset({"script", "style", "template", "noscript"})
_BREAK = frozenset(
    {"address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
     "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
     "nav", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul"}
)  # fmt: skip


class _TextExtractor(HTMLParser):
    """Collects text outside script and style elements.

    `convert_charrefs=True` makes the parser decode each entity exactly once as it
    reads the text, which is the single decoding step the plan requires.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag in _BREAK:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BREAK:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def strip_html(html_text: str) -> str:
    """Drop tags, scripts and styles, decoding entities once on the way."""
    parser = _TextExtractor()
    parser.feed(html_text)
    parser.close()
    return "".join(parser.parts)


def to_plain_text(value: str, content_type: str = "text/html") -> str:
    """Plain text from a feed field, decoded exactly once.

    Feeds say whether a field is HTML or plain text. HTML goes through `strip_html`,
    which decodes its entities. Plain text has already been decoded by the feed parser,
    so it is left alone: decoding it again would turn a literal `&amp;` in a title
    about HTML into `&`. Whitespace runs are collapsed either way.
    """
    text = strip_html(value) if "html" in content_type else value
    return collapse_whitespace(text)


def canon_text(text: str) -> str:
    """Canonical form for hashing: NFKC, then casefold, then collapsed whitespace.

    NFKC comes first so that characters it turns into capitals are folded too
    (`ℌ` becomes `H`, then `h`). `casefold` also folds `ß` to `ss`, which `lower` doesn't.
    """
    return collapse_whitespace(unicodedata.normalize("NFKC", text).casefold())
