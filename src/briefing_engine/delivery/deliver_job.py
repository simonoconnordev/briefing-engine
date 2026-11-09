"""The deliver job: render the briefing and send it by email.

Checkpoint 1.1 sends from the job directly. If the worker died after sending but before
marking the job done, a retry would send again. Checkpoint 1.2 adds the `deliveries`
state machine (§8.3) so an email is sent once.
"""

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import text

from briefing_engine.context import Context
from briefing_engine.delivery.render import EmailBriefing, EmailItem, render_briefing
from briefing_engine.queue.jobs import Job, complete

log = logging.getLogger(__name__)


def load_email_briefing(ctx: Context, briefing_id: uuid.UUID) -> tuple[str, EmailBriefing]:
    """Returns the recipient and the data the templates render."""
    with ctx.engine.connect() as conn:
        b = conn.execute(
            text(
                """
                SELECT b.id, b.briefing_date, b.writeup, b.cost_micros, u.email,
                       p.name AS profile_name, p.threshold
                FROM briefings b
                JOIN users u ON u.id = b.user_id
                JOIN profiles p ON p.id = b.profile_id
                WHERE b.id = :id
                """
            ),
            {"id": briefing_id},
        ).one()
        items = [
            EmailItem(
                rank=r.rank,
                title=r.title,
                url=r.url_canonical,
                sources=r.sources,
                published_at=r.published_at,
                score=r.score,
                reason=r.blurb,
            )
            for r in conn.execute(
                text(
                    """
                    SELECT bi.rank, bi.score, bi.blurb, a.title, a.url_canonical,
                           a.published_at,
                           string_agg(DISTINCT s.name, ', ') AS sources
                    FROM briefing_items bi
                    JOIN articles a ON a.id = bi.article_id
                    JOIN article_sources asrc ON asrc.article_id = a.id
                    JOIN sources s ON s.id = asrc.source_id
                    WHERE bi.briefing_id = :id
                    GROUP BY bi.rank, bi.score, bi.blurb, a.id
                    ORDER BY bi.rank
                    """
                ),
                {"id": briefing_id},
            )
        ]
    writeup = b.writeup or {}
    return b.email, EmailBriefing(
        profile_name=b.profile_name,
        briefing_date=b.briefing_date,
        items=items,
        in_window=int(writeup.get("in_window", 0)),
        scored=int(writeup.get("scored", 0)),
        above_threshold=int(writeup.get("above_threshold", len(items))),
        threshold=float(b.threshold),
        cost_micros=int(b.cost_micros),
        model=ctx.scorer.model,
        briefing_id=str(b.id),
        mark_url=f"{ctx.web_base_url.rstrip('/')}/briefings/{b.id}" if ctx.web_base_url else None,
    )


def handle_deliver(ctx: Context, jobs: Sequence[Job]) -> None:
    for job in jobs:
        briefing_id = uuid.UUID(job.payload["briefing_id"])
        recipient, briefing = load_email_briefing(ctx, briefing_id)
        message_id = ctx.mailer.send(recipient, render_briefing(briefing))
        with ctx.engine.begin() as conn:
            complete(conn, job.id, ctx.worker_id)
        log.info("sent briefing %s (%d items) as %s", briefing_id, len(briefing.items), message_id)
