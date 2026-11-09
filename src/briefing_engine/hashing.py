"""Canonical JSON and the hashes built from it.

Two values that are equal as data must hash the same, whatever order their keys were
written in. Canonical JSON guarantees that: keys sorted, no extra whitespace, UTF-8.
"""

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()
