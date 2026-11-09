"""Ids that appear outside the database are UUID v7, generated in the application.

UUID v7 sorts by creation time. Generating it here means the id exists before the insert,
so it can go straight into log lines and job payloads (ADR-015). If the project ever has
to fall back to Python 3.13, only this file changes (to the `uuid6` package).
"""

import uuid


def new_id() -> uuid.UUID:
    return uuid.uuid7()
