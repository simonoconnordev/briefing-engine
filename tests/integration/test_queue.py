import time
from collections.abc import Sequence

import pytest
from sqlalchemy import Engine, text

from briefing_engine.queue.jobs import (
    Job,
    LeaseLost,
    Reaped,
    ReplayRefused,
    RetryLater,
    claim,
    claim_score_batch,
    complete,
    defer,
    enqueue,
    fail,
    purge,
    reap,
    replay,
    retry,
)
from briefing_engine.queue.worker import Heartbeat, run_until_idle

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


# --- checkpoint 1.2: retries, deferrals, leases, replay ---------------------------------


def make_due(db: Engine) -> None:
    with db.begin() as conn:
        conn.execute(text("UPDATE jobs SET run_at = now() WHERE state = 'queued'"))


def expire_leases(db: Engine) -> None:
    with db.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' "
                 "WHERE state = 'running'")
        )  # fmt: skip


def test_retry_backs_off_then_buries_the_job_with_a_dead_letter(db: Engine) -> None:
    with db.begin() as conn:
        job_id = enqueue(conn, "fetch", "fetch_source", {"source_id": 7}, max_attempts=2)
    claim(db, ["fetch"], "w1")
    with db.begin() as conn:
        assert retry(conn, job_id, "w1", "HTTP 502", delay_s=60) == "queued"
        due_in = conn.execute(
            text("SELECT extract(epoch FROM run_at - now()) FROM jobs WHERE id = :id"),
            {"id": job_id},
        ).scalar_one()
    assert 55 < due_in <= 60
    assert claim(db, ["fetch"], "w1") == []  # not due yet
    make_due(db)
    claim(db, ["fetch"], "w1")
    with db.begin() as conn:
        assert retry(conn, job_id, "w1", "HTTP 502 again") == "dead"
        letter = conn.execute(text("SELECT queue, payload, error FROM dead_letters")).one()
    assert state(db, job_id) == ("dead", 2, "HTTP 502 again")
    assert tuple(letter) == ("fetch", {"source_id": 7}, "HTTP 502 again")


def test_a_deferral_gives_the_attempt_back(db: Engine) -> None:
    with db.begin() as conn:
        job_id = enqueue(conn, "fetch", "fetch_source", {})
    for _ in range(10):  # far more than max_attempts
        claim(db, ["fetch"], "w1")
        with db.begin() as conn:
            assert defer(conn, job_id, "w1", 0, "host busy")
    with db.connect() as conn:
        row = conn.execute(
            text("SELECT state, attempts, deferrals FROM jobs WHERE id = :id"), {"id": job_id}
        ).one()
    assert tuple(row) == ("queued", 0, 10)


def test_the_reaper_recovers_an_expired_lease_and_buries_a_poison_pill(db: Engine) -> None:
    with db.begin() as conn:
        job_id = enqueue(conn, "build", "build_briefing", {}, max_attempts=2)
    claim(db, ["build"], "crashed-worker")
    assert reap(db) == Reaped(0, 0)  # the lease is still live
    expire_leases(db)
    assert reap(db) == Reaped(requeued=1, dead=0)
    assert state(db, job_id)[:2] == ("queued", 1)
    with pytest.raises(LeaseLost), db.begin() as conn:  # the crashed worker can't finish it
        complete(conn, job_id, "crashed-worker")

    make_due(db)
    claim(db, ["build"], "crashed-again")
    expire_leases(db)
    assert reap(db) == Reaped(requeued=0, dead=1)
    assert state(db, job_id) == ("dead", 2, "lease expired (worker crashed-again)")
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM dead_letters")).scalar_one() == 1


def test_the_heartbeat_keeps_a_slow_job_alive(db: Engine) -> None:
    with db.begin() as conn:
        enqueue(conn, "build", "build_briefing", {})
    [job] = claim(db, ["build"], "w1", lease_s=1)
    with Heartbeat(db, [job.id], "w1", lease_s=1):
        time.sleep(1.6)  # longer than the lease
        assert reap(db) == Reaped(0, 0)
    with db.begin() as conn:
        complete(conn, job.id, "w1")


def test_replay_runs_a_dead_job_again_once(db: Engine) -> None:
    with db.begin() as conn:
        job_id = enqueue(conn, "fetch", "fetch_source", {}, dedupe_key="fetch:1")
    claim(db, ["fetch"], "w1")
    with db.begin() as conn:
        fail(conn, job_id, "w1", "boom")
    with db.begin() as conn:
        job = replay(conn, job_id)
    assert (job.id, job.attempts) == (job_id, 0)
    assert state(db, job_id)[:2] == ("queued", 0)
    with db.connect() as conn:
        assert conn.execute(text("SELECT replayed_at IS NOT NULL FROM dead_letters")).scalar_one()
    with pytest.raises(ReplayRefused, match="only dead jobs"), db.begin() as conn:
        replay(conn, job_id)

    # Dead again, but meanwhile a new job for the same work was queued: replay refuses.
    claim(db, ["fetch"], "w1")
    with db.begin() as conn:
        fail(conn, job_id, "w1", "boom")
        enqueue(conn, "fetch", "fetch_source", {}, dedupe_key="fetch:1")
    with pytest.raises(ReplayRefused, match="already does the same work"), db.begin() as conn:
        replay(conn, job_id)


def test_purge_keeps_succeeded_jobs_a_week_and_dead_ones_a_month(db: Engine) -> None:
    with db.begin() as conn:
        for _ in range(4):
            enqueue(conn, "fetch", "fetch_source", {})
        conn.execute(
            text(
                """
                UPDATE jobs SET finished_at = now() - make_interval(days => d.days),
                                state = d.state
                FROM (VALUES (1, 'succeeded', 8), (2, 'succeeded', 6),
                             (3, 'dead', 31), (4, 'dead', 29)) AS d(id, state, days)
                WHERE jobs.id = d.id
                """
            )
        )
        assert purge(conn) == 2
        assert conn.execute(text("SELECT array_agg(id ORDER BY id) FROM jobs")).scalar_one() == [
            2,
            4,
        ]


def test_the_worker_waits_for_a_retry_due_soon(db: Engine) -> None:
    from briefing_engine.context import Context
    from tests.integration.fakes import FakeMailer, FakeScorer

    with db.begin() as conn:
        enqueue(conn, "build", "build_briefing", {})
    calls: list[int] = []

    def flaky(ctx: Context, jobs: Sequence[Job]) -> None:
        calls.append(jobs[0].attempts)
        if len(calls) == 1:
            raise RetryLater("try again shortly", delay_s=0.3)
        with ctx.engine.begin() as conn:
            complete(conn, jobs[0].id, ctx.worker_id)

    def make(wait: float) -> Context:
        return Context(engine=db, scorer=FakeScorer(), mailer=FakeMailer(),
                       http_client=lambda: None, worker_id="w1",  # type: ignore[arg-type,return-value]
                       wait_up_to_s=wait)  # fmt: skip

    stats = run_until_idle(make(5), {"build": flaky})
    assert calls == [1, 2] and stats.ran == {"build": 1}

    with db.begin() as conn:
        enqueue(conn, "build", "build_briefing", {})
    calls.clear()
    stats = run_until_idle(make(0.1), {"build": flaky})  # the retry is further off than that
    assert calls == [1] and stats.retried == {"build": 1}
