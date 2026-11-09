from dataclasses import dataclass

from briefing_engine.hashing import canonical_json
from briefing_engine.ingest.fetch_job import content_hash
from briefing_engine.llm.rubric import Interest, Rubric, rubric_hash
from briefing_engine.llm.scoring_input import MAX_SCORING_CHARS, input_hash, scoring_input


def test_canonical_json_ignores_key_order() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": [1, 2], "b": 1})
    assert canonical_json({"t": "é"}) == '{"t":"é"}'.encode()


def test_rubric_hash_ignores_key_order() -> None:
    raw = {
        "description": "AI news",
        "interests": [{"topic": "agents", "weight": 2}],
        "avoid": [],
        "scale_hint": "10 = must read",
    }
    reordered = {
        "scale_hint": "10 = must read",
        "avoid": [],
        "interests": [{"weight": 2, "topic": "agents"}],
        "description": "AI news",
    }
    assert rubric_hash(Rubric.model_validate(raw)) == rubric_hash(Rubric.model_validate(reordered))


def test_rubric_hash_changes_with_content() -> None:
    base = Rubric(description="d", interests=[Interest(topic="t", weight=1)], scale_hint="s")
    edited = base.model_copy(update={"description": "d2"})
    assert rubric_hash(base) != rubric_hash(edited)


@dataclass
class _A:
    title: str
    summary_text: str


def test_scoring_input_is_title_and_truncated_text() -> None:
    fields = scoring_input(_A("T", "x" * (MAX_SCORING_CHARS + 50)))
    assert fields == {"title": "T", "text": "x" * MAX_SCORING_CHARS}


def test_input_hash_follows_what_the_model_reads() -> None:
    a = input_hash(scoring_input(_A("Title", "Summary")))
    assert a == input_hash(scoring_input(_A("Title", "Summary")))
    # The model reads the real text, so a case change is a different input...
    assert a != input_hash(scoring_input(_A("TITLE", "Summary")))


def test_content_hash_is_canonical() -> None:
    # ...but the dedup identity treats it as the same story.
    assert content_hash("Title", "Summary  text") == content_hash("TITLE", "summary text")
