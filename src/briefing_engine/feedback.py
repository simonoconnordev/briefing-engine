"""The reader's own verdicts: "interesting" or "not for me", with an optional reason.

These marks are how the app learns what the reader actually values. The per-feed report and
the agreement check (report.py) read them; the rubric is edited by hand from what they show.

Two places to mark:
- a sent briefing (context "briefing"), and
- the blind review (context "review"): every article scored in the last few days, shown
  without Claude's score until it has been marked, so the score can't sway the verdict.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

VERDICTS = ("interesting", "not_for_me")
CONTEXTS = ("briefing", "review")
MAX_NOTE = 1000


class FeedbackError(ValueError):
    pass


def resolve_user(conn: Connection, email: str | None) -> uuid.UUID:
    """The user to act for: the one with this email, or the only user there is."""
    if email:
        user_id = conn.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": email}
        ).scalar_one_or_none()
        if user_id is None:
            raise FeedbackError(f"no user with email {email!r}")
        return uuid.UUID(str(user_id))
    ids = conn.execute(text("SELECT id FROM users ORDER BY created_at LIMIT 2")).scalars().all()
    if len(ids) != 1:
        raise FeedbackError("choose a user with --email" if ids else "no users yet")
    return uuid.UUID(str(ids[0]))


def record(
    conn: Connection,
    user_id: uuid.UUID,
    article_id: int,
    verdict: str,
    note: str,
    context: str,
) -> None:
    if verdict not in VERDICTS:
        raise FeedbackError(f"verdict must be one of {VERDICTS}")
    if context not in CONTEXTS:
        raise FeedbackError(f"context must be one of {CONTEXTS}")
    note = note.strip()
    if len(note) > MAX_NOTE:
        raise FeedbackError(f"note is longer than {MAX_NOTE} characters")
    exists = conn.execute(
        text("SELECT 1 FROM articles WHERE id = :a"), {"a": article_id}
    ).scalar_one_or_none()
    if exists is None:
        raise FeedbackError(f"no article {article_id}")
    conn.execute(
        text(
            """
            INSERT INTO article_feedback (user_id, article_id, verdict, note, context)
            VALUES (:u, :a, :v, :n, :c)
            ON CONFLICT (user_id, article_id) DO UPDATE
               SET verdict = EXCLUDED.verdict, note = EXCLUDED.note,
                   context = EXCLUDED.context, updated_at = now()
            """
        ),
        {"u": user_id, "a": article_id, "v": verdict, "n": note, "c": context},
    )


@dataclass(frozen=True)
class MarkItem:
    article_id: int
    title: str
    url: str
    sources: str
    published_at: datetime | None
    excerpt: str
    score: int | None
    reason: str | None
    rank: int | None  # position in the briefing, when it was sent
    verdict: str | None
    note: str


@dataclass(frozen=True)
class Profile:
    id: uuid.UUID
    name: str
    threshold: float
    rubric_hash: bytes


def profiles(conn: Connection, user_id: uuid.UUID) -> list[Profile]:
    return [
        Profile(uuid.UUID(str(r.id)), r.name, float(r.threshold), bytes(r.rubric_hash))
        for r in conn.execute(
            text(
                """
                SELECT p.id, p.name, p.threshold, pv.rubric_hash
                FROM profiles p
                JOIN profile_versions pv ON pv.profile_id = p.id
                                        AND pv.version = p.current_version
                WHERE p.user_id = :u ORDER BY p.created_at, p.name
                """
            ),
            {"u": user_id},
        )
    ]


def _excerpt(summary: str, limit: int = 320) -> str:
    if len(summary) <= limit:
        return summary
    cut = summary[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def _item(r: Any) -> MarkItem:
    return MarkItem(
        article_id=r.id,
        title=r.title,
        url=r.url_canonical,
        sources=r.sources,
        published_at=r.published_at,
        excerpt=_excerpt(r.summary_text),
        score=r.score,
        reason=r.reason,
        rank=r.rank,
        verdict=r.verdict,
        note=r.note or "",
    )


@dataclass(frozen=True)
class BriefingView:
    id: uuid.UUID
    profile_name: str
    briefing_date: Any
    items: list[MarkItem]


def latest_briefing_id(conn: Connection, user_id: uuid.UUID) -> uuid.UUID | None:
    value = conn.execute(
        text(
            "SELECT id FROM briefings WHERE user_id = :u "
            "ORDER BY briefing_date DESC, created_at DESC LIMIT 1"
        ),
        {"u": user_id},
    ).scalar_one_or_none()
    return uuid.UUID(str(value)) if value else None


def briefing_view(conn: Connection, user_id: uuid.UUID, briefing_id: uuid.UUID) -> BriefingView:
    head = conn.execute(
        text(
            """
            SELECT b.id, b.briefing_date, p.name FROM briefings b
            JOIN profiles p ON p.id = b.profile_id
            WHERE b.id = :b AND b.user_id = :u
            """
        ),
        {"b": briefing_id, "u": user_id},
    ).one_or_none()
    if head is None:
        raise FeedbackError("no such briefing")
    rows = conn.execute(
        text(
            """
            SELECT a.id, a.title, a.url_canonical, a.published_at, a.summary_text,
                   bi.score, bi.blurb AS reason, bi.rank, f.verdict, f.note,
                   string_agg(DISTINCT s.name, ', ') AS sources
            FROM briefing_items bi
            JOIN articles a ON a.id = bi.article_id
            JOIN article_sources asrc ON asrc.article_id = a.id
            JOIN sources s ON s.id = asrc.source_id
            LEFT JOIN article_feedback f ON f.article_id = a.id AND f.user_id = :u
            WHERE bi.briefing_id = :b
            GROUP BY a.id, bi.score, bi.blurb, bi.rank, f.verdict, f.note
            ORDER BY bi.rank
            """
        ),
        {"b": briefing_id, "u": user_id},
    )
    return BriefingView(
        id=uuid.UUID(str(head.id)),
        profile_name=head.name,
        briefing_date=head.briefing_date,
        items=[_item(r) for r in rows],
    )


def review_items(
    conn: Connection, user_id: uuid.UUID, profile: Profile, days: int
) -> list[MarkItem]:
    """Every article from the profile's feeds first seen in the last `days` days that has a
    score under the profile's current rubric. Unmarked first, newest first."""
    rows = conn.execute(
        text(
            """
            WITH scored AS (
                SELECT DISTINCT ON (a.id) a.id, s.score, s.reason
                FROM articles a
                JOIN llm_scores s ON s.input_hash = a.input_hash
                                 AND s.rubric_hash = :rubric_hash
                WHERE a.first_seen_at >= now() - make_interval(days => :days)
                ORDER BY a.id, s.created_at DESC
            )
            SELECT a.id, a.title, a.url_canonical, a.published_at, a.summary_text,
                   sc.score, sc.reason, NULL::int AS rank, f.verdict, f.note,
                   string_agg(DISTINCT src.name, ', ') AS sources
            FROM scored sc
            JOIN articles a ON a.id = sc.id
            JOIN article_sources asrc ON asrc.article_id = a.id
            JOIN subscriptions sub ON sub.source_id = asrc.source_id
                                  AND sub.profile_id = :profile_id
            JOIN sources src ON src.id = asrc.source_id
            LEFT JOIN article_feedback f ON f.article_id = a.id AND f.user_id = :u
            GROUP BY a.id, sc.score, sc.reason, f.verdict, f.note
            ORDER BY (f.verdict IS NOT NULL), a.published_at DESC NULLS LAST, a.id DESC
            """
        ),
        {
            "rubric_hash": profile.rubric_hash,
            "days": days,
            "profile_id": profile.id,
            "u": user_id,
        },
    )
    return [_item(r) for r in rows]
