import json

from briefing_engine.llm.writer import (
    BLURB_MAX_WORDS,
    WriteInput,
    build_user_message,
    clip_words,
    validate_reply,
)


def reply(items: list[dict[str, str]], headline: str = "H", intro: str = "I") -> str:
    return json.dumps({"headline": headline, "intro": intro, "items": items})


def test_every_ref_gets_its_blurb_and_unknown_or_repeated_refs_are_ignored() -> None:
    text = reply(
        [
            {"ref": "i1", "blurb": "First.", "story": ""},
            {"ref": "i1", "blurb": "Second copy.", "story": ""},
            {"ref": "zz", "blurb": "Made up.", "story": ""},
            {"ref": "i3", "blurb": "   ", "story": ""},
        ]
    )
    parsed, items, missing = validate_reply(text, ["i1", "i2", "i3"])
    assert parsed is not None
    assert {ref: i.blurb for ref, i in items.items()} == {"i1": "First."}
    assert missing == ["i2", "i3"]  # never sent back, and sent back empty


def test_a_reply_that_is_not_the_schema_writes_nothing() -> None:
    assert validate_reply("not json", ["i1"]) == (None, {}, ["i1"])
    assert validate_reply(None, ["i1"]) == (None, {}, ["i1"])


def test_blurbs_are_clipped_and_stray_markdown_or_tags_removed() -> None:
    long = " ".join(f"w{n}" for n in range(BLURB_MAX_WORDS + 10))
    assert clip_words(long, BLURB_MAX_WORDS).endswith("…")
    assert len(clip_words(long, BLURB_MAX_WORDS).split()) == BLURB_MAX_WORDS
    assert clip_words("**Bold** and <i>tagged</i> `code`", 20) == "Bold and tagged code"
    assert clip_words("  spaced   out  ", 5) == "spaced out"


def test_each_article_sits_inside_this_requests_boundary() -> None:
    item = WriteInput(ref="i1", title="T", sources="S", text="x" * 5000, reason="R")
    message = build_user_message([item], "b0und")
    assert message.startswith("Boundary token for this message: b0und")
    assert '<article ref="i1" b="b0und">' in message and '</article b="b0und">' in message
    assert "x" * 2000 in message and "x" * 2001 not in message  # text is capped
