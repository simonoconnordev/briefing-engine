"""The REST API (§5). Every route but the health checks needs a key (§5.3).

Run with `briefing api`. Interactive docs are at /docs, the OpenAPI document at
/openapi.json. Work the API asks for (building a briefing, replaying a delivery) is queued
for `briefing worker`; the API itself never calls a model or sends an email.
"""

import datetime as dt
import logging
import re
import time as time_mod
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import date, datetime, time
from typing import Annotated, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine, text

from briefing_engine.api import cursor as cursors
from briefing_engine.api import idempotency, keys
from briefing_engine.api.keys import Principal
from briefing_engine.api.problems import Problem, install, request_id_var
from briefing_engine.api.ssrf import Resolver, UrlNotAllowed, check_url, system_resolver
from briefing_engine.db.ids import new_id
from briefing_engine.ingest.urls import normalise_url
from briefing_engine.llm.pricing import load_prices
from briefing_engine.llm.rubric import Rubric, rubric_hash
from briefing_engine.queue.jobs import ReplayRefused, enqueue
from briefing_engine.replay import replay_job

log = logging.getLogger(__name__)

REQUIRED_SCHEMA = 5
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


# --- request bodies ----------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MePatch(_Body):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = None
    send_time: time | None = None


class KeyCreate(_Body):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=lambda: ["read"], min_length=1)


class SubscriptionCreate(_Body):
    url: str = Field(min_length=1, max_length=2000)
    profile: str
    kind: str = Field(default="rss", pattern="^(rss|atom)$")
    name: str | None = Field(default=None, max_length=200)
    weight: float = Field(default=1.0, gt=0, le=10)


class ProfileCreate(_Body):
    name: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9][a-z0-9-]*$")
    threshold: float = Field(ge=0, le=10)
    max_items: int = Field(default=12, ge=1, le=50)
    max_age_days: int = Field(default=7, ge=1, le=365)
    rubric: Rubric


class BriefingCreate(_Body):
    profile: str
    date: dt.date | None = None  # the user's local today when omitted


# --- plumbing ----------------------------------------------------------------------------


def _engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def principal(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    found = None
    if scheme.lower() == "bearer" and token:
        with _engine(request).begin() as conn:
            found = keys.authenticate(conn, token.strip())
    if found is None:
        raise Problem(
            401,
            "unauthorised",
            "A valid API key is required.",
            headers={"WWW-Authenticate": 'Bearer realm="briefing-engine"'},
        )
    return found


def needs(scope: str) -> Callable[[Principal], Principal]:
    def check(p: Annotated[Principal, Depends(principal)]) -> Principal:
        if scope not in p.scopes:
            raise Problem(
                403,
                "insufficient-scope",
                f"This key lacks the '{scope}' scope.",
                headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'},
            )
        return p

    return check


Reader = Annotated[Principal, Depends(needs("read"))]
Writer = Annotated[Principal, Depends(needs("write"))]
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]


def _cursor_key(request: Request) -> bytes:
    key: bytes = request.app.state.cursor_key
    return key


def _after(
    request: Request, cursor: str | None, resource: str, filters: dict[str, Any]
) -> list[Any]:
    if cursor is None:
        return [None, None]
    try:
        return cursors.decode(_cursor_key(request), cursor, resource, filters)
    except cursors.InvalidCursor as exc:
        raise Problem(400, "invalid-cursor", str(exc)) from exc


def _page(
    request: Request,
    rows: Sequence[Any],
    limit: int,
    resource: str,
    filters: dict[str, Any],
    sort_key: Callable[[Any], list[Any]],
    render: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        cursors.encode(
            _cursor_key(request), resource, jsonable_encoder(sort_key(rows[-1])), filters
        )
        if more
        else None
    )
    return {"data": [render(r) for r in rows], "next_cursor": next_cursor}


def _profile(conn: Connection, user_id: uuid.UUID, name: str | None) -> Any:
    """The named profile, or the user's only one when no name is given."""
    rows = conn.execute(
        text(
            """
            SELECT p.id, p.name, p.threshold, p.max_items, p.max_age_days, p.current_version,
                   p.created_at, pv.rubric, pv.rubric_hash
            FROM profiles p
            JOIN profile_versions pv ON pv.profile_id = p.id AND pv.version = p.current_version
            WHERE p.user_id = :u AND (CAST(:name AS text) IS NULL OR p.name = :name)
            ORDER BY p.name
            """
        ),
        {"u": user_id, "name": name},
    ).all()
    if name is not None and not rows:
        raise Problem(422, "unknown-profile", f"You have no profile named '{name}'.")
    if name is None and len(rows) != 1:
        raise Problem(422, "profile-required", "Say which profile: you have more than one.")
    return rows[0]


