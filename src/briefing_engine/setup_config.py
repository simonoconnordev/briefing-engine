"""`briefing load-config`: create or update the user, profiles, sources and subscriptions
from a small TOML file.

Editing a rubric inserts a new profile version; unchanged rubrics keep their version,
so their cached scores stay valid. The file is the list of subscriptions: a feed taken out
of it (or out of a profile) is unsubscribed, so it is no longer fetched, scored or sent.
Its source row and articles stay, so past briefings and marks keep their history.
"""

import json
import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine, text

from briefing_engine.db.ids import new_id
from briefing_engine.ingest.urls import normalise_url
from briefing_engine.llm.rubric import Rubric, rubric_hash


class UserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    display_name: str
    timezone: str = "Europe/Dublin"
    send_time: time = time(6, 30)


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    threshold: float = Field(ge=0, le=10)
    max_items: int = Field(default=12, ge=1, le=50)
    # Articles published longer ago than this are neither scored nor sent.
    max_age_days: int = Field(default=7, ge=1, le=365)
    rubric: Rubric


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    url: str
    kind: str = Field(pattern="^(rss|atom)$")
    profiles: list[str] = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0)
    # Feed categories whose entries are skipped, compared case-insensitively.
    exclude_categories: list[str] = Field(default_factory=list)


class BriefingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user: UserConfig
    profiles: list[ProfileConfig] = Field(min_length=1)
    sources: list[SourceConfig] = Field(min_length=1)


@dataclass
class LoadReport:
    profiles_new_version: list[str]
    sources: int
    subscriptions: int
    unsubscribed: int = 0


def read_config(path: Path) -> BriefingConfig:
    raw: dict[str, Any] = tomllib.loads(path.read_text("utf-8"))
    config = BriefingConfig.model_validate(raw)
    ZoneInfo(config.user.timezone)  # raises if the timezone name is wrong
    names = {p.name for p in config.profiles}
    for source in config.sources:
        unknown = set(source.profiles) - names
        if unknown:
            raise ValueError(f"source {source.name!r} names unknown profiles {sorted(unknown)}")
    return config


def _upsert_user(conn: Connection, user: UserConfig) -> Any:
    return conn.execute(
        text(
            """
            INSERT INTO users (id, email, display_name, timezone, send_time)
            VALUES (:id, :email, :name, :tz, :send_time)
            ON CONFLICT (email) DO UPDATE
               SET display_name = EXCLUDED.display_name, timezone = EXCLUDED.timezone,
                   send_time = EXCLUDED.send_time
            RETURNING id
            """
        ),
        {
            "id": new_id(),
            "email": user.email,
            "name": user.display_name,
            "tz": user.timezone,
            "send_time": user.send_time,
        },
    ).scalar_one()


def _upsert_profile(conn: Connection, user_id: Any, profile: ProfileConfig) -> tuple[Any, bool]:
    """Returns (profile id, whether a new rubric version was created)."""
    rubric_json = profile.rubric.as_json()
    new_hash = rubric_hash(profile.rubric)
    row = conn.execute(
        text(
            """
            SELECT p.id, p.current_version, pv.rubric_hash
            FROM profiles p
            JOIN profile_versions pv ON pv.profile_id = p.id AND pv.version = p.current_version
            WHERE p.user_id = :u AND p.name = :name
            """
        ),
        {"u": user_id, "name": profile.name},
    ).first()
    if row is None:
        profile_id, version = new_id(), 1
        conn.execute(
            text(
                """
                INSERT INTO profiles (id, user_id, name, current_version, threshold, max_items,
                                      max_age_days)
                VALUES (:id, :u, :name, 1, :threshold, :max_items, :max_age_days)
                """
            ),
            {
                "id": profile_id,
                "u": user_id,
                "name": profile.name,
                "threshold": profile.threshold,
                "max_items": profile.max_items,
                "max_age_days": profile.max_age_days,
            },
        )
    else:
        profile_id = row.id
        version = row.current_version + 1 if row.rubric_hash != new_hash else row.current_version
        conn.execute(
            text(
                "UPDATE profiles SET threshold = :threshold, max_items = :max_items, "
                "max_age_days = :max_age_days WHERE id = :id"
            ),
            {
                "id": profile_id,
                "threshold": profile.threshold,
                "max_items": profile.max_items,
                "max_age_days": profile.max_age_days,
            },
        )
        if version == row.current_version:
            return profile_id, False
    conn.execute(
        text(
            """
            INSERT INTO profile_versions (profile_id, version, rubric, rubric_hash)
            VALUES (:p, :v, CAST(:rubric AS jsonb), :h)
            """
        ),
        {"p": profile_id, "v": version, "rubric": json.dumps(rubric_json), "h": new_hash},
    )
    conn.execute(
        text("UPDATE profiles SET current_version = :v WHERE id = :p"),
        {"p": profile_id, "v": version},
    )
    return profile_id, True


