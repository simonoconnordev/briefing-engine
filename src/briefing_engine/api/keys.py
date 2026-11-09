"""Per-user API keys (§5.3, ADR-004).

A key looks like `bek_<prefix>_<secret>`. The `bek_` marker lets secret scanners spot a
leaked key; the 12-character prefix is a non-secret lookup id; the secret is 32 random
bytes in base62. Only SHA-256 of the secret is stored: with 256 bits of entropy a fast
hash can't be brute-forced, and checking a key stays well under a millisecond.

Missing, malformed, wrong, revoked and expired keys all fail the same way, so a caller
can't tell which it was.
"""

import hmac
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, text

from briefing_engine.db.ids import new_id
from briefing_engine.hashing import sha256

SCOPES = ("read", "write")
PREFIX_LEN = 12
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_KEY = re.compile(r"^bek_([0-9A-Za-z]{12})_([0-9A-Za-z]{40,48})$")
KEY_PATTERN = re.compile(r"bek_[0-9A-Za-z_]+")  # for redacting logs


def _base62(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n:
        n, r = divmod(n, 62)
        out.append(_ALPHABET[r])
    return "".join(reversed(out)).rjust(43, "0")


@dataclass(frozen=True)
class NewKey:
    id: uuid.UUID
    prefix: str
    token: str  # shown once, never stored


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    key_id: uuid.UUID
    scopes: frozenset[str]


def generate() -> tuple[str, str, bytes]:
    """Returns (prefix, full token, hash of the secret)."""
    prefix = "".join(secrets.choice(_ALPHABET) for _ in range(PREFIX_LEN))
    secret = _base62(secrets.token_bytes(32))
    return prefix, f"bek_{prefix}_{secret}", sha256(secret.encode("ascii"))


def create_key(
    conn: Connection,
    user_id: uuid.UUID,
    name: str,
    scopes: list[str],
    expires_at: datetime | None = None,
) -> NewKey:
    unknown = set(scopes) - set(SCOPES)
    if unknown or not scopes:
        raise ValueError(f"scopes must be some of {SCOPES}")
    prefix, token, secret_hash = generate()
    key_id = new_id()
    conn.execute(
        text(
            """
            INSERT INTO api_keys (id, user_id, prefix, secret_hash, name, scopes, expires_at)
            VALUES (:id, :u, :prefix, :h, :name, :scopes, :expires)
            """
        ),
        {
            "id": key_id,
            "u": user_id,
            "prefix": prefix,
            "h": secret_hash,
            "name": name,
            "scopes": sorted(set(scopes)),
            "expires": expires_at,
        },
    )
    return NewKey(id=key_id, prefix=prefix, token=token)


def authenticate(conn: Connection, token: str) -> Principal | None:
    match = _KEY.match(token)
    if match is None:
        return None
    prefix, secret = match.groups()
    row = conn.execute(
        text(
            """
            SELECT k.id, k.user_id, k.secret_hash, k.scopes,
                   k.last_used_at IS NULL OR k.last_used_at < now() - interval '1 minute'
                       AS stale
            FROM api_keys k JOIN users u ON u.id = k.user_id
            WHERE k.prefix = :prefix AND k.revoked_at IS NULL
              AND (k.expires_at IS NULL OR k.expires_at > now())
              AND u.disabled_at IS NULL
            """
        ),
        {"prefix": prefix},
    ).first()
    if row is None or not hmac.compare_digest(row.secret_hash, sha256(secret.encode("ascii"))):
        return None
    if row.stale:  # at most one write a minute per key, so reads stay reads
        conn.execute(
            text("UPDATE api_keys SET last_used_at = now() WHERE id = :id"), {"id": row.id}
        )
    return Principal(user_id=row.user_id, key_id=row.id, scopes=frozenset(row.scopes))


class RedactKeys(logging.Filter):
    """Never log a key: anything that looks like one is replaced, wherever it appears."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "bek_" in message:
            record.msg = KEY_PATTERN.sub("bek_[redacted]", message)
            record.args = None
        return True