def _profile_json(p: Any) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "threshold": p.threshold,
        "max_items": p.max_items,
        "max_age_days": p.max_age_days,
        "current_version": p.current_version,
        "rubric": p.rubric,
        "created_at": p.created_at,
    }


def _clear_validators(
    conn: Connection, *, profile_id: uuid.UUID | None = None, source_id: int | None = None
) -> None:
    """A new rubric or a new subscription must see the feed's current posts (§6.4)."""
    conn.execute(
        text(
            """
            UPDATE sources SET etag = NULL, last_modified = NULL, last_body_sha256 = NULL
            WHERE id = :s OR id IN (SELECT source_id FROM subscriptions WHERE profile_id = :p)
            """
        ),
        {"s": source_id, "p": profile_id},
    )


def _run_idempotent(
    request: Request,
    p: Principal,
    key: str | None,
    body: Any,
    handler: Callable[[Connection], tuple[int, dict[str, Any]]],
) -> JSONResponse:
    engine = _engine(request)
    if key is None:
        with engine.begin() as conn:
            status, out = handler(conn)
        return JSONResponse(jsonable_encoder(out), status_code=status)
    req_hash = idempotency.request_hash(request.method, request.url.path, body)
    stored = idempotency.claim(engine, p.user_id, key, req_hash)
    if stored is not None:
        return JSONResponse(
            stored.body, status_code=stored.status, headers={"Idempotent-Replayed": "true"}
        )
    try:
        with engine.begin() as conn:
            status, out = handler(conn)
            encoded = jsonable_encoder(out)
            idempotency.finish(conn, p.user_id, key, status, encoded)
    except BaseException:
        idempotency.release(engine, p.user_id, key)
        raise
    return JSONResponse(encoded, status_code=status)


# --- the app -----------------------------------------------------------------------------


