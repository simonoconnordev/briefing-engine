"""The REST API against the real database: auth, scopes, problems, cursors, idempotency,
the SSRF guard and, above all, that one user never sees another's data."""

import logging
from collections.abc import Iterable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from briefing_engine.api import idempotency
from briefing_engine.api.app import create_app
from briefing_engine.api.keys import RedactKeys, create_key
from briefing_engine.api.problems import Problem
from briefing_engine.context import Context
from briefing_engine.db.ids import new_id
from briefing_engine.ingest.feeds import make_http_client
from briefing_engine.pipeline import run_daily
from briefing_engine.setup_config import load_config
from tests.integration.fakes import FakeMailer, FakeScorer, no_host_spacing
from tests.integration.test_pipeline import DAY, FEED, ROOT, one_feed_config

pytestmark = pytest.mark.integration

ADDRESSES = {
    "feeds.example": ["93.184.216.34"],
    "other.example": ["93.184.216.35"],
    "internal.example": ["10.0.0.7"],
    "mixed.example": ["93.184.216.36", "127.0.0.1"],
    "metadata.example": ["169.254.169.254"],
    "mapped.example": ["::ffff:192.168.1.1"],
}


def fake_resolver(host: str, _port: int) -> Iterable[str]:
    if host not in ADDRESSES:
        raise OSError("no such host")
    return ADDRESSES[host]


RUBRIC = {
    "description": "Practical AI engineering.",
    "interests": [{"topic": "agents", "weight": 5}],
    "avoid": ["funding rounds"],
    "scale_hint": "10 means I'd read it today.",
}


class Api:
    def __init__(self, db: Engine):
        self.db = db
        self.client = TestClient(create_app(db, b"k" * 32, resolver=fake_resolver))

    def key_for(self, user_id: Any, scopes: list[str]) -> str:
        with self.db.begin() as conn:
            return create_key(conn, user_id, "test", scopes).token

    def as_(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}


def new_user(db: Engine, email: str) -> Any:
    with db.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, timezone, send_time) "
                "VALUES (:id, :e, 'Test', 'Europe/Dublin', '06:30') RETURNING id"
            ),
            {"id": new_id(), "e": email},
        ).scalar_one()


@pytest.fixture
def day_run(db: Engine) -> Context:
    """A real day for the config's user: articles, scores, a briefing and a delivery."""
    load_config(db, one_feed_config())
    no_host_spacing(db)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=FEED))
    ctx = Context(
        engine=db,
        scorer=FakeScorer(),
        mailer=FakeMailer(),
        http_client=lambda: make_http_client(transport),
        worker_id="t",
    )
    run_daily(ctx, DAY)
    return ctx


@pytest.fixture
def api(db: Engine) -> Api:
    return Api(db)


def me_id(db: Engine) -> Any:
    with db.connect() as conn:
        return conn.execute(
            text("SELECT id FROM users WHERE email = 'you@example.com'")
        ).scalar_one()


# --- auth and problems -------------------------------------------------------------------


def test_a_missing_or_wrong_key_is_one_indistinguishable_401(api: Api, db: Engine) -> None:
    user = new_user(db, "a@example.com")
    good = api.key_for(user, ["read"])
    with db.begin() as conn:
        conn.execute(text("UPDATE api_keys SET revoked_at = now()"))
    tampered = good[:-1] + ("A" if good[-1] != "A" else "B")
    bodies = []
    for headers in ({}, api.as_("nonsense"), api.as_(tampered), api.as_(good)):
        r = api.client.get("/v1/me", headers=headers)
        assert r.status_code == 401
        assert r.headers["content-type"] == "application/problem+json"
        assert r.headers["www-authenticate"].startswith("Bearer")
        body = r.json()
        assert body["request_id"] == r.headers["x-request-id"]
        bodies.append({k: v for k, v in body.items() if k != "request_id"})
    assert all(b == bodies[0] for b in bodies)  # revoked looks exactly like made up


def test_a_read_key_cannot_write_or_mint_a_stronger_key(api: Api, db: Engine) -> None:
    user = new_user(db, "a@example.com")
    reader = api.as_(api.key_for(user, ["read"]))
    r = api.client.patch("/v1/me", json={"display_name": "X"}, headers=reader)
    assert r.status_code == 403
    assert r.json()["type"] == "/problems/insufficient-scope"
    assert 'error="insufficient_scope", scope="write"' in r.headers["www-authenticate"]

    writer = api.as_(api.key_for(user, ["write"]))  # write without read
    r = api.client.post("/v1/api-keys", json={"name": "x", "scopes": ["read"]}, headers=writer)
    assert r.status_code == 403  # it can't grant the read scope it doesn't have


