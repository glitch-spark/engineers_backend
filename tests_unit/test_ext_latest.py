"""Bid Track updates from the server: the packaged zip shipped with the server (app/static/bid-track.zip) is served at
/ext/download, and /ext/latest names its version (read from the zip's manifest) and that link, so an older extension
shows "Update available". EXT_LATEST_VERSION / EXT_DOWNLOAD_URL override both; no zip and no settings = no notice."""
import io
import json
import os
import zipfile

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

    s = get_settings().model_copy(update={"EXT_LATEST_VERSION": None, "EXT_DOWNLOAD_URL": None, **values})
    monkeypatch.setattr(bidder_ext, "get_settings", lambda: s)


def package(monkeypatch, tmp_path, version="3.5.2"):
    from app.routers import bidder_ext

    path = tmp_path / "bid-track.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"name": "Bid Track", "version": version}))
        z.writestr("background.js", "// x")
    path.write_bytes(buf.getvalue())
    monkeypatch.setattr(bidder_ext, "EXT_PACKAGE", path)
    return buf.getvalue()


async def test_the_shipped_package_is_the_latest_and_downloads_here(api, monkeypatch, tmp_path):
    settings(monkeypatch)
    body = package(monkeypatch, tmp_path)
    assert (await api.get("/ext/latest")).json() == {"version": "3.5.2", "url": "http://test/ext/download"}
    r = await api.get("/ext/download")
    assert r.status_code == 200 and r.content == body
    assert r.headers["content-type"] == "application/zip"
    assert 'filename="bid-track-3.5.2.zip"' in r.headers["content-disposition"]


async def test_settings_override_the_package(api, monkeypatch, tmp_path):
    settings(monkeypatch, EXT_LATEST_VERSION="3.6.0", EXT_DOWNLOAD_URL="https://files.example/bt.zip")
    package(monkeypatch, tmp_path)
    assert (await api.get("/ext/latest")).json() == {"version": "3.6.0", "url": "https://files.example/bt.zip"}


async def test_nothing_to_announce_without_a_package_or_settings(api, monkeypatch, tmp_path):
    from app.routers import bidder_ext

    settings(monkeypatch)
    monkeypatch.setattr(bidder_ext, "EXT_PACKAGE", tmp_path / "missing.zip")
    assert (await api.get("/ext/latest")).json() == {"version": None, "url": None}
    assert (await api.get("/ext/download")).status_code == 404
