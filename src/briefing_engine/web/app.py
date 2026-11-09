"""A small local web app for marking articles and reading the per-feed report.

Run with `briefing web`. It listens on 127.0.0.1 only and acts for one user. Pages:

    /                      the latest briefing
    /briefings/{id}        a briefing's items, each with Interesting / Not for me
    /review                every article scored in the last few days, scores hidden until marked
    /report                what each feed contributes, and where Claude and you disagree

Marking is a plain HTML form POST, so it works without JavaScript. Cross-site posts are
refused using the browser's Sec-Fetch-Site and Origin headers.
"""

import uuid
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from sqlalchemy import Engine

from briefing_engine.delivery.render import long_date, safe_href, short_date
from briefing_engine.feedback import (
    FeedbackError,
    Profile,
    briefing_view,
    latest_briefing_id,
    profiles,
    record,
    resolve_user,
    review_items,
)
from briefing_engine.report import agreement, source_report

_env = Environment(
    loader=PackageLoader("briefing_engine.web", "templates"),
    autoescape=select_autoescape(enabled_extensions=("html",), default=True),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["long_date"] = long_date
_env.filters["short_date"] = short_date
_env.filters["href"] = safe_href


def _page(name: str, **context: object) -> HTMLResponse:
    return HTMLResponse(_env.get_template(name).render(**context))


def _same_origin(request: Request) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if origin and origin != "null":
        host = request.headers.get("host", "")
        return urlsplit(origin).netloc == host
    return True


def create_app(engine: Engine, user_email: str | None = None) -> FastAPI:
    app = FastAPI(title="briefing-engine", docs_url=None, redoc_url=None, openapi_url=None)

    def user_id() -> uuid.UUID:
        with engine.connect() as conn:
            try:
                return resolve_user(conn, user_email)
            except FeedbackError as exc:
                raise HTTPException(500, str(exc)) from exc

    def pick_profile(uid: uuid.UUID, name: str | None) -> tuple[Profile, list[Profile]]:
        with engine.connect() as conn:
            all_profiles = profiles(conn, uid)
        if not all_profiles:
            raise HTTPException(404, "no profiles yet; run `briefing load-config`")
        for p in all_profiles:
            if name is None or p.name == name:
                return p, all_profiles
        raise HTTPException(404, f"no profile named {name!r}")

    @app.get("/", response_model=None)
    def home() -> Response:
        uid = user_id()
        with engine.connect() as conn:
            latest = latest_briefing_id(conn, uid)
        return RedirectResponse(f"/briefings/{latest}" if latest else "/review", 303)

    @app.get("/briefings/{briefing_id}")
    def show_briefing(briefing_id: uuid.UUID) -> HTMLResponse:
        uid = user_id()
        with engine.connect() as conn:
            try:
                view = briefing_view(conn, uid, briefing_id)
            except FeedbackError as exc:
                raise HTTPException(404, str(exc)) from exc
        return _page("briefing.html", view=view, back=f"/briefings/{briefing_id}", tab="briefing")

    @app.get("/review")
    def review(
        days: Annotated[int, Query(ge=1, le=60)] = 7,
        profile: str | None = None,
    ) -> HTMLResponse:
        uid = user_id()
        chosen, all_profiles = pick_profile(uid, profile)
        with engine.connect() as conn:
            items = review_items(conn, uid, chosen, days)
        back = f"/review?days={days}&profile={chosen.name}"
        return _page(
            "review.html",
            items=items,
            profile=chosen,
            profiles=all_profiles,
            days=days,
            back=back,
            unmarked=sum(1 for i in items if i.verdict is None),
            tab="review",
        )

    @app.get("/report")
    def report(
        days: Annotated[int, Query(ge=1, le=365)] = 30,
        profile: str | None = None,
    ) -> HTMLResponse:
        uid = user_id()
        chosen, all_profiles = pick_profile(uid, profile)
        with engine.connect() as conn:
            rows = source_report(conn, uid, chosen, days)
            agree = agreement(conn, uid, chosen)
        return _page(
            "report.html",
            rows=rows,
            agree=agree,
            profile=chosen,
            profiles=all_profiles,
            days=days,
            tab="report",
        )

    @app.post("/feedback", response_model=None)
    def feedback(
        request: Request,
        article_id: Annotated[int, Form()],
        verdict: Annotated[str, Form()],
        context: Annotated[str, Form()],
        back: Annotated[str, Form()],
        note: Annotated[str, Form()] = "",
    ) -> Response:
        if not _same_origin(request):
            raise HTTPException(403, "cross-site request refused")
        if not back.startswith("/") or back.startswith("//"):
            raise HTTPException(400, "bad return address")
        uid = user_id()
        with engine.begin() as conn:
            try:
                record(conn, uid, article_id, verdict, note, context)
            except FeedbackError as exc:
                raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(f"{back}#a{article_id}", 303)

    return app