def test_a_new_key_is_shown_once_and_works(api: Api, db: Engine) -> None:
    user = new_user(db, "a@example.com")
    admin = api.as_(api.key_for(user, ["read", "write"]))
    r = api.client.post(
        "/v1/api-keys", json={"name": "dashboard", "scopes": ["read"]}, headers=admin
    )
    assert r.status_code == 201
    token = r.json()["key"]
    assert token.startswith("bek_") and r.json()["prefix"] in token
    listed = api.client.get("/v1/api-keys", headers=admin).json()["data"]
    assert len(listed) == 2 and all("key" not in k for k in listed)
    assert api.client.get("/v1/me", headers=api.as_(token)).status_code == 200

    api.client.delete(f"/v1/api-keys/{r.json()['id']}", headers=admin)
    assert api.client.get("/v1/me", headers=api.as_(token)).status_code == 401  # at once


def test_request_ids_are_echoed_or_made(api: Api) -> None:
    assert (
        api.client.get("/healthz", headers={"X-Request-Id": "abc-123"}).headers["x-request-id"]
        == "abc-123"
    )
    made = api.client.get("/healthz", headers={"X-Request-Id": "bad id with spaces"})
    assert made.headers["x-request-id"] != "bad id with spaces"


def test_validation_errors_name_each_field(api: Api, db: Engine) -> None:
    admin = api.as_(api.key_for(new_user(db, "a@example.com"), ["read", "write"]))
    r = api.client.post("/v1/profiles", json={"name": "Bad Name", "threshold": 11}, headers=admin)
    assert r.status_code == 422
    fields = {e["field"] for e in r.json()["errors"]}
    assert {"name", "threshold", "rubric"} <= fields


def test_health_and_readiness(api: Api) -> None:
    assert api.client.get("/healthz").json() == {"status": "ok"}
    ready = api.client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["schema_version"] >= 5


def test_keys_never_reach_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "header Bearer %s", ("bek_abcdefghijkl_secretsecret",), None
    )
    RedactKeys().filter(record)
    assert "secret" not in record.getMessage() and "bek_[redacted]" in record.getMessage()


# --- profiles, subscriptions and the SSRF guard ------------------------------------------


def onboard(api: Api, db: Engine, email: str) -> dict[str, str]:
    """A user set up only through the API, as stage 2's definition of done asks."""
    headers = api.as_(api.key_for(new_user(db, email), ["read", "write"]))
    r = api.client.post(
        "/v1/profiles", headers=headers, json={"name": "ai-news", "threshold": 6, "rubric": RUBRIC}
    )
    assert r.status_code == 201, r.text
    return headers


def test_subscribing_twice_is_one_subscription_and_sources_are_shared(api: Api, db: Engine) -> None:
    a = onboard(api, db, "a@example.com")
    b = onboard(api, db, "b@example.com")
    body = {"url": "https://feeds.example/atom.xml?utm_source=x", "profile": "ai-news"}
    first = api.client.post("/v1/subscriptions", json=body, headers=a)
    again = api.client.post("/v1/subscriptions", json=body, headers=a)
    other = api.client.post("/v1/subscriptions", json=body, headers=b)
    assert (first.status_code, again.status_code, other.status_code) == (201, 200, 201)
    assert first.json()["id"] == again.json()["id"]
    assert first.json()["source"]["id"] == other.json()["source"]["id"]  # one fetch for both
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM sources")).scalar_one() == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://internal.example/feed",
        "http://mixed.example/feed",
        "http://metadata.example/latest/meta-data",
        "http://mapped.example/feed",
        "http://127.0.0.1:5432/",
        "http://[::1]/feed",
        "file:///etc/passwd",
        "http://user:pass@feeds.example/feed",
        "http://unknown.example/feed",
    ],
)
def test_urls_that_dont_point_at_the_public_internet_are_refused(
    api: Api, db: Engine, url: str
) -> None:
    a = onboard(api, db, "a@example.com")
    r = api.client.post("/v1/subscriptions", json={"url": url, "profile": "ai-news"}, headers=a)
    assert r.status_code == 422
    assert r.json()["type"] == "/problems/url-not-allowed"
    assert "10.0.0.7" not in r.text and "169.254" not in r.text  # never says which address


def test_unsubscribing_the_last_reader_pauses_the_source(api: Api, db: Engine) -> None:
    a = onboard(api, db, "a@example.com")
    sub = api.client.post(
        "/v1/subscriptions",
        headers=a,
        json={"url": "https://feeds.example/x", "profile": "ai-news"},
    ).json()
    assert api.client.delete(f"/v1/subscriptions/{sub['id']}", headers=a).status_code == 204
    with db.connect() as conn:
        assert conn.execute(text("SELECT status FROM sources")).scalar_one() == "paused"
    again = api.client.post(
        "/v1/subscriptions",
        headers=a,
        json={"url": "https://feeds.example/x", "profile": "ai-news"},
    )
    assert again.json()["source"]["status"] == "active"


