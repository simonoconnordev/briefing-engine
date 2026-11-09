"""Exactly what the scorer is sent about an article, and the hash of it (§6.5).

The request body and the cache key are both built from `scoring_input`, so they can't
drift apart. Adding a field to the request means adding it here, which changes the hash.
"""

from typing import Protocol

from briefing_engine.hashing import canonical_json, sha256

MAX_SCORING_CHARS = 6000


class HasScoringFields(Protocol):
    @property
    def title(self) -> str: ...

    @property
    def summary_text(self) -> str: ...


def scoring_input(article: HasScoringFields) -> dict[str, str]:
    # Checkpoint 1.3 adds extracted full text: `text` becomes body_text when there is one.
    return {"title": article.title, "text": article.summary_text[:MAX_SCORING_CHARS]}


def input_hash(fields: dict[str, str]) -> bytes:
    return sha256(canonical_json(fields))
