"""Idempotency-Key for POSTs that create work (§5.1).

1. The first request with a key inserts it as `in_progress` and runs the handler; the
   handler stores its response in the same transaction as its own writes.
2. The same key with a different request is refused (422).
3. The same request again gets the stored response, marked `Idempotent-Replayed: true`.
4. The same request while the first is still running gets 409 with `Retry-After: 1`, so
   the handler never runs twice at once.
5. If the first request crashed (its lock expired), the next one takes the key over.

Keys last 24 hours. A handler that fails releases its key, so the client can retry.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, text

from briefing_engine.api.problems import Problem
from briefing_engine.hashing import canonical_json, sha256

LOCK_S = 60
KEEP_H = 24


@dataclass(frozen=True)
class Stored:
    status: int
    body: Any


def request_hash(method: str, path: str, body: Any) -> bytes:
    return sha256(canonical_json({"method": method, "path": path, "body": body}))


def claim(engine: Engine, user_id: uuid.UUID, key: str, req_hash: bytes) -> Stored | None:
    """None means this request owns the key and must run the handler."""
    if not 1 <= len(key) <= 255:
        raise Problem(
            400, "invalid-idempotency-key", "Idempotency-Key must be 1 to 255 characters."
        )
    params = {"u": user_id, "k": key, "h": req_hash, "lock": LOCK_S, "keep": KEEP_H}
    with engine.begin() as conn:
        owned = conn.execute(
            text(
                """
                INSERT INTO idempotency_keys (user_id, key, request_hash, state, locked_until,
                                              expires_at)
                VALUES (:u, :k, :h, 'in_progress', now() + make_interval(secs => :lock),
                        now() + make_interval(hours => :keep))
                ON CONFLICT (user_id, key) DO NOTHING
                RETURNING 1
                """
            ),
            params,
        ).first()
        if owned:
            return None
        row = conn.execute(
            text(
                """
                SELECT request_hash, state, response_status, response_body,
                       locked_until < now() AS lock_expired, expires_at < now() AS expired
                FROM idempotency_keys WHERE user_id = :u AND key = :k
                FOR UPDATE
                """
            ),
            params,
        ).one()
        if row.expired or (
            row.state == "in_progress" and row.lock_expired and row.request_hash == req_hash
        ):
            # An old key not yet purged, or a crashed first request: start afresh.
            conn.execute(
                text(
                    """
                    UPDATE idempotency_keys
                    SET request_hash = :h, state = 'in_progress', response_status = NULL,
                        response_body = NULL, locked_until = now() + make_interval(secs => :lock),
                        expires_at = now() + make_interval(hours => :keep)
                    WHERE user_id = :u AND key = :k
                    """
                ),
                params,
            )
            return None
        if row.request_hash != req_hash:
            raise Problem(
                422,
                "idempotency-key-reused",
                "This Idempotency-Key was used with a different request.",
            )
        if row.state == "completed":
            return Stored(status=row.response_status, body=row.response_body)
        raise Problem(
            409,
            "idempotency-request-in-progress",
            "A request with this Idempotency-Key is still being processed.",
            headers={"Retry-After": "1"},
        )


def finish(conn: Connection, user_id: uuid.UUID, key: str, status: int, body: Any) -> None:
    conn.execute(
        text(
            """
            UPDATE idempotency_keys
            SET state = 'completed', response_status = :s, response_body = CAST(:b AS jsonb)
            WHERE user_id = :u AND key = :k
            """
        ),
        {"u": user_id, "k": key, "s": status, "b": canonical_json(body).decode("utf-8")},
    )


def release(engine: Engine, user_id: uuid.UUID, key: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM idempotency_keys "
                "WHERE user_id = :u AND key = :k AND state = 'in_progress'"
            ),
            {"u": user_id, "k": key},
        )


def purge(conn: Connection) -> int:
    return conn.execute(text("DELETE FROM idempotency_keys WHERE expires_at < now()")).rowcount
