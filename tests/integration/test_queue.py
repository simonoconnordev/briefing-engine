import pytest
from sqlalchemy import Engine, text

from briefing_engine.queue.jobs import (
    LeaseLost,
    claim,
    claim_score_batch,
    complete,
    enqueue,
    fail,
)

pytestmark = pytest.mark.integration


def state(db: Engine, job_id: int) -> tuple[str, int, str | None]:
    with db.connect() as conn:
        row = conn.execute(
            text("SELECT state, attempts, last_error FROM jobs WHERE id = :id"), {"id": job_id}
        ).one()
    return row.state, row.attempts, row.last_error


def test_dedupe_key_allows_one_live_job(db: Engine) -> None:
    with db.begin() as conn:
        first = enqueue(conn, "fetch", "fetch_source", {"source_id": 1}, dedupe_key="fetch:1")
        second = enqueue(conn, "fetch", "fetch_source", {"source_id": 1}, dedupe_key="fetch:1")
    assert first is not None and second is None

    [job] = claim(db, ["fetch"], "w1")
    with db.begin() as conn:
        assert enqueue(conn, "fetch", "fetch_source", {}, dedupe_key="fetch:1") is None  # running
        complete(conn, job.id, "w1")
    with db.begin() as conn:  # a finished job no longer blocks a new one
        assert enqueue(conn, "fetch", "fetch_source", {}, dedupe_key="fetch:1") is not None


def test_claim_takes_highest_priority_and_counts_the_attempt(db: Engine) -> None:
    with db.begin() as conn:
        enqueue(conn, "fetch", "fetch_source", {"n": 1})
        build = enqueue(conn, "build", "build_briefing", {"n": 2})
    [job] = claim(db, ["fetch", "build"], "w1")
    assert job.id == build
    assert job.attempts == 1
    assert state(db, job.id)[0] == "running"


def test_claimed_jobs_are_skipped_by_other_workers(db: Engine) -> None:
    with db.begin() as conn:
        for n in range(3):
            enqueue(conn, "fetch", "fetch_source", {"n": n})
    a = claim(db, ["fetch"], "w1", batch=2)
    b = claim(db, ["fetch"], "w2", batch=2)
    assert len(a) == 2 and len(b) == 1
    assert not {j.id for j in a} & {j.id for j in b}


def test_score_batch_shares_one_rubric_and_caps_at_ten(db: Engine) -> None:
    with db.begin() as conn:
        for n in range(12):
            enqueue(conn, "score", "score_article", {"n": n}, group_key="rubric-a")
        enqueue(conn, "score", "score_article", {"n": 99}, group_key="rubric-b")
    batch = claim_score_batch(db, "w1")
    assert len(batch) == 10
    assert {j.group_key for j in batch} == {"rubric-a"}


def test_finishing_requires_the_lease(db: Engine) -> None:
    with db.begin() as conn:
        job_id = enqueue(conn, "fetch", "fetch_source", {})
    assert job_id is not None
    claim(db, ["fetch"], "w1")
    with pytest.raises(LeaseLost), db.begin() as conn:
        complete(conn, job_id, "someone-else")
    with db.begin() as conn:
        assert fail(conn, job_id, "someone-else", "nope") is False
        assert fail(conn, job_id, "w1", "boom") is True
    assert state(db, job_id) == ("dead", 1, "boom")
