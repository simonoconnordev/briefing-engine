from datetime import UTC, datetime

from briefing_engine.build import Candidate, rank


def cand(
    article_id: int,
    score: int,
    *,
    h: bytes | None = None,
    n: int = 1,
    w: float = 1.0,
    day: int = 9,
) -> Candidate:
    return Candidate(
        article_id=article_id,
        content_hash=h or bytes([article_id]),
        score=score,
        reason="",
        published_at=datetime(2026, 11, day, tzinfo=UTC),
        n_sources=n,
        weight=w,
    )


def test_single_source_rank_value_is_the_score() -> None:
    assert cand(1, 7).rank_value == 7


def test_ranks_by_value_then_newer_then_lower_id() -> None:
    ranked = rank([cand(1, 6), cand(2, 8), cand(3, 6, day=8), cand(4, 6)], max_items=10)
    assert [c.article_id for c in ranked] == [2, 1, 4, 3]


def test_more_sources_and_weight_raise_rank() -> None:
    ranked = rank([cand(1, 7), cand(2, 6, n=3), cand(3, 6, w=1.5)], max_items=10)
    assert [c.article_id for c in ranked] == [2, 3, 1]


def test_skips_repeated_content_and_caps_items() -> None:
    same = b"same"
    ranked = rank([cand(1, 9, h=same), cand(2, 8, h=same), cand(3, 7), cand(4, 6)], max_items=2)
    assert [c.article_id for c in ranked] == [1, 3]
