"""Rendering the briefing email: data stays raw until the template, and only the
template escapes (§8.1).

Escaping is chosen by template file extension: `briefing.html` escapes and
`briefing.txt` doesn't. A golden test checks single escaping (Horizon bug #3), and a
lint test forbids `|safe` in HTML templates.
"""

from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlsplit

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

_env = Environment(
    loader=PackageLoader("briefing_engine.delivery", "templates"),
    autoescape=select_autoescape(
        enabled_extensions=("html",), disabled_extensions=("txt",), default=True
    ),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def dollars(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}"


_env.filters["dollars"] = dollars


def safe_href(url: str) -> str | None:
    """Only http and https links survive; `javascript:` and everything else is dropped."""
    try:
        scheme = urlsplit(url.strip()).scheme.lower()
    except ValueError:
        return None
    return url.strip() if scheme in ("http", "https") else None


@dataclass(frozen=True)
class EmailItem:
    rank: int
    title: str
    url: str
    sources: str
    published_at: datetime | None
    score: int
    reason: str  # the scorer's one-line reason
    blurb: str = ""  # the write-up's paragraph, when there is one
    story: str = ""  # a heading shared by items about the same story

    @property
    def href(self) -> str | None:
        return safe_href(self.url)

    @property
    def text(self) -> str:
        return self.blurb or self.reason


@dataclass(frozen=True)
class Section:
    heading: str  # empty for an item that stands alone
    items: list[EmailItem]


@dataclass(frozen=True)
class EmailBriefing:
    profile_name: str
    briefing_date: date
    items: list[EmailItem]
    in_window: int
    scored: int
    above_threshold: int
    threshold: float
    cost_micros: int
    model: str
    briefing_id: str
    mark_url: str | None = None  # the local page for marking these items
    headline: str = ""
    intro: str = ""
    writer: str | None = None  # the model that wrote the blurbs, if any did
    degraded: bool = False  # the write-up failed; the scorer's reasons stand in

    @property
    def mark_href(self) -> str | None:
        return safe_href(self.mark_url) if self.mark_url else None

    @property
    def sections(self) -> list[Section]:
        """Items in rank order, with items about one story gathered under the first."""
        sections: list[Section] = []
        by_story: dict[str, Section] = {}
        for item in self.items:
            key = item.story.casefold()
            if key and key in by_story:
                by_story[key].items.append(item)
                continue
            section = Section(heading=item.story, items=[item])
            sections.append(section)
            if key:
                by_story[key] = section
        # A story heading only earns its place over two or more items.
        return [s if len(s.items) > 1 else Section(heading="", items=s.items) for s in sections]


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str


def long_date(value: date) -> str:
    # Built by hand because %-d (day without a leading zero) isn't portable to Windows.
    return f"{value:%A} {value.day} {value:%B %Y}"


def short_date(value: datetime | None) -> str:
    return f"{value.day} {value:%b}" if value else ""


_env.filters["long_date"] = long_date
_env.filters["short_date"] = short_date


def render_briefing(briefing: EmailBriefing) -> RenderedEmail:
    context = {"b": briefing}
    subject = f"Your {briefing.profile_name} briefing for {long_date(briefing.briefing_date)}"
    return RenderedEmail(
        subject=subject,
        html=_env.get_template("briefing.html").render(context),
        text=_env.get_template("briefing.txt").render(context),
    )