def _upsert_source(conn: Connection, source: SourceConfig) -> Any:
    normalised = normalise_url(source.url)
    return conn.execute(
        text(
            """
            INSERT INTO sources (kind, name, url, url_normalised, host, exclude_categories)
            VALUES (:kind, :name, :url, :norm, :host, :exclude)
            ON CONFLICT (url_normalised) DO UPDATE
               SET name = EXCLUDED.name, kind = EXCLUDED.kind, url = EXCLUDED.url,
                   exclude_categories = EXCLUDED.exclude_categories
            RETURNING id
            """
        ),
        {
            "kind": source.kind,
            "name": source.name,
            "url": source.url,
            "norm": normalised,
            "host": urlsplit(normalised).hostname,
            "exclude": source.exclude_categories,
        },
    ).scalar_one()


def load_config(engine: Engine, config: BriefingConfig) -> LoadReport:
    report = LoadReport(profiles_new_version=[], sources=0, subscriptions=0)
    with engine.begin() as conn:
        user_id = _upsert_user(conn, config.user)
        profile_ids: dict[str, Any] = {}
        new_feeds_for: set[str] = set()
        kept: list[tuple[Any, int]] = []
        for profile in config.profiles:
            profile_id, created = _upsert_profile(conn, user_id, profile)
            profile_ids[profile.name] = profile_id
            if created:
                report.profiles_new_version.append(profile.name)
        for source in config.sources:
            source_id = _upsert_source(conn, source)
            report.sources += 1
            for name in source.profiles:
                inserted = conn.execute(
                    text(
                        """
                        INSERT INTO subscriptions (id, user_id, source_id, profile_id, weight)
                        VALUES (:id, :u, :s, :p, :w)
                        ON CONFLICT (profile_id, source_id) DO UPDATE SET weight = EXCLUDED.weight
                        RETURNING (xmax = 0) AS inserted
                        """
                    ),
                    {
                        "id": new_id(),
                        "u": user_id,
                        "s": source_id,
                        "p": profile_ids[name],
                        "w": source.weight,
                    },
                ).scalar_one()
                report.subscriptions += 1
                kept.append((profile_ids[name], source_id))
                if inserted:
                    new_feeds_for.add(name)
        report.unsubscribed = conn.execute(
            text(
                """
                DELETE FROM subscriptions
                WHERE user_id = :u
                  AND (profile_id, source_id) NOT IN (
                      SELECT * FROM unnest(CAST(:profiles AS uuid[]), CAST(:sources AS bigint[])))
                """
            ),
            {"u": user_id, "profiles": [p for p, _ in kept], "sources": [s for _, s in kept]},
        ).rowcount
        # A new rubric, or a feed new to a profile, must see the feed's current posts, so
        # the next fetch of those feeds is unconditional (no 304, no soft 304).
        changed = set(report.profiles_new_version) | new_feeds_for
        if changed:
            conn.execute(
                text(
                    """
                    UPDATE sources SET etag = NULL, last_modified = NULL, last_body_sha256 = NULL
                    WHERE id IN (SELECT source_id FROM subscriptions
                                 WHERE profile_id = ANY(:profiles))
                    """
                ),
                {"profiles": [profile_ids[name] for name in changed]},
            )
    return report
