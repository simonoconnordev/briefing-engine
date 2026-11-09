"""The score job: score up to 10 articles that share a rubric in one call (§7.3, §9.1).

The cache is checked again before calling, because another worker may have filled it.
The ledger row is committed as soon as the call returns. The scores and the job states
follow in one transaction, which rolls back if the lease was lost meanwhile; the cost
stays recorded either way, because it was paid either way.
"""

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import Connection, text

from briefing_engine.context import Context
from briefing_engine.llm.client import ErrorKind, LlmApiError
from briefing_engine.llm.rubric import Rubric
from briefing_engine.llm.scorer import BatchResult, ScoreInput
from briefing_engine.llm.scoring_input import input_hash, scoring_input
from briefing_engine.queue.jobs import Fatal, Job, RetryLater, complete, fail

log = logging.getLogger(__name__)


def record_call(
    conn: Connection, *, user_id: uuid.UUID, purpose: str, model: str, result: BatchResult
) -> int:
    """Write one row to the cost ledger and return its id."""
    return int(
        conn.execute(
            text(
                """
                INSERT INTO llm_calls (user_id, purpose, model, request_id, input_tokens,
                    output_tokens, cache_read_tokens, cache_write_tokens, cost_micros,
                    estimate_micros, latency_ms, outcome)
                VALUES (:user_id, :purpose, :model, :request_id, :input_tokens,
                    :output_tokens, :cache_read_tokens, :cache_write_tokens, :cost_micros,
                    :estimate_micros, :latency_ms, :outcome)
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "purpose": purpose,
                "model": model,
                "request_id": result.request_id,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "cache_read_tokens": result.usage.cache_read_tokens,
                "cache_write_tokens": result.usage.cache_write_tokens,
                "cost_micros": result.cost_micros,
                "estimate_micros": result.estimate_micros,
                "latency_ms": result.latency_ms,
                "outcome": result.outcome,
            },
        ).scalar_one()
    )


def handle_score(ctx: Context, jobs: Sequence[Job]) -> None:
    rubric_hash = bytes.fromhex(jobs[0].group_key or jobs[0].payload["rubric_hash"])
    billed_user = uuid.UUID(jobs[0].payload["billed_user_id"])
    model = ctx.scorer.model
    article_ids = [int(job.payload["article_id"]) for job in jobs]

    with ctx.engine.connect() as conn:
        rubric_json = conn.execute(
            text("SELECT rubric FROM profile_versions WHERE rubric_hash = :h LIMIT 1"),
            {"h": rubric_hash},
        ).scalar_one()
        rows = {
            r.id: r
            for r in conn.execute(
                text("SELECT id, title, summary_text FROM articles WHERE id = ANY(:ids)"),
                {"ids": article_ids},
            )
        }
        cached = {
            r.input_hash
            for r in conn.execute(
                text(
                    """
                    SELECT input_hash FROM llm_scores
                    WHERE rubric_hash = :h AND model = :m AND prompt_version = :pv
                      AND input_hash = ANY(:hashes)
                    """
                ),
                {
                    "h": rubric_hash,
                    "m": model,
                    "pv": ctx.prompt_version,
                    "hashes": [input_hash(scoring_input(r)) for r in rows.values()],
                },
            )
        }
    rubric = Rubric.model_validate(rubric_json)

    missing: list[Job] = []
    already: list[Job] = []
    to_score: dict[str, tuple[Job, bytes]] = {}
    inputs: list[ScoreInput] = []
    for job in jobs:
        article = rows.get(int(job.payload["article_id"]))
        if article is None:
            missing.append(job)
            continue
        fields = scoring_input(article)
        ihash = input_hash(fields)
        if ihash in cached:
            already.append(job)
            continue
        ref = f"a{len(inputs) + 1}"
        to_score[ref] = (job, ihash)
        inputs.append(ScoreInput(ref=ref, title=fields["title"], text=fields["text"]))

    try:
        result = ctx.scorer.score(rubric, inputs) if inputs else None
    except LlmApiError as exc:
        # Rate limits, overload and 5xx come back later (after Retry-After when the API
        # sends one); a rejected request or key would fail the same way every time.
        if exc.kind is ErrorKind.RETRYABLE:
            raise RetryLater(str(exc), delay_s=exc.retry_after_s) from exc
        raise Fatal(str(exc)) from exc

    # The call has been paid for, so its ledger row commits on its own: if this worker
    # has lost its lease, the scores below roll back but the cost is still recorded.
    call_id = None
    if result is not None:
        with ctx.engine.begin() as conn:
            call_id = record_call(
                conn, user_id=billed_user, purpose="score", model=model, result=result
            )

    with ctx.engine.begin() as conn:
        for job in missing:
            fail(conn, job.id, ctx.worker_id, "article no longer exists")
        for job in already:
            complete(conn, job.id, ctx.worker_id)
        if result is None:
            return
        for ref, (job, ihash) in to_score.items():
            item = result.items.get(ref)
            if item is None:
                fail(conn, job.id, ctx.worker_id, result.invalid.get(ref, "not scored"))
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO llm_scores (input_hash, rubric_hash, model, prompt_version,
                                            score, reason, topics, method, llm_call_id)
                    VALUES (:ih, :rh, :model, :pv, :score, :reason, :topics, 'llm', :call)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "ih": ihash,
                    "rh": rubric_hash,
                    "model": model,
                    "pv": ctx.prompt_version,
                    "score": item.score,
                    "reason": item.reason,
                    "topics": list(item.topics),
                    "call": call_id,
                },
            )
            complete(conn, job.id, ctx.worker_id)

    if result.cost_micros > result.estimate_micros:
        log.warning(
            "estimate_exceeded: call %s cost %d µ$ > estimate %d µ$",
            call_id, result.cost_micros, result.estimate_micros,
        )  # fmt: skip
    log.info(
        "scored %d/%d articles (%s), %d µ$, cache read %d tokens",
        len(result.items), len(inputs), result.outcome, result.cost_micros,
        result.usage.cache_read_tokens,
    )  # fmt: skip
