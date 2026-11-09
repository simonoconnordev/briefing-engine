"""Signed keyset cursors (§5.4, ADR-008).

A cursor is `<payload>.<sig>`: base64url JSON holding the resource, the last row's sort
key and a hash of the filters, signed with HMAC-SHA256. A client can't edit it, use it on
another endpoint, or change the filters halfway through a walk and silently skip rows.
"""

import base64
import hmac
import json
from typing import Any

from briefing_engine.hashing import canonical_json, sha256

VERSION = 1


class InvalidCursor(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def filter_hash(filters: dict[str, Any]) -> str:
    return sha256(canonical_json({k: v for k, v in filters.items() if v is not None}))[:8].hex()


def _sig(key: bytes, payload: str) -> str:
    return _b64(hmac.new(key, payload.encode("ascii"), "sha256").digest()[:16])


def encode(key: bytes, resource: str, sort_key: list[Any], filters: dict[str, Any]) -> str:
    body = {"v": VERSION, "r": resource, "k": sort_key, "f": filter_hash(filters)}
    payload = _b64(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_sig(key, payload)}"


def decode(key: bytes, cursor: str, resource: str, filters: dict[str, Any]) -> list[Any]:
    payload, _, sig = cursor.partition(".")
    try:
        ok = hmac.compare_digest(sig.encode("ascii"), _sig(key, payload).encode("ascii"))
    except UnicodeEncodeError, ValueError:
        ok = False
    if not ok:
        raise InvalidCursor("the cursor's signature doesn't match")
    try:
        body = json.loads(_unb64(payload))
    except ValueError as exc:
        raise InvalidCursor("the cursor is malformed") from exc
    if body.get("v") != VERSION or body.get("r") != resource:
        raise InvalidCursor("the cursor belongs to another list")
    if body.get("f") != filter_hash(filters):
        raise InvalidCursor("the filters changed since this cursor was issued")
    sort_key = body.get("k")
    if not isinstance(sort_key, list) or len(sort_key) != 2:
        raise InvalidCursor("the cursor is malformed")
    return sort_key
