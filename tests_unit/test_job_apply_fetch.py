"""Unit tests for fetching postings: URL routing, ATS APIs, JSON-LD and HTML fallback (httpx.MockTransport)."""
import json
from datetime import date

import httpx
import pytest

from app.job_apply.fetch import FetchError, fetch_job, html_to_text, parse_jsonld, route

LONG = "<p>" + "You will build distributed systems in Python and Go. " * 12 + "</p>"


def client(routes: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for prefix, resp in routes.items():
            if url.startswith(prefix):
                status, body = resp
                if isinstance(body, (dict, list)):
                    return httpx.Response(status, json=body)
                return httpx.Response(status, text=body, headers={"content-type": "text/html"})
        return httpx.Response(404, text="nope")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_route_kinds():
    assert route("https://boards.greenhouse.io/acme/jobs/123") == ("greenhouse", {"board": "acme", "id": "123"})
    assert route("https://job-boards.greenhouse.io/acme/jobs/123?gh_jid=123")[0] == "greenhouse"
    assert route("https://jobs.lever.co/acme/1f2e-33/apply") == ("lever", {"company": "acme", "id": "1f2e-33"})
    assert route("https://jobs.ashbyhq.com/acme/9a8b-77") == ("ashby", {"org": "acme", "id": "9a8b-77"})
    assert route("https://apply.workable.com/acme/j/AB12CD/") == ("workable", {"account": "acme", "shortcode": "AB12CD"})
    assert route("https://jobs.smartrecruiters.com/Acme/7445-senior-engineer") == ("smartrecruiters", {"company": "Acme", "id": "7445"})
    kind, p = route("https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Remote-USA/Senior-Engineer_R123")
    assert kind == "workday" and p == {"host": "acme.wd5.myworkdayjobs.com", "tenant": "acme", "site": "Careers", "path": "Remote-USA/Senior-Engineer_R123"}
    assert route("https://www.linkedin.com/jobs/view/1")[0] == "unsupported"
    assert route("https://uk.indeed.com/viewjob?jk=1")[0] == "unsupported"
    assert route("https://careers.example.com/jobs/1")[0] == "generic"


async def test_greenhouse_api():
    body = {"title": "Senior Engineer", "content": "&lt;p&gt;" + "Build things in Python. " * 20 + "&lt;/p&gt;",
            "updated_at": "2026-09-20T10:00:00-04:00", "first_published": "2026-09-15T10:00:00-04:00",
            "location": {"name": "Remote - US"}, "company_name": "Acme"}
    async with client({"https://boards-api.greenhouse.io/v1/boards/acme/jobs/123": (200, body)}) as c:
        res = await fetch_job("https://boards.greenhouse.io/acme/jobs/123", client=c)
    assert res.source == "ats_api" and res.title == "Senior Engineer" and res.company == "Acme"
    assert res.postedDate == date(2026, 9, 15) and "Build things in Python" in res.text and "<p>" not in res.text
    assert "Remote - US" in res.text


async def test_lever_api():
    body = {"text": "Backend Engineer", "descriptionPlain": "We build. " * 30, "createdAt": 1758326400000,
            "lists": [{"text": "Requirements", "content": "<li>5+ years Python</li>"}],
            "categories": {"location": "Remote"}, "workplaceType": "remote"}
    async with client({"https://api.lever.co/v0/postings/acme/1f2e-33": (200, body)}) as c:
        res = await fetch_job("https://jobs.lever.co/acme/1f2e-33", client=c)
    assert res.postedDate == date(2025, 9, 20) and "Requirements" in res.text and "5+ years Python" in res.text
    assert "Workplace type: remote" in res.text


async def test_ashby_api():
    body = {"jobs": [{"id": "other", "title": "X"}, {"id": "9a8b-77", "title": "ML Engineer", "descriptionPlain": "Train models. " * 30,
                     "publishedAt": "2026-09-28T00:00:00Z", "isRemote": True, "location": "Brazil"}]}
    async with client({"https://api.ashbyhq.com/posting-api/job-board/acme": (200, body)}) as c:
        res = await fetch_job("https://jobs.ashbyhq.com/acme/9a8b-77", client=c)
    assert res.title == "ML Engineer" and res.postedDate == date(2026, 9, 28) and "Workplace type: remote" in res.text


def test_parse_jsonld_graph_and_list():
    html = ('<script type="application/ld+json">{"@graph":[{"@type":"Organization"},{"@type":"JobPosting","title":"T",'
            '"datePosted":"2026-09-10"}]}</script>')
    assert parse_jsonld(html)["title"] == "T"
    html = '<script type="application/ld+json">[{"@type":["JobPosting"],"title":"L"}]</script>'
    assert parse_jsonld(html)["title"] == "L"
    assert parse_jsonld("<html></html>") is None


async def test_generic_jsonld_date():
    ld = {"@type": "JobPosting", "title": "Data Engineer", "datePosted": "2026-09-10", "description": LONG,
          "hiringOrganization": {"name": "Beta"}, "jobLocationType": "TELECOMMUTE",
          "applicantLocationRequirements": [{"@type": "Country", "name": "USA"}]}
    page = f'<html><script type="application/ld+json">{json.dumps(ld)}</script><body>short</body></html>'
    async with client({"https://careers.beta.io/": (200, page)}) as c:
        res = await fetch_job("https://careers.beta.io/jobs/1", client=c)
    assert res.source == "html" and res.postedDate == date(2026, 9, 10) and res.company == "Beta"
    assert "Workplace type: remote" in res.text and "USA" in res.text and "distributed systems" in res.text


async def test_generic_text_fallback():
    page = f"<html><head><style>.x{{}}</style><script>var a=1</script></head><nav>Menu</nav><body>{LONG}</body><footer>(c)</footer></html>"
    async with client({"https://careers.beta.io/": (200, page)}) as c:
        res = await fetch_job("https://careers.beta.io/jobs/1", client=c)
    assert "distributed systems" in res.text and "Menu" not in res.text and "var a" not in res.text and res.postedDate is None


async def test_browser_used_when_page_is_thin():
    async def browser(url):
        return f"<html><body>{LONG}</body></html>"

    async with client({"https://careers.beta.io/": (200, "<html><body><div id=root></div></body></html>")}) as c:
        res = await fetch_job("https://careers.beta.io/jobs/1", client=c, browser=browser)
    assert res.source == "browser" and "distributed systems" in res.text


async def test_unsupported_host_raises():
    async with client({}) as c:
        with pytest.raises(FetchError, match="unsupported"):
            await fetch_job("https://www.linkedin.com/jobs/view/1", client=c)


async def test_no_text_raises():
    async with client({"https://careers.beta.io/": (200, "<html><body>Hi</body></html>")}) as c:
        with pytest.raises(FetchError, match="no job text"):
            await fetch_job("https://careers.beta.io/jobs/1", client=c)


async def test_ats_failure_falls_back_to_generic():
    page = f"<html><body>{LONG}</body></html>"
    async with client({"https://boards-api.greenhouse.io/": (500, "err"), "https://boards.greenhouse.io/acme/jobs/1": (200, page)}) as c:
        res = await fetch_job("https://boards.greenhouse.io/acme/jobs/1", client=c)
    assert res.source == "html" and "distributed systems" in res.text


def test_html_to_text_keeps_line_structure():
    text = html_to_text("<h2>Requirements</h2><ul><li>Python</li><li>Go</li></ul>")
    assert text.splitlines() == ["Requirements", "- Python", "- Go"]
