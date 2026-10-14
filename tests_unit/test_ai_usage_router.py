"""GET /ai-usage: admin-only AI spend report over the ai_usage_events ledger (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import AiUsageEvent, UserExtra

pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")

# Viewer in São Paulo (UTC-3): Oct 14 local = 03:00Z Oct 14 .. 03:00Z Oct 15.
OCT14 = {"from": "2026-10-14T03:00:00.000Z", "to": "2026-10-15T03:00:00.000Z", "tz": "America/Sao_Paulo"}
OCT13_14 = {"from": "2026-10-13T03:00:00.000Z", "to": "2026-10-15T03:00:00.000Z", "tz": "America/Sao_Paulo"}
OCT_WEEK = {"from": "2026-10-12T03:00:00.000Z", "to": "2026-10-19T03:00:00.000Z", "tz": "America/Sao_Paulo"}


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["ai-usage-router"]
    await init_beanie(database=database, document_models=[UserExtra, AiUsageEvent])
    return database


async def make_user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def event(at, user=None, feature="resume", model="claude-sonnet-5-5", cost=0.01, tin=1000, tout=500, **kw):
    return await AiUsageEvent(
        createdAt=at, userId=user.id if user else None, feature=feature, provider="anthropic", model=model,
        inputTokens=tin, outputTokens=tout, costUsd=cost, **kw,
    ).insert()


async def get(user, params):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import ai_usage

    app = FastAPI()
    app.include_router(ai_usage.router)
    app.dependency_overrides[get_current_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get("/ai-usage", params=params)


async def test_admin_only(db):
    pedro = await make_user("Pedro")
    assert (await get(pedro, OCT14)).status_code == 403


async def test_totals_rows_and_options(db):
    root, pedro, maria = await make_user("Root", "admin"), await make_user("Pedro"), await make_user("Maria")
    await event(datetime(2026, 10, 14, 12), pedro, "cover_letter", "gpt-5.6-sol", cost=0.30)
    await event(datetime(2026, 10, 14, 13), pedro, "cover_letter", "gpt-5.6-sol", cost=0.20)
    await event(datetime(2026, 10, 14, 14), maria, "resume", cost=0.10, estimated=True, source="backfill")
    await event(datetime(2026, 10, 14, 15), None, "job_extract", "gpt-4o-mini", cost=None)
    await event(datetime(2026, 10, 14, 1), pedro, cost=9.0)  # Oct 13 22:00 local: outside the day
    await event(datetime(2026, 10, 15, 4), pedro, cost=9.0)  # Oct 15 local: outside the day
    r = await get(root, OCT14)
    assert r.status_code == 200, r.text
    body = r.json()
    t = body["totals"]
    assert t["costUsd"] == pytest.approx(0.60) and t["calls"] == 4
    assert (t["inputTokens"], t["outputTokens"]) == (4000, 2000)
    assert t["estimatedCostUsd"] == pytest.approx(0.10) and t["unpricedCalls"] == 1
    rows = [(x["userName"], x["feature"], x["model"], x["calls"], round(x["costUsd"], 2)) for x in body["rows"]]
    assert rows == [
        ("Pedro", "cover_letter", "gpt-5.6-sol", 2, 0.50),
        ("Maria", "resume", "claude-sonnet-5-5", 1, 0.10),
        ("Unattributed", "job_extract", "gpt-4o-mini", 1, 0.0),
    ]
    opts = body["options"]
    assert [u["name"] for u in opts["users"]] == ["Maria", "Pedro", "Unattributed"]
    assert opts["features"] == ["cover_letter", "job_extract", "resume"]
    assert opts["models"] == ["claude-sonnet-5-5", "gpt-4o-mini", "gpt-5.6-sol"]


async def test_filters_by_user_feature_and_model(db):
    root, pedro, maria = await make_user("Root", "admin"), await make_user("Pedro"), await make_user("Maria")
    await event(datetime(2026, 10, 14, 12), pedro, "cover_letter", "gpt-5.6-sol", cost=0.30)
    await event(datetime(2026, 10, 14, 12), pedro, "resume", cost=0.05)
    await event(datetime(2026, 10, 14, 12), maria, "cover_letter", cost=0.10)
    await event(datetime(2026, 10, 14, 12), None, "job_extract", "gpt-4o-mini", cost=0.01)

    body = (await get(root, {**OCT14, "userId": [str(pedro.id)]})).json()
    assert body["totals"]["costUsd"] == pytest.approx(0.35)
    assert len(body["options"]["users"]) == 3  # options ignore the filters so the dropdowns don't shrink

    body = (await get(root, {**OCT14, "feature": ["cover_letter"], "model": ["claude-sonnet-5-5"]})).json()
    assert [(x["userName"], x["costUsd"]) for x in body["rows"]] == [("Maria", pytest.approx(0.10))]

    body = (await get(root, {**OCT14, "userId": ["unattributed"]})).json()
    assert [x["feature"] for x in body["rows"]] == ["job_extract"]


async def test_hour_buckets_follow_the_viewer_time_zone(db):
    root, pedro = await make_user("Root", "admin"), await make_user("Pedro")
    await event(datetime(2026, 10, 14, 3, 30), pedro, "resume", cost=0.10)       # 00:30 local
    await event(datetime(2026, 10, 14, 15, 10), pedro, "cover_letter", cost=0.20)  # 12:10 local
    body = (await get(root, OCT14)).json()
    assert body["bucket"] == "hour"
    series = body["series"]
    assert len(series) == 24 and series[0]["bucket"] == "2026-10-14T00:00"
    assert series[0]["costUsd"] == pytest.approx(0.10) and series[0]["byFeature"] == {"resume": pytest.approx(0.10)}
    assert series[12]["byFeature"] == {"cover_letter": pytest.approx(0.20)}
    assert series[12]["byUser"] == {str(pedro.id): pytest.approx(0.20)}
    assert series[12]["byModel"] == {"claude-sonnet-5-5": pytest.approx(0.20)}
    assert series[5]["costUsd"] == 0


async def test_day_buckets_follow_the_viewer_time_zone(db):
    root = await make_user("Root", "admin")
    await event(datetime(2026, 10, 14, 1), None, cost=0.10)   # Oct 13 22:00 local
    await event(datetime(2026, 10, 14, 4), None, cost=0.20)   # Oct 14 01:00 local
    body = (await get(root, OCT_WEEK)).json()
    assert body["bucket"] == "day"
    by_day = {s["bucket"]: s["costUsd"] for s in body["series"]}
    assert list(by_day) == [f"2026-10-{d}" for d in range(12, 19)]
    assert by_day["2026-10-13"] == pytest.approx(0.10) and by_day["2026-10-14"] == pytest.approx(0.20)


async def test_two_days_still_bucket_by_hour_and_bucket_can_be_forced(db):
    root = await make_user("Root", "admin")
    assert (await get(root, OCT13_14)).json()["bucket"] == "hour"
    body = (await get(root, {**OCT13_14, "bucket": "day"})).json()
    assert [s["bucket"] for s in body["series"]] == ["2026-10-13", "2026-10-14"]


@pytest.mark.parametrize("params", [
    {**OCT14, "from": OCT14["to"], "to": OCT14["from"]},          # from after to
    {**OCT14, "tz": "Mars/Olympus"},                              # unknown zone
    {**OCT14, "from": "2024-01-01T00:00:00Z"},                    # span over 366 days
    {**OCT14, "from": "yesterday"},                               # not a date
    {**OCT14, "from": "2026-10-11T03:00:00Z", "bucket": "hour"},  # 96 hour buckets: over the cap
])
async def test_bad_ranges_are_422(db, params):
    root = await make_user("Root", "admin")
    assert (await get(root, params)).status_code == 422


async def test_z_suffix_parses_without_python_311_fromisoformat(db, monkeypatch):
    """The production image runs Python 3.10, whose fromisoformat rejects a trailing "Z"."""
    from app.routers import ai_usage as router_mod

    class Py310Datetime(datetime):
        @classmethod
        def fromisoformat(cls, value):
            if value.endswith("Z"):
                raise ValueError(f"Invalid isoformat string: {value!r}")
            return super().fromisoformat(value)

    monkeypatch.setattr(router_mod, "datetime", Py310Datetime)
    root = await make_user("Root", "admin")
    assert (await get(root, OCT14)).status_code == 200


async def test_a_local_year_across_dst_is_accepted(db):
    # Nov 1 2025 -> Nov 1 2026 local in New York is 366 days and one hour of UTC.
    root = await make_user("Root", "admin")
    params = {"from": "2025-11-01T04:00:00.000Z", "to": "2026-11-02T05:00:00.000Z", "tz": "America/New_York"}
    assert (await get(root, params)).status_code == 200
