"""Marking items through the local web app, and the reports built from the marks."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from briefing_engine.context import Context
from briefing_engine.feedback import profiles, resolve_user
from briefing_engine.pipeline import run_daily
from briefing_engine.report import agreement, source_report
from briefing_engine.web.app import create_app
from tests.integration.test_pipeline import DAY, ctx  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration


def article_id(db: Engine, title_part: str) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(
                text("SELECT id FROM articles WHERE title LIKE :t"), {"t": f"%{title_part}%"}
            ).scalar_one()
        )


@pytest.fixture
def client(ctx: Context) -> TestClient:  # noqa: F811
    run_daily(ctx, DAY)
    return TestClient(create_app(ctx.engine))


def mark(client: TestClient, aid: int, verdict: str, note: str = "", **headers: str) -> int:
    response = client.post(
        "/feedback",
        data={
            "article_id": str(aid),
            "verdict": verdict,
            "note": note,
            "context": "review",
            "back": "/review",
        },
        headers=headers,
        follow_redirects=False,
    )
    return response.status_code


def test_home_opens_the_latest_briefing_with_escaped_titles(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "Building an agent with &lt;b&gt;tools&lt;/b&gt; &amp; tests" in page.text
    assert "Not for me" in page.text


def test_marking_records_and_replaces_a_verdict(client: TestClient, ctx: Context) -> None:  # noqa: F811
    aid = article_id(ctx.engine, "eval harness")
    assert mark(client, aid, "interesting", "I'd try this") == 303
    assert mark(client, aid, "not_for_me", "changed my mind") == 303
    with ctx.engine.connect() as conn:
        rows = conn.execute(text("SELECT verdict, note FROM article_feedback")).all()
    assert [(r.verdict, r.note) for r in rows] == [("not_for_me", "changed my mind")]


def test_cross_site_and_malformed_posts_are_refused(client: TestClient, ctx: Context) -> None:  # noqa: F811
    aid = article_id(ctx.engine, "eval harness")
    assert mark(client, aid, "interesting", **{"Sec-Fetch-Site": "cross-site"}) == 403
    assert mark(client, aid, "interesting", Origin="https://evil.example") == 403
    assert mark(client, aid, "loved it") == 400
    response = client.post(
        "/feedback",
        data={"article_id": str(aid), "verdict": "interesting", "context": "review",
              "back": "//evil.example/"},
        follow_redirects=False,
    )  # fmt: skip
    assert response.status_code == 400
    with ctx.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM article_feedback")).scalar_one() == 0


def test_review_hides_the_score_until_the_item_is_marked(client: TestClient, ctx: Context) -> None:  # noqa: F811
    page = client.get("/review?days=7")
    assert "Startup raises" in page.text  # below threshold, so never sent, but reviewable
    assert "Claude:" not in page.text and "3 of 3 still to mark" in page.text
    mark(client, article_id(ctx.engine, "Startup raises"), "interesting", "funding I care about")
    page = client.get("/review?days=7")
    assert "Claude:" in page.text and "2 of 3 still to mark" in page.text


def test_reports_count_per_feed_and_list_disagreements(client: TestClient, ctx: Context) -> None:  # noqa: F811
    mark(client, article_id(ctx.engine, "Startup raises"), "interesting", "funding I care about")
    mark(client, article_id(ctx.engine, "Building an agent"), "not_for_me", "too basic")
    mark(client, article_id(ctx.engine, "eval harness"), "interesting")
    with ctx.engine.connect() as conn:
        user = resolve_user(conn, None)
        [profile] = profiles(conn, user)
        [row] = source_report(conn, user, profile, 30)
        agree = agreement(conn, user, profile)
    assert (row.fetched, row.scored, row.above_threshold, row.sent) == (3, 3, 2, 2)
    assert (row.interesting, row.not_for_me) == (2, 1)
    assert agree.marked == 3 and agree.agreed == 1
    assert [d.title for d in agree.missed] == ["Startup raises $50M"]
    assert [d.note for d in agree.unwanted] == ["too basic"]
    page = client.get("/report")
    assert "Claude would skip these, you found them interesting" in page.text
