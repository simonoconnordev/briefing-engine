"""What each feed contributes, and how far Claude's scores agree with the reader's marks.

Both read only data the pipeline already records: which feed brought each article
(article_sources), its score under the profile's current rubric (llm_scores), what was sent
(briefing_items) and the reader's verdicts (article_feedback).
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from briefing_engine.feedback import Profile


@dataclass(frozen=True)
class SourceRow:
    name: str
    fetched: int
    scored: int
    above_threshold: int
    sent: int
    interesting: int
    not_for_me: int

    @property
    def hit_rate(self) -> float | None:
        return self.above_threshold / self.scored if self.scored else None


def source_report(
    conn: Connection, user_id: uuid.UUID, profile: Profile, days: int
) -> list[SourceRow]:
    rows = conn.execute(
        text(
            """
            WITH recent AS (
                SELECT asrc.source_id, a.id, a.input_hash
                FROM article_sources asrc
                JOIN articles a ON a.id = asrc.article_id
                WHERE asrc.first_seen_at >= now() - make_interval(days => :days)
            ),
            scored AS (
                SELECT DISTINCT ON (r.source_id, r.id) r.source_id, r.id, s.score
                FROM recent r
                JOIN llm_scores s ON s.input_hash = r.input_hash
                                 AND s.rubric_hash = :rubric_hash
                ORDER BY r.source_id, r.id, s.created_at DESC
            ),
            sent AS (
                SELECT DISTINCT bi.article_id
                FROM briefing_items bi
                JOIN briefings b ON b.id = bi.briefing_id
                WHERE b.user_id = :u AND b.profile_id = :profile_id
            )
            SELECT src.name,
                   count(DISTINCT r.id) AS fetched,
                   count(DISTINCT sc.id) AS scored,
                   count(DISTINCT sc.id) FILTER (WHERE sc.score >= :threshold)
                       AS above_threshold,
                   count(DISTINCT r.id) FILTER (WHERE sent.article_id IS NOT NULL) AS sent,
                   count(DISTINCT r.id) FILTER (WHERE f.verdict = 'interesting')
                       AS interesting,
                   count(DISTINCT r.id) FILTER (WHERE f.verdict = 'not_for_me') AS not_for_me
            FROM subscriptions sub
            JOIN sources src ON src.id = sub.source_id
            LEFT JOIN recent r ON r.source_id = src.id
            LEFT JOIN scored sc ON sc.source_id = src.id AND sc.id = r.id
            LEFT JOIN sent ON sent.article_id = r.id
            LEFT JOIN article_feedback f ON f.article_id = r.id AND f.user_id = :u
            WHERE sub.profile_id = :profile_id
            GROUP BY src.id, src.name
            ORDER BY src.name
            """
        ),
        {
            "days": days,
            "rubric_hash": profile.rubric_hash,
            "u": user_id,
            "profile_id": profile.id,
            "threshold": profile.threshold,
        },
    )
    return [
        SourceRow(
            r.name, r.fetched, r.scored, r.above_threshold, r.sent, r.interesting, r.not_for_me
        )
        for r in rows
    ]


@dataclass(frozen=True)
class Disagreement:
    title: str
    score: int
    verdict: str
    note: str


@dataclass
class Agreement:
    marked: int = 0
    interesting: int = 0
    not_for_me: int = 0
    mean_score_interesting: float | None = None
    mean_score_not_for_me: float | None = None
    # Claude would send it, the reader didn't want it / Claude would skip it, the reader did.
    unwanted: list[Disagreement] = field(default_factory=list)
    missed: list[Disagreement] = field(default_factory=list)

    @property
    def agreed(self) -> int:
        return self.marked - len(self.unwanted) - len(self.missed)


def agreement(conn: Connection, user_id: uuid.UUID, profile: Profile) -> Agreement:
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT ON (a.id) a.title, s.score, f.verdict, f.note
            FROM article_feedback f
            JOIN articles a ON a.id = f.article_id
            JOIN llm_scores s ON s.input_hash = a.input_hash AND s.rubric_hash = :rubric_hash
            WHERE f.user_id = :u
            ORDER BY a.id, s.created_at DESC
            """
        ),
        {"u": user_id, "rubric_hash": profile.rubric_hash},
    ).all()
    result = Agreement(marked=len(rows))
    liked = [r.score for r in rows if r.verdict == "interesting"]
    disliked = [r.score for r in rows if r.verdict == "not_for_me"]
    result.interesting, result.not_for_me = len(liked), len(disliked)
    result.mean_score_interesting = sum(liked) / len(liked) if liked else None
    result.mean_score_not_for_me = sum(disliked) / len(disliked) if disliked else None
    for r in sorted(rows, key=lambda r: -r.score):
        item = Disagreement(r.title, r.score, r.verdict, r.note)
        if r.verdict == "not_for_me" and r.score >= profile.threshold:
            result.unwanted.append(item)
        elif r.verdict == "interesting" and r.score < profile.threshold:
            result.missed.append(item)
    return result
