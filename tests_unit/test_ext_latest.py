"""Bid Track update notice: the extension asks which version is the latest and where to download it (Railway settings
EXT_LATEST_VERSION / EXT_DOWNLOAD_URL); nothing to announce while they are unset."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest


@pytest.fixture
async def api():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.routers import bidder_ext

    app = FastAPI()
    app.include_router(bidder_ext.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def settings(monkeypatch, **values):
    from app.config import get_settings
    from app.routers import bidder_ext

    s = get_settings().model_copy(update=values)
    monkeypatch.setattr(bidder_ext, "get_settings", lambda: s)


async def test_latest_version_and_download_link(api, monkeypatch):
    settings(monkeypatch, EXT_LATEST_VERSION="3.5.0", EXT_DOWNLOAD_URL="https://files.example/bid-track-3.5.0.zip")
    r = await api.get("/ext/latest")
    assert r.status_code == 200
    assert r.json() == {"version": "3.5.0", "url": "https://files.example/bid-track-3.5.0.zip"}


async def test_nothing_to_announce_while_unset(api, monkeypatch):
    settings(monkeypatch, EXT_LATEST_VERSION=None, EXT_DOWNLOAD_URL=None)
    assert (await api.get("/ext/latest")).json() == {"version": None, "url": None}