def test_subscriptions_page_with_signed_cursors(api: Api, db: Engine) -> None:
    a = onboard(api, db, "a@example.com")
    for n in range(5):
        api.client.post(
            "/v1/subscriptions",
            headers=a,
            json={"url": f"https://feeds.example/{n}", "profile": "ai-news"},
        )
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = api.client.get("/v1/subscriptions", params=params, headers=a).json()
        seen += [s["source"]["url"] for s in page["data"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == [f"https://feeds.example/{n}" for n in range(5)]
    assert len(set(seen)) == 5  # no repeats, none skipped

    first = api.client.get("/v1/subscriptions", params={"limit": 2}, headers=a).json()
    tampered = first["next_cursor"][:-2] + "AA"
    r = api.client.get("/v1/subscriptions", params={"cursor": tampered}, headers=a)
    assert r.status_code == 400 and r.json()["type"] == "/problems/invalid-cursor"


def test_a_cursor_is_bound_to_its_filters(api: Api, db: Engine, day_run: Context) -> None:
    me = api.as_(api.key_for(me_id(db), ["read"]))
    page = api.client.get("/v1/articles", params={"limit": 1}, headers=me).json()
    assert page["next_cursor"]
    changed = api.client.get(
        "/v1/articles", headers=me, params={"cursor": page["next_cursor"], "min_score": 8}
    )
    assert changed.status_code == 400
    assert "filters changed" in changed.json()["detail"]


def test_a_rubric_put_is_idempotent_and_history_stays_linear(api: Api, db: Engine) -> None:
    a = onboard(api, db, "a@example.com")
    profile = api.client.get("/v1/profiles", headers=a).json()["data"][0]
    url = f"/v1/profiles/{profile['id']}/rubric"
    same = api.client.put(url, json=RUBRIC, headers=a).json()
    edited = api.client.put(url, json={**RUBRIC, "avoid": []}, headers=a).json()
    back = api.client.put(url, json=RUBRIC, headers=a).json()
    assert same == {"version": 1, "created": False}
    assert edited == {"version": 2, "created": True}
    assert back == {"version": 3, "created": True}  # back to v1's rubric is a new version
    versions = api.client.get(f"/v1/profiles/{profile['id']}/versions", headers=a).json()
    hashes = [v["rubric_hash"] for v in versions["data"]]
    assert hashes[0] == hashes[2] != hashes[1]  # v3 shares v1's hash, so its cached scores


# --- articles, briefings, jobs and deliveries --------------------------------------------


def test_articles_carry_this_profiles_scores(api: Api, db: Engine, day_run: Context) -> None:
    me = api.as_(api.key_for(me_id(db), ["read"]))
    data = api.client.get("/v1/articles", headers=me).json()["data"]
    assert len(data) == 3 and all(a["score"] is not None for a in data)
    high = api.client.get("/v1/articles", params={"min_score": 8}, headers=me).json()["data"]
    assert [a["title"] for a in high] == [a["title"] for a in data if a["score"]["value"] >= 8]


def test_a_briefing_by_id_or_date(api: Api, db: Engine, day_run: Context) -> None:
    me = api.as_(api.key_for(me_id(db), ["read"]))
    [listed] = api.client.get("/v1/briefings", headers=me).json()["data"]
    by_id = api.client.get(f"/v1/briefings/{listed['id']}", headers=me).json()
    by_date = api.client.get(f"/v1/briefings/{DAY.isoformat()}", headers=me).json()
    assert by_id == by_date
    assert by_id["items"] and by_id["items"][0]["rank"] == 1
    assert by_id["items"][0]["reason"].startswith("Why:")


def test_no_user_sees_another_users_data(api: Api, db: Engine, day_run: Context) -> None:
    mine = api.as_(api.key_for(me_id(db), ["read", "write"]))
    other = onboard(api, db, "b@example.com")
    briefing = api.client.get("/v1/briefings", headers=mine).json()["data"][0]
    [sub] = api.client.get("/v1/subscriptions", headers=mine).json()["data"]
    with db.connect() as conn:
        job_id = conn.execute(text("SELECT id FROM jobs WHERE queue = 'build'")).scalar_one()
        delivery = conn.execute(text("SELECT id FROM deliveries")).scalar_one()

    assert api.client.get(f"/v1/briefings/{briefing['id']}", headers=other).status_code == 404
    assert api.client.get(f"/v1/jobs/{job_id}", headers=other).status_code == 404
    assert api.client.get(f"/v1/jobs/{job_id}", headers=mine).status_code == 200
    assert api.client.delete(f"/v1/subscriptions/{sub['id']}", headers=other).status_code == 404
    assert api.client.post(f"/v1/deliveries/{delivery}/replay", headers=other).status_code == 404
    for path in ("/v1/briefings", "/v1/subscriptions", "/v1/deliveries", "/v1/articles"):
        assert api.client.get(path, headers=other).json()["data"] == [], path
    assert api.client.get("/v1/usage", headers=other).json()["cost_micros"] == 0
    assert api.client.get("/v1/usage", headers=mine).json()["cost_micros"] > 0


def test_building_a_briefing_is_idempotent(api: Api, db: Engine) -> None:
    a = onboard(api, db, "a@example.com")
    body = {"profile": "ai-news", "date": "2026-11-20"}
    key = {"Idempotency-Key": "build-20-nov", **a}
    first = api.client.post("/v1/briefings", json=body, headers=key)
    replayed = api.client.post("/v1/briefings", json=body, headers=key)
    assert first.status_code == replayed.status_code == 202
    assert replayed.json() == first.json()
    assert replayed.headers["idempotent-replayed"] == "true"

    reused = api.client.post("/v1/briefings", json={**body, "date": "2026-11-21"}, headers=key)
    assert reused.status_code == 422
    assert reused.json()["type"] == "/problems/idempotency-key-reused"

    # Without a key, the same work still maps to the same job (the dedupe key).
    plain = api.client.post("/v1/briefings", json=body, headers=a)
    assert plain.json()["job_id"] == first.json()["job_id"]
    job = api.client.get(f"/v1/jobs/{first.json()['job_id']}", headers=a).json()
    assert job["state"] == "queued" and job["queue"] == "build"


def test_a_concurrent_request_with_the_same_key_gets_409(api: Api, db: Engine) -> None:
    user = new_user(db, "a@example.com")
    req = idempotency.request_hash("POST", "/v1/briefings", {"x": 1})
    assert idempotency.claim(db, user, "k1", req) is None  # the first request owns it...
    with pytest.raises(Problem) as busy:  # ...so a second one, while it runs, is told to wait
        idempotency.claim(db, user, "k1", req)
    assert busy.value.status == 409 and busy.value.headers == {"Retry-After": "1"}
    with db.begin() as conn:  # the first request crashed: its lock runs out
        conn.execute(text("UPDATE idempotency_keys SET locked_until = now() - interval '1 s'"))
    assert idempotency.claim(db, user, "k1", req) is None  # and the next one takes over


def test_only_a_dead_delivery_can_be_replayed(api: Api, db: Engine, day_run: Context) -> None:
    me = api.as_(api.key_for(me_id(db), ["read", "write"]))
    [delivery] = api.client.get("/v1/deliveries", headers=me).json()["data"]
    assert delivery["status"] == "succeeded"
    r = api.client.post(f"/v1/deliveries/{delivery['id']}/replay", headers=me)
    assert r.status_code == 409 and r.json()["type"] == "/problems/delivery-not-dead"

    with db.begin() as conn:
        conn.execute(text("UPDATE deliveries SET status = 'dead'"))
        conn.execute(text("UPDATE jobs SET state = 'dead' WHERE queue = 'deliver'"))
    headers = {"Idempotency-Key": "replay-1", **me}
    r = api.client.post(f"/v1/deliveries/{delivery['id']}/replay", headers=headers)
    again = api.client.post(f"/v1/deliveries/{delivery['id']}/replay", headers=headers)
    assert r.status_code == 202 and again.json() == r.json()
    with db.connect() as conn:
        assert conn.execute(text("SELECT status FROM deliveries")).scalar_one() == "pending"


def test_patch_me_checks_the_timezone(api: Api, db: Engine) -> None:
    a = api.as_(api.key_for(new_user(db, "a@example.com"), ["read", "write"]))
    bad = api.client.patch("/v1/me", json={"timezone": "Mars/Olympus"}, headers=a)
    assert bad.status_code == 422 and bad.json()["type"] == "/problems/invalid-timezone"
    ok = api.client.patch(
        "/v1/me", json={"timezone": "America/New_York", "send_time": "07:15"}, headers=a
    ).json()
    assert (ok["timezone"], ok["send_time"]) == ("America/New_York", "07:15")


def test_the_openapi_document_lists_the_api(api: Api) -> None:
    paths = api.client.get("/openapi.json").json()["paths"]
    assert {"/v1/me", "/v1/subscriptions", "/v1/briefings", "/v1/usage"} <= set(paths)


_ = ROOT  # the example config lives at the repo root
