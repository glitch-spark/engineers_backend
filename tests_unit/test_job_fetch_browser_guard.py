"""The headless browser's route guard: every redirect hop is checked like the httpx path (no network, fake route)."""

import pytest

from app.job_apply import fetch as fetch_mod
from app.job_apply.fetch import FetchError, MAX_REDIRECTS, _browser_guard, _redirect_hop


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """'internal.corp' / '*.railway.internal' resolve privately, every other name publicly."""
    async def resolve(host):
        if host == "internal.corp" or host.endswith(".railway.internal"):
            return ["10.0.0.5"]
        return ["93.184.216.34"]

    monkeypatch.setattr(fetch_mod, "_resolve", resolve)


class _Resp:
    def __init__(self, status, location=None):
        self.status = status
        self.headers = {"location": location} if location else {}


class _Frame:
    parent_frame = None


class _Request:
    def __init__(self, url, *, nav=False, method="GET"):
        self.url = url
        self.method = method
        self._nav = nav
        self.frame = _Frame()

    def is_navigation_request(self):
        return self._nav


class _Route:
    """Answers route.fetch from a {url: response} map and records what the guard did."""

    def __init__(self, url, answers, **request_kw):
        self.request = _Request(url, **request_kw)
        self.answers = answers
        self.fetched = []
        self.fulfilled = None
        self.aborted = None

    async def fetch(self, *, url=None, max_redirects=None, timeout=None, method=None, post_data=None):
        assert max_redirects == 0, "the browser must never follow a redirect on its own"
        target = url or self.request.url
        self.fetched.append((target, method))
        return self.answers[target]

    async def fulfill(self, *, response):
        self.fulfilled = response

    async def abort(self, error_code="failed"):
        self.aborted = error_code


async def _run(route, state=None):
    await _browser_guard({} if state is None else state)(route)
    return route


async def test_redirect_hop_resolves_relative_location():
    assert await _redirect_hop("https://jobs.example.com/a/b", "/c") == "https://jobs.example.com/c"
    assert await _redirect_hop("https://jobs.example.com/a/b", " https://other.example.com/x ") == (
        "https://other.example.com/x"
    )


@pytest.mark.parametrize(
    "location",
    [
        None,
        "",
        "http://127.0.0.1:8080/",
        "http://10.1.2.3/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://api.railway.internal/",
        "http://internal.corp/",
        "https://jobs.example.com:8443/",
        "file:///etc/passwd",
        "gopher://jobs.example.com/",
    ],
)
async def test_redirect_hop_refuses_private_targets(location):
    with pytest.raises(FetchError):
        await _redirect_hop("https://jobs.example.com/a", location)


async def test_subresource_redirect_chain_followed_hop_by_hop():
    final = _Resp(200)
    route = await _run(_Route("https://cdn.example.com/a.js", {
        "https://cdn.example.com/a.js": _Resp(302, "/b.js"),
        "https://cdn.example.com/b.js": _Resp(301, "https://static.example.net/c.js"),
        "https://static.example.net/c.js": final,
    }))
    assert [u for u, _ in route.fetched] == [
        "https://cdn.example.com/a.js", "https://cdn.example.com/b.js", "https://static.example.net/c.js",
    ]
    assert route.fulfilled is final and route.aborted is None


@pytest.mark.parametrize(
    "target", ["http://127.0.0.1:8080/", "http://api.railway.internal/secret", "http://169.254.169.254/"],
)
async def test_subresource_redirect_into_private_network_is_aborted(target):
    route = await _run(_Route("https://cdn.example.com/a.js", {
        "https://cdn.example.com/a.js": _Resp(302, target),
    }))
    assert route.fetched == [("https://cdn.example.com/a.js", None)]  # the private target is never requested
    assert route.fulfilled is None and route.aborted == "blockedbyclient"


async def test_private_request_is_never_sent():
    route = await _run(_Route("http://10.0.0.1/admin", {}))
    assert route.fetched == [] and route.aborted == "blockedbyclient"


async def test_page_redirect_is_handed_back_to_be_opened():
    state = {}
    route = await _run(_Route("https://short.example.com/j", {
        "https://short.example.com/j": _Resp(301, "https://jobs.example.com/123"),
    }, nav=True), state)
    assert state == {"redirect": "https://jobs.example.com/123"}
    assert route.aborted == "aborted" and route.fulfilled is None


async def test_page_redirect_into_private_network_is_blocked():
    state = {}
    route = await _run(_Route("https://short.example.com/j", {
        "https://short.example.com/j": _Resp(302, "http://127.0.0.1:8080/"),
    }, nav=True), state)
    assert state == {} and route.aborted == "blockedbyclient"


async def test_redirect_loop_is_cut_off():
    route = await _run(_Route("https://cdn.example.com/loop", {
        "https://cdn.example.com/loop": _Resp(302, "/loop"),
    }))
    assert len(route.fetched) == MAX_REDIRECTS + 1
    assert route.aborted == "blockedbyclient" and route.fulfilled is None


async def test_see_other_after_post_becomes_get():
    route = await _run(_Route("https://jobs.example.com/apply", {
        "https://jobs.example.com/apply": _Resp(303, "/done"),
        "https://jobs.example.com/done": _Resp(200),
    }, method="POST"))
    assert route.fetched == [("https://jobs.example.com/apply", None), ("https://jobs.example.com/done", "GET")]


async def test_not_modified_is_served_as_is():
    resp = _Resp(304)
    route = await _run(_Route("https://cdn.example.com/a.css", {"https://cdn.example.com/a.css": resp}))
    assert route.fulfilled is resp


async def test_fetch_failure_aborts():
    class _Broken(_Route):
        async def fetch(self, **kw):
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")

    route = await _run(_Broken("https://cdn.example.com/a.js", {}))
    assert route.aborted == "failed"