def create_app(engine: Engine, cursor_key: bytes, resolver: Resolver = system_resolver) -> FastAPI:
    app = FastAPI(
        title="briefing-engine",
        version="2",
        description="Feeds in, a scored and written-up daily briefing out. See docs/plan.md §5.",
    )
    app.state.engine = engine
    app.state.cursor_key = cursor_key
    app.state.resolver = resolver
    install(app)

    @app.middleware("http")
    async def request_ids(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        given = request.headers.get("x-request-id", "")
        rid = given if _REQUEST_ID.match(given) else str(new_id())
        token = request_id_var.set(rid)
        started = time_mod.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-Id"] = rid
            log.info(
                "%s %s %s %.1f ms",
                request.method,
                request.url.path,
                response.status_code,
                (time_mod.perf_counter() - started) * 1000,
            )
            return response
        finally:
            request_id_var.reset(token)

    # --- health (§12.3): no auth ---

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    def readyz() -> JSONResponse:
        try:
            with engine.connect() as conn:
                conn.execute(text("SET LOCAL statement_timeout = 2000"))
                version = conn.execute(text("SELECT schema_version FROM schema_meta")).scalar_one()
        except Exception:
            return JSONResponse({"status": "database unavailable"}, status_code=503)
        if version < REQUIRED_SCHEMA:
            return JSONResponse(
                {"status": f"schema {version} < {REQUIRED_SCHEMA}"}, status_code=503
            )
        return JSONResponse({"status": "ready", "schema_version": version})

    # --- me ---

    def _me(conn: Connection, user_id: uuid.UUID) -> dict[str, Any]:
        u = conn.execute(
            text(
                """
                SELECT u.id, u.email, u.display_name, u.timezone, u.send_time,
                       u.monthly_budget_micros, u.email_enabled,
                       (SELECT CAST(coalesce(sum(c.cost_micros), 0) AS bigint) FROM llm_calls c
                        WHERE c.user_id = u.id
                          AND c.created_at >= date_trunc('month', now())) AS spent
                FROM users u WHERE u.id = :u
                """
            ),
            {"u": user_id},
        ).one()
        return {
            "id": u.id,
            "email": u.email,
            "display_name": u.display_name,
            "timezone": u.timezone,
            "send_time": u.send_time.strftime("%H:%M"),
            "email_enabled": u.email_enabled,
            "budget": {
                "monthly_micros": u.monthly_budget_micros,
                "spent_micros": u.spent,
                "left_micros": max(0, u.monthly_budget_micros - u.spent),
            },
        }

    @app.get("/v1/me", tags=["me"])
    def get_me(p: Reader) -> dict[str, Any]:
        with engine.connect() as conn:
            return _me(conn, p.user_id)

    @app.patch("/v1/me", tags=["me"])
    def patch_me(body: MePatch, p: Writer) -> dict[str, Any]:
        if body.timezone is not None:
            try:
                ZoneInfo(body.timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise Problem(
                    422, "invalid-timezone", f"'{body.timezone}' is not an IANA timezone name."
                ) from exc
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE users SET display_name = coalesce(:name, display_name),
                                     timezone = coalesce(:tz, timezone),
                                     send_time = coalesce(:st, send_time)
                    WHERE id = :u
                    """
                ),
                {
                    "u": p.user_id,
                    "name": body.display_name,
                    "tz": body.timezone,
                    "st": body.send_time,
                },
            )
            return _me(conn, p.user_id)

    # --- API keys ---

    def _key_json(k: Any) -> dict[str, Any]:
        return {
            "id": k.id,
            "prefix": k.prefix,
            "name": k.name,
            "scopes": k.scopes,
            "created_at": k.created_at,
            "last_used_at": k.last_used_at,
            "expires_at": k.expires_at,
            "revoked_at": k.revoked_at,
        }

    @app.get("/v1/api-keys", tags=["api keys"])
    def list_keys(p: Reader) -> dict[str, Any]:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM api_keys WHERE user_id = :u ORDER BY created_at DESC"),
                {"u": p.user_id},
            ).all()
        return {"data": [_key_json(k) for k in rows]}

    @app.post("/v1/api-keys", tags=["api keys"], status_code=201)
    def create_key(body: KeyCreate, p: Writer) -> dict[str, Any]:
        wanted = set(body.scopes)
        if not wanted <= set(keys.SCOPES):
            raise Problem(422, "unknown-scope", f"Scopes must be some of {list(keys.SCOPES)}.")
        if not wanted <= p.scopes:  # a key can't mint a key stronger than itself
            raise Problem(403, "insufficient-scope", "A key can only grant scopes it has.")
        with engine.begin() as conn:
            new = keys.create_key(conn, p.user_id, body.name, sorted(wanted))
            row = conn.execute(text("SELECT * FROM api_keys WHERE id = :id"), {"id": new.id}).one()
        # The only response that ever contains the secret.
        return {**_key_json(row), "key": new.token}

    @app.delete("/v1/api-keys/{key_id}", tags=["api keys"], status_code=204)
    def revoke_key(key_id: uuid.UUID, p: Writer) -> Response:
        with engine.begin() as conn:
            found = conn.execute(
                text(
                    "UPDATE api_keys SET revoked_at = coalesce(revoked_at, now()) "
                    "WHERE id = :id AND user_id = :u RETURNING 1"
                ),
                {"id": key_id, "u": p.user_id},
            ).first()
        if found is None:
            raise Problem(404, "not-found", "No such key.")
        return Response(status_code=204)

    # --- subscriptions ---

    _SUB_SELECT = """
        SELECT sub.id, sub.weight, sub.created_at, p.name AS profile, s.id AS source_id,
               s.name, s.url, s.kind, s.status
        FROM subscriptions sub
        JOIN sources s ON s.id = sub.source_id
        JOIN profiles p ON p.id = sub.profile_id
    """

    def _sub_json(r: Any) -> dict[str, Any]:
        return {
            "id": r.id,
            "profile": r.profile,
            "weight": r.weight,
            "created_at": r.created_at,
            "source": {
                "id": r.source_id,
                "name": r.name,
                "url": r.url,
                "kind": r.kind,
                "status": r.status,
            },
        }

    @app.get("/v1/subscriptions", tags=["subscriptions"])
    def list_subscriptions(
        request: Request, p: Reader, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
    ) -> dict[str, Any]:
        t, last = _after(request, cursor, "subscriptions", {})
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    _SUB_SELECT
                    + """
                    WHERE sub.user_id = :u
                      AND (CAST(:t AS timestamptz) IS NULL
                           OR (sub.created_at, sub.id)
                              < (CAST(:t AS timestamptz), CAST(:id AS uuid)))
                    ORDER BY sub.created_at DESC, sub.id DESC
                    LIMIT :n
                    """
                ),
                {"u": p.user_id, "t": t, "id": last, "n": limit + 1},
            ).all()
        return _page(
            request, rows, limit, "subscriptions", {}, lambda r: [r.created_at, r.id], _sub_json
        )

    @app.post("/v1/subscriptions", tags=["subscriptions"], status_code=201)
    def subscribe(body: SubscriptionCreate, p: Writer, response: Response) -> dict[str, Any]:
        try:
            check_url(body.url, app.state.resolver)
        except UrlNotAllowed as exc:
            raise Problem(422, "url-not-allowed", str(exc)) from exc
        normalised = normalise_url(body.url)
        with engine.begin() as conn:
            profile = _profile(conn, p.user_id, body.profile)
            source_id = conn.execute(
                text(
                    """
                    INSERT INTO sources (kind, name, url, url_normalised, host)
                    VALUES (:kind, :name, :url, :norm, :host)
                    ON CONFLICT (url_normalised) DO UPDATE
                       SET status = CASE WHEN sources.status = 'paused' THEN 'active'
                                         ELSE sources.status END
                    RETURNING id
                    """
                ),
                {
                    "kind": body.kind,
                    "name": body.name or urlsplit(normalised).hostname,
                    "url": body.url,
                    "norm": normalised,
                    "host": urlsplit(normalised).hostname,
                },
            ).scalar_one()
            created = conn.execute(
                text(
                    """
                    INSERT INTO subscriptions (id, user_id, source_id, profile_id, weight)
                    VALUES (:id, :u, :s, :p, :w)
                    ON CONFLICT (profile_id, source_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {"id": new_id(), "u": p.user_id, "s": source_id, "p": profile.id, "w": body.weight},
            ).first()
            if created is not None:
                _clear_validators(conn, source_id=source_id)
            else:
                response.status_code = 200  # already subscribed: nothing changed
            row = conn.execute(
                text(_SUB_SELECT + " WHERE sub.profile_id = :p AND sub.source_id = :s"),
                {"p": profile.id, "s": source_id},
            ).one()
        return _sub_json(row)

    @app.delete("/v1/subscriptions/{sub_id}", tags=["subscriptions"], status_code=204)
    def unsubscribe(sub_id: uuid.UUID, p: Writer) -> Response:
        with engine.begin() as conn:
            source_id = conn.execute(
                text(
                    "DELETE FROM subscriptions WHERE id = :id AND user_id = :u RETURNING source_id"
                ),
                {"id": sub_id, "u": p.user_id},
            ).scalar_one_or_none()
            if source_id is None:
                raise Problem(404, "not-found", "No such subscription.")
            # A source nobody reads any more is paused, not deleted: its articles stay.
            conn.execute(
                text(
                    """
                    UPDATE sources SET status = 'paused'
                    WHERE id = :s AND status = 'active'
                      AND NOT EXISTS (SELECT 1 FROM subscriptions WHERE source_id = :s)
                    """
                ),
                {"s": source_id},
            )
        return Response(status_code=204)

    # --- profiles ---

    @app.get("/v1/profiles", tags=["profiles"])
    def list_profiles(p: Reader) -> dict[str, Any]:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT p.*, pv.rubric FROM profiles p
                    JOIN profile_versions pv
                      ON pv.profile_id = p.id AND pv.version = p.current_version
                    WHERE p.user_id = :u ORDER BY p.name
                    """
                ),
                {"u": p.user_id},
            ).all()
        return {"data": [_profile_json(r) for r in rows]}

    @app.post("/v1/profiles", tags=["profiles"], status_code=201)
    def create_profile(body: ProfileCreate, p: Writer) -> dict[str, Any]:
        from briefing_engine.setup_config import ProfileConfig, _upsert_profile

        with engine.begin() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM profiles WHERE user_id = :u AND name = :n"),
                {"u": p.user_id, "n": body.name},
            ).first()
            if exists:
                raise Problem(
                    409, "profile-exists", f"You already have a profile named '{body.name}'."
                )
            _upsert_profile(conn, p.user_id, ProfileConfig.model_validate(body.model_dump()))
            return _profile_json(_profile(conn, p.user_id, body.name))

    def _own_profile(conn: Connection, user_id: uuid.UUID, profile_id: uuid.UUID) -> Any:
        name = conn.execute(
            text("SELECT name FROM profiles WHERE id = :id AND user_id = :u"),
            {"id": profile_id, "u": user_id},
        ).scalar_one_or_none()
        if name is None:
            raise Problem(404, "not-found", "No such profile.")
        return _profile(conn, user_id, name)

    @app.get("/v1/profiles/{profile_id}", tags=["profiles"])
    def get_profile(profile_id: uuid.UUID, p: Reader) -> dict[str, Any]:
        with engine.connect() as conn:
            return _profile_json(_own_profile(conn, p.user_id, profile_id))

    @app.put("/v1/profiles/{profile_id}/rubric", tags=["profiles"])
    def put_rubric(profile_id: uuid.UUID, rubric: Rubric, p: Writer) -> dict[str, Any]:
        new_hash = rubric_hash(rubric)
        with engine.begin() as conn:
            current = conn.execute(
                text(
                    """
                    SELECT p.current_version, pv.rubric_hash FROM profiles p
                    JOIN profile_versions pv
                      ON pv.profile_id = p.id AND pv.version = p.current_version
                    WHERE p.id = :id AND p.user_id = :u
                    FOR UPDATE OF p
                    """
                ),
                {"id": profile_id, "u": p.user_id},
            ).first()
            if current is None:
                raise Problem(404, "not-found", "No such profile.")
            if current.rubric_hash == new_hash:  # an identical PUT changes nothing
                return {"version": current.current_version, "created": False}
            # Going back to an older rubric is still a new version: history stays linear,
            # and its cached scores are found again through the shared rubric hash.
            version = current.current_version + 1
            conn.execute(
                text(
                    """
                    INSERT INTO profile_versions (profile_id, version, rubric, rubric_hash)
                    VALUES (:p, :v, CAST(:r AS jsonb), :h)
                    """
                ),
                {
                    "p": profile_id,
                    "v": version,
                    "h": new_hash,
                    "r": rubric.model_dump_json(),
                },
            )
            conn.execute(
                text("UPDATE profiles SET current_version = :v WHERE id = :p"),
                {"p": profile_id, "v": version},
            )
            _clear_validators(conn, profile_id=profile_id)
        return {"version": version, "created": True}

    @app.get("/v1/profiles/{profile_id}/versions", tags=["profiles"])
    def profile_versions(profile_id: uuid.UUID, p: Reader) -> dict[str, Any]:
        with engine.connect() as conn:
            _own_profile(conn, p.user_id, profile_id)
            rows = conn.execute(
                text(
                    "SELECT version, rubric, rubric_hash, created_at FROM profile_versions "
                    "WHERE profile_id = :p ORDER BY version DESC"
                ),
                {"p": profile_id},
            ).all()
        return {
            "data": [
                {
                    "version": r.version,
                    "rubric": r.rubric,
                    "rubric_hash": r.rubric_hash.hex(),
                    "created_at": r.created_at,
                }
                for r in rows
            ]
        }

    # --- articles ---

    @app.get("/v1/articles", tags=["articles"])
    def list_articles(
        request: Request,
        p: Reader,
        profile: str | None = None,
        since: datetime | None = None,
        min_score: Annotated[int | None, Query(ge=0, le=10)] = None,
        source_id: int | None = None,
        limit: Limit = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        filters = {
            "profile": profile,
            "since": since and since.isoformat(),
            "min_score": min_score,
            "source_id": source_id,
        }
        t, last = _after(request, cursor, "articles", filters)
        with engine.connect() as conn:
            prof = _profile(conn, p.user_id, profile)
            rows = conn.execute(
                text(
                    """
                    SELECT a.id, a.title, a.url_canonical, a.published_at, a.first_seen_at,
                           sc.score, sc.reason, sc.topics,
                           ARRAY(SELECT s.name FROM article_sources x
                                 JOIN sources s ON s.id = x.source_id
                                 WHERE x.article_id = a.id ORDER BY s.name) AS sources
                    FROM articles a
                    LEFT JOIN LATERAL (
                        SELECT s.score, s.reason, s.topics FROM llm_scores s
                        WHERE s.input_hash = a.input_hash AND s.rubric_hash = :rh
                        ORDER BY s.created_at DESC LIMIT 1
                    ) sc ON true
                    WHERE EXISTS (
                        SELECT 1 FROM article_sources asrc
                        JOIN subscriptions sub ON sub.source_id = asrc.source_id
                        WHERE asrc.article_id = a.id AND sub.profile_id = :pid
                          AND (CAST(:src AS bigint) IS NULL OR asrc.source_id = :src))
                      AND (CAST(:since AS timestamptz) IS NULL OR a.first_seen_at >= :since)
                      AND (CAST(:min AS int) IS NULL OR sc.score >= :min)
                      AND (CAST(:t AS timestamptz) IS NULL
                           OR (a.first_seen_at, a.id)
                              < (CAST(:t AS timestamptz), CAST(:id AS bigint)))
                    ORDER BY a.first_seen_at DESC, a.id DESC
                    LIMIT :n
                    """
                ),
                {
                    "rh": prof.rubric_hash,
                    "pid": prof.id,
                    "src": source_id,
                    "since": since,
                    "min": min_score,
                    "t": t,
                    "id": last,
                    "n": limit + 1,
                },
            ).all()
        return _page(
            request,
            rows,
            limit,
            "articles",
            filters,
            lambda r: [r.first_seen_at, r.id],
            lambda r: {
                "id": r.id,
                "title": r.title,
                "url": r.url_canonical,
                "published_at": r.published_at,
                "first_seen_at": r.first_seen_at,
                "sources": r.sources,
                "profile": prof.name,
                "score": None
                if r.score is None
                else {"value": r.score, "reason": r.reason, "topics": r.topics},
            },
        )

    # --- briefings ---

    _BRIEFING_SELECT = """
        SELECT b.id, b.briefing_date, b.status, b.revision, b.profile_version, b.cost_micros,
               b.created_at, b.ready_at, b.writeup, p.name AS profile,
               (SELECT count(*) FROM briefing_items i WHERE i.briefing_id = b.id) AS items
        FROM briefings b JOIN profiles p ON p.id = b.profile_id
    """

    def _briefing_json(b: Any) -> dict[str, Any]:
        writeup = b.writeup or {}
        return {
            "id": b.id,
            "date": b.briefing_date,
            "profile": b.profile,
            "status": b.status,
            "headline": writeup.get("headline"),
            "item_count": b.items,
            "cost_micros": b.cost_micros,
            "created_at": b.created_at,
            "ready_at": b.ready_at,
        }

    @app.get("/v1/briefings", tags=["briefings"])
    def list_briefings(
        request: Request,
        p: Reader,
        profile: str | None = None,
        limit: Limit = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        filters = {"profile": profile}
        d, last = _after(request, cursor, "briefings", filters)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    _BRIEFING_SELECT
                    + """
                    WHERE b.user_id = :u AND (CAST(:profile AS text) IS NULL OR p.name = :profile)
                      AND (CAST(:d AS date) IS NULL
                           OR (b.briefing_date, b.id) < (CAST(:d AS date), CAST(:id AS uuid)))
                    ORDER BY b.briefing_date DESC, b.id DESC
                    LIMIT :n
                    """
                ),
                {"u": p.user_id, "profile": profile, "d": d, "id": last, "n": limit + 1},
            ).all()
        return _page(
            request,
            rows,
            limit,
            "briefings",
            filters,
            lambda r: [r.briefing_date, r.id],
            _briefing_json,
        )

    @app.get("/v1/briefings/{ref}", tags=["briefings"])
    def get_briefing(ref: str, p: Reader, profile: str | None = None) -> dict[str, Any]:
        with engine.connect() as conn:
            params: dict[str, Any]
            try:
                where, params = "b.id = :id", {"id": uuid.UUID(ref)}
            except ValueError:
                try:
                    day = date.fromisoformat(ref)
                except ValueError as exc:
                    raise Problem(404, "not-found", "Give a briefing id or a date.") from exc
                prof = _profile(conn, p.user_id, profile)
                where, params = (
                    "b.briefing_date = :d AND b.profile_id = :pid",
                    {"d": day, "pid": prof.id},
                )
            b = conn.execute(
                text(_BRIEFING_SELECT + f" WHERE b.user_id = :u AND {where}"),
                {"u": p.user_id, **params},
            ).first()
            if b is None:
                raise Problem(404, "not-found", "No such briefing.")
            items = conn.execute(
                text(
                    """
                    SELECT i.rank, i.score, i.blurb AS reason, a.id, a.title, a.url_canonical,
                           a.published_at
                    FROM briefing_items i JOIN articles a ON a.id = i.article_id
                    WHERE i.briefing_id = :b ORDER BY i.rank
                    """
                ),
                {"b": b.id},
            ).all()
        written = (b.writeup or {}).get("items", {})
        return {
            **_briefing_json(b),
            "intro": (b.writeup or {}).get("intro"),
            "items": [
                {
                    "rank": i.rank,
                    "score": i.score,
                    "reason": i.reason,
                    "blurb": written.get(str(i.id), {}).get("blurb"),
                    "story": written.get(str(i.id), {}).get("story") or None,
                    "article": {
                        "id": i.id,
                        "title": i.title,
                        "url": i.url_canonical,
                        "published_at": i.published_at,
                    },
                }
                for i in items
            ],
        }

    @app.post("/v1/briefings", tags=["briefings"], status_code=202)
    def build_briefing(
        request: Request,
        body: BriefingCreate,
        p: Writer,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        def handler(conn: Connection) -> tuple[int, dict[str, Any]]:
            prof = _profile(conn, p.user_id, body.profile)
            tz = conn.execute(
                text("SELECT timezone FROM users WHERE id = :u"), {"u": p.user_id}
            ).scalar_one()
            day = body.date or datetime.now(ZoneInfo(tz)).date()
            existing = conn.execute(
                text(
                    "SELECT id FROM briefings "
                    "WHERE user_id = :u AND profile_id = :p AND briefing_date = :d"
                ),
                {"u": p.user_id, "p": prof.id, "d": day},
            ).scalar_one_or_none()
            if existing is not None:
                return 200, {"briefing_id": existing, "date": day, "status": "exists"}
            dedupe = f"build:{p.user_id}:{prof.id}:{day.isoformat()}"
            job_id = enqueue(
                conn,
                "build",
                "build_briefing",
                {
                    "user_id": str(p.user_id),
                    "profile_id": str(prof.id),
                    "briefing_date": day.isoformat(),
                },
                dedupe_key=dedupe,
            )
            if job_id is None:  # already queued: the same work, so the same job
                job_id = conn.execute(
                    text(
                        "SELECT id FROM jobs "
                        "WHERE dedupe_key = :k AND state IN ('queued', 'running')"
                    ),
                    {"k": dedupe},
                ).scalar_one()
            return 202, {
                "job_id": job_id,
                "date": day,
                "status": "queued",
                "job_url": f"/v1/jobs/{job_id}",
            }

        return _run_idempotent(request, p, idempotency_key, jsonable_encoder(body), handler)

    # --- jobs ---

    @app.get("/v1/jobs/{job_id}", tags=["jobs"])
    def get_job(job_id: int, p: Reader) -> dict[str, Any]:
        with engine.connect() as conn:
            j = conn.execute(
                text(
                    """
                    SELECT j.id, j.queue, j.kind, j.state, j.attempts, j.max_attempts, j.run_at,
                           j.created_at, j.finished_at, j.last_error
                    FROM jobs j
                    WHERE j.id = :id AND (
                        j.payload->>'user_id' = CAST(:u AS text)
                        OR (j.queue = 'deliver' AND EXISTS (
                            SELECT 1 FROM deliveries d JOIN briefings b ON b.id = d.briefing_id
                            WHERE d.id::text = j.payload->>'delivery_id' AND b.user_id = :u)))
                    """
                ),
                {"id": job_id, "u": p.user_id},
            ).first()
        if j is None:  # someone else's job looks exactly like a missing one
            raise Problem(404, "not-found", "No such job.")
        return dict(j._mapping)

    # --- deliveries ---

    @app.get("/v1/deliveries", tags=["deliveries"])
    def list_deliveries(
        p: Reader,
        status: Annotated[str | None, Query(pattern="^(pending|in_flight|succeeded|dead)$")] = None,
        channel: Annotated[str | None, Query(pattern="^(email|webhook)$")] = None,
        limit: Limit = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT d.id, d.briefing_id, b.briefing_date, d.channel, d.status, d.attempts,
                           d.last_error, d.message_id, d.created_at, d.updated_at, d.succeeded_at
                    FROM deliveries d JOIN briefings b ON b.id = d.briefing_id
                    WHERE b.user_id = :u
                      AND (CAST(:status AS text) IS NULL OR d.status = :status)
                      AND (CAST(:channel AS text) IS NULL OR d.channel = :channel)
                    ORDER BY d.created_at DESC, d.id DESC LIMIT :n
                    """
                ),
                {"u": p.user_id, "status": status, "channel": channel, "n": limit},
            ).all()
        return {"data": [dict(r._mapping) for r in rows]}

    @app.post("/v1/deliveries/{delivery_id}/replay", tags=["deliveries"], status_code=202)
    def replay_delivery(
        request: Request,
        delivery_id: uuid.UUID,
        p: Writer,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        with engine.connect() as conn:
            d = conn.execute(
                text(
                    """
                    SELECT d.status,
                           (SELECT j.id FROM jobs j
                            WHERE j.queue = 'deliver' AND j.state = 'dead'
                              AND j.payload->>'delivery_id' = d.id::text
                            ORDER BY j.id DESC LIMIT 1) AS job_id
                    FROM deliveries d JOIN briefings b ON b.id = d.briefing_id
                    WHERE d.id = :id AND b.user_id = :u
                    """
                ),
                {"id": delivery_id, "u": p.user_id},
            ).first()
        if d is None:
            raise Problem(404, "not-found", "No such delivery.")

        def handler(_conn: Connection) -> tuple[int, dict[str, Any]]:
            if d.status != "dead" or d.job_id is None:
                raise Problem(
                    409,
                    "delivery-not-dead",
                    f"Only a dead delivery can be replayed; this one is {d.status}.",
                )
            try:
                job = replay_job(engine, d.job_id)
            except ReplayRefused as exc:
                raise Problem(409, "replay-refused", str(exc)) from exc
            return 202, {"job_id": job.id, "status": "queued", "job_url": f"/v1/jobs/{job.id}"}

        return _run_idempotent(request, p, idempotency_key, {}, handler)

    # --- usage ---

    @app.get("/v1/usage", tags=["usage"])
    def usage(
        p: Reader, period: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None
    ) -> dict[str, Any]:
        month = period or datetime.now().strftime("%Y-%m")
        start = date.fromisoformat(f"{month}-01")
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT purpose, model, count(*) AS calls,
                           CAST(sum(input_tokens) AS bigint) AS input,
                           CAST(sum(output_tokens) AS bigint) AS output,
                           CAST(sum(cache_read_tokens) AS bigint) AS cache_read,
                           CAST(sum(cache_write_tokens) AS bigint) AS cache_write,
                           CAST(sum(cost_micros) AS bigint) AS cost
                    FROM llm_calls
                    WHERE user_id = :u AND created_at >= :start
                      AND created_at < CAST(:start AS date) + interval '1 month'
                    GROUP BY purpose, model ORDER BY purpose, model
                    """
                ),
                {"u": p.user_id, "start": start},
            ).all()
            budget = conn.execute(
                text("SELECT monthly_budget_micros FROM users WHERE id = :u"), {"u": p.user_id}
            ).scalar_one()
        prices = load_prices()
        lines, spent, saved = [], 0, 0
        for r in rows:
            price = prices.get(r.model)
            # What the cached input would have cost at the full input rate, less what it did.
            savings = (r.cache_read * (price.input - price.cache_read) // 1_000_000) if price else 0
            spent += r.cost
            saved += savings
            lines.append(
                {
                    "purpose": r.purpose,
                    "model": r.model,
                    "calls": r.calls,
                    "input_tokens": r.input,
                    "output_tokens": r.output,
                    "cache_read_tokens": r.cache_read,
                    "cache_write_tokens": r.cache_write,
                    "cost_micros": r.cost,
                    "cache_savings_micros": savings,
                }
            )
        return {
            "period": month,
            "by_purpose_and_model": lines,
            "cost_micros": spent,
            "cache_savings_micros": saved,
            "budget_micros": budget,
            "budget_left_micros": max(0, budget - spent),
        }

    return app


def install_log_redaction(handlers: Iterable[logging.Handler] | None = None) -> None:
    """Keys never reach a log line, whichever logger writes it (§5.3)."""
    for handler in handlers if handlers is not None else logging.getLogger().handlers:
        handler.addFilter(keys.RedactKeys())
