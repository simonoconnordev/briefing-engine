"""`briefing load-config`: create or update the user, profiles, sources and subscriptions
from a small TOML file.

Editing a rubric inserts a new profile version; unchanged rubrics keep their version,
so their cached scores stay valid. Nothing is deleted: removing a source from the file
leaves its row in place.
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
    rubric: Rubric


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    url: str
    kind: str = Field(pattern="^(rss|atom)$")
    profiles: list[str] = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0)


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
                INSERT INTO profiles (id, user_id, name, current_version, threshold, max_items)
                VALUES (:id, :u, :name, 1, :threshold, :max_items)
                """
            ),
            {
                "id": profile_id,
                "u": user_id,
                "name": profile.name,
                "threshold": profile.threshold,
                "max_items": profile.max_items,
            },
        )
    else:
        profile_id = row.id
        version = row.current_version + 1 if row.rubric_hash != new_hash else row.current_version
        conn.execute(
            text(
                "UPDATE profiles SET threshold = :threshold, max_items = :max_items WHERE id = :id"
            ),
            {"id": profile_id, "threshold": profile.threshold, "max_items": profile.max_items},
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
            INSERT INTO sources (kind, name, url, url_normalised, host)
            VALUES (:kind, :name, :url, :norm, :host)
            ON CONFLICT (url_normalised) DO UPDATE
               SET name = EXCLUDED.name, kind = EXCLUDED.kind, url = EXCLUDED.url
            RETURNING id
            """
        ),
        {
            "kind": source.kind,
            "name": source.name,
            "url": source.url,
            "norm": normalised,
            "host": urlsplit(normalised).hostname,
        },
    ).scalar_one()


def load_config(engine: Engine, config: BriefingConfig) -> LoadReport:
    report = LoadReport(profiles_new_version=[], sources=0, subscriptions=0)
    with engine.begin() as conn:
        user_id = _upsert_user(conn, config.user)
        profile_ids: dict[str, Any] = {}
        for profile in config.profiles:
            profile_id, created = _upsert_profile(conn, user_id, profile)
            profile_ids[profile.name] = profile_id
            if created:
                report.profiles_new_version.append(profile.name)
        for source in config.sources:
            source_id = _upsert_source(conn, source)
            report.sources += 1
            for name in source.profiles:
                conn.execute(
                    text(
                        """
                        INSERT INTO subscriptions (id, user_id, source_id, profile_id, weight)
                        VALUES (:id, :u, :s, :p, :w)
                        ON CONFLICT (profile_id, source_id) DO UPDATE SET weight = EXCLUDED.weight
                        """
                    ),
                    {
                        "id": new_id(),
                        "u": user_id,
                        "s": source_id,
                        "p": profile_ids[name],
                        "w": source.weight,
                    },
                )
                report.subscriptions += 1
    return report
