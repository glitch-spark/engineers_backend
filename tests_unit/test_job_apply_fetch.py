"""Unit tests for fetching postings: URL routing, ATS APIs, JSON-LD and HTML fallback (httpx.MockTransport)."""
import json
from datetime import date

import httpx
import pytest

from app.job_apply import fetch as fetch_mod
from app.job_apply.fetch import FetchError, fetch_job, html_to_text, parse_jsonld, route

@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """No real DNS in unit tests: 'internal.corp' resolves to a private IP, everything else to a public one."""
    async def resolve(host):
        return ["10.0.0.5"] if host == "internal.corp" else ["93.184.216.34"]

    monkeypatch.setattr(fetch_mod, "_resolve", resolve)


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
    assert route("https://www.linkedin.com/jobs/view/1")[0] == "blocked"
    assert route("https://uk.indeed.com/viewjob?jk=1")[0] == "blocked"
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
        with pytest.raises(FetchError, match="blocks automated access"):
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


def test_route_greenhouse_eu_board():
    assert route("https://job-boards.eu.greenhouse.io/appinio/jobs/4949519101") == (
        "greenhouse", {"board": "appinio", "id": "4949519101"},
    )


async def test_ats_job_off_the_board_is_closed():
    # The API answering "gone" is definitive: the job page would only show the company's other openings.
    page = f"<html><body>{LONG}</body></html>"
    async with client({
        "https://api.ashbyhq.com/posting-api/job-board/acme": (200, {"jobs": [{"id": "other"}]}),
        "https://jobs.ashbyhq.com/acme/9a8b-77": (200, page),
    }) as c:
        with pytest.raises(FetchError, match="job closed"):
            await fetch_job("https://jobs.ashbyhq.com/acme/9a8b-77", client=c)


async def test_ats_404_is_closed():
    page = f"<html><body>{LONG}</body></html>"
    async with client({
        "https://api.lever.co/v0/postings/acme/1f2e-33": (404, {"ok": False}),
        "https://jobs.lever.co/acme/1f2e-33": (200, page),
    }) as c:
        with pytest.raises(FetchError, match="job closed"):
            await fetch_job("https://jobs.lever.co/acme/1f2e-33", client=c)


@pytest.mark.parametrize("status", [404, 410])
async def test_gone_page_is_closed_without_trying_the_browser(status):
    async def browser(url):  # renders the "no longer available" page as if it were the job
        return f"<html><body>{LONG}</body></html>"

    async with client({"https://careers.beta.io/": (status, f"<html><body>{LONG}</body></html>")}) as c:
        with pytest.raises(FetchError, match="job closed"):
            await fetch_job("https://careers.beta.io/jobs/1", client=c, browser=browser)


@pytest.mark.parametrize("target", ["/careers", "/", "/acme?error=true"])
async def test_redirect_to_job_list_is_closed(target):
    def handler(request):
        if request.url.path == "/careers/597":
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, text=f"<html><body>{LONG}</body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(FetchError, match="job closed"):
            await fetch_job("https://careers.beta.io/careers/597", client=c)


async def test_redirect_to_reposted_job_is_followed():
    def handler(request):
        if request.url.path == "/companies/acme/111-engineer":
            return httpx.Response(301, headers={"location": "/companies/acme/222-engineer"})
        return httpx.Response(200, text=f"<html><body>{LONG}</body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await fetch_job("https://careers.beta.io/companies/acme/111-engineer", client=c)
    assert "distributed systems" in res.text


@pytest.mark.parametrize("banner", [
    "This job is no longer active",
    "Sorry, this job was removed by the employer.",
    "We're sorry! The vacancy is already closed.",
    "Job Not Found",
    "This position has been filled.",
    "This job is no longer available",
    "Position Closed. Sorry, this position is no longer accepting candidates.",
])
async def test_page_saying_the_job_is_gone_is_closed(banner):
    page = f"<html><head><title>Senior Engineer</title></head><body><div>{banner}</div>{LONG}</body></html>"
    async with client({"https://careers.beta.io/": (200, page)}) as c:
        with pytest.raises(FetchError, match="job closed"):
            await fetch_job("https://careers.beta.io/jobs/1", client=c)


async def test_rendered_page_saying_the_job_is_gone_is_closed():
    async def browser(url):
        return "<html><head><title>Job Not Found</title></head><body><div id=root>Job Not Found</div></body></html>"

    async with client({"https://careers.beta.io/": (200, "<html><body><div id=root></div></body></html>")}) as c:
        with pytest.raises(FetchError, match="job closed"):
            await fetch_job("https://careers.beta.io/jobs/1", client=c, browser=browser)


async def test_past_valid_through_alone_is_not_closed():
    # Built In sets validThrough = posted + 30 days on jobs that are still open.
    ld = {"@type": "JobPosting", "title": "Data Engineer", "datePosted": "2020-01-10", "validThrough": "2020-02-10",
          "description": LONG}
    page = f'<html><script type="application/ld+json">{json.dumps(ld)}</script><body>Log in to apply</body></html>'
    async with client({"https://careers.beta.io/": (200, page)}) as c:
        res = await fetch_job("https://careers.beta.io/jobs/1", client=c)
    assert res.postedDate == date(2020, 1, 10)


async def test_ordinary_wording_is_not_closed():
    text = "<p>You will no longer need to wait for reviews. Our product is available anywhere.</p>"
    page = f"<html><head><title>Senior Engineer</title></head><body>{text}{LONG}</body></html>"
    async with client({"https://careers.beta.io/": (200, page)}) as c:
        res = await fetch_job("https://careers.beta.io/jobs/1", client=c)
    assert "distributed systems" in res.text


def test_fetcher_version_bumped_for_closed_wording():
    assert fetch_mod.FETCHER_VERSION >= 3


def test_html_to_text_keeps_line_structure():
    text = html_to_text("<h2>Requirements</h2><ul><li>Python</li><li>Go</li></ul>")
    assert text.splitlines() == ["Requirements", "- Python", "- Go"]


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/x",
    "https://internal.corp/jobs/1",
    "http://careers.beta.io:8080/jobs/1",
    "ftp://careers.beta.io/jobs/1",
])
async def test_blocks_internal_targets(url):
    async with client({"": (200, f"<html><body>{LONG}</body></html>")}) as c:
        with pytest.raises(FetchError, match="blocked"):
            await fetch_job(url, client=c)


async def test_blocks_redirect_to_private_address():
    def handler(request):
        if request.url.host == "careers.beta.io":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text=f"<html><body>{LONG}</body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(FetchError, match="blocked"):
            await fetch_job("https://careers.beta.io/jobs/1", client=c)


async def test_follows_public_redirects():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text=f"<html><body>{LONG}</body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await fetch_job("https://careers.beta.io/old", client=c)
    assert "distributed systems" in res.text


# ---------- why a page can't be read; retries; per-site limit (from job_url_filter's screen.py) ----------


@pytest.fixture
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(fetch_mod, "RETRY_DELAY", 0)


@pytest.mark.parametrize("url", [
    "https://www.glassdoor.com/job-listing/x-JV_1.htm", "https://www.ziprecruiter.com/c/Acme/Job/x",
    "https://wellfound.com/jobs/1", "https://uk.indeed.com/viewjob?jk=1", "https://www.linkedin.com/jobs/view/1",
])
async def test_sites_that_block_bots_are_never_requested(url):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, text=f"<html><body>{LONG}</body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(FetchError, match="blocks automated access"):
            await fetch_job(url, client=c)
    assert calls == []


async def test_bot_check_page_is_not_read_as_the_job():
    page = f"<html><head><title>Just a moment...</title></head><body>{LONG}</body></html>"

    async def browser(url):
        return page

    async with client({"https://careers.beta.io/": (200, page)}) as c:
        with pytest.raises(FetchError, match="bot protection"):
            await fetch_job("https://careers.beta.io/jobs/1", client=c, browser=browser)


@pytest.mark.parametrize("status, reason", [(403, "blocks automated access"), (503, "blocks automated access"),
                                            (429, "rate-limited"), (500, "HTTP 500")])
async def test_failed_page_says_why(status, reason, no_retry_wait):
    async with client({"https://careers.beta.io/": (status, "<html></html>")}) as c:
        with pytest.raises(FetchError, match=reason):
            await fetch_job("https://careers.beta.io/jobs/1", client=c)


async def test_rate_limit_is_retried_once(no_retry_wait):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, text=f"<html><body>{LONG}</body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await fetch_job("https://careers.beta.io/jobs/1", client=c)
    assert len(calls) == 2 and "distributed systems" in res.text


async def test_timeout_is_retried_once_then_reported(no_retry_wait):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(FetchError, match="timed out"):
            await fetch_job("https://careers.beta.io/jobs/1", client=c)
    assert len(calls) == 2


async def test_at_most_four_requests_per_site_at_once():
    import asyncio

    live, peak = 0, 0

    async def handler(request):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return httpx.Response(200, text=f"<html><body>{LONG}</body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await asyncio.gather(*(fetch_job(f"https://careers.beta.io/jobs/{i}", client=c) for i in range(10)))
    assert peak == 4


# ---------- more job boards (from job_url_filter's screen.py) ----------


def test_route_more_boards():
    assert route("https://jobs.eu.lever.co/acme/1f2e-33") == ("lever", {"company": "acme", "id": "1f2e-33", "eu": True})
    kind, p = route("https://wd3.myworkdaysite.com/en-US/recruiting/acme/Careers/job/Remote/Engineer_R1")
    assert kind == "workday" and p == {"host": "wd3.myworkdaysite.com", "tenant": "acme", "site": "Careers",
                                       "path": "Remote/Engineer_R1"}
    assert route("https://careers.smartrecruiters.com/Acme/7445-engineer") == ("smartrecruiters", {"company": "Acme", "id": "7445"})
    assert route("https://app.careerpuck.com/job-board/acme/job/4004") == ("greenhouse", {"board": "acme", "id": "4004"})
    assert route("https://acme.bamboohr.com/careers/251") == ("bamboohr", {"host": "acme.bamboohr.com", "id": "251"})
    assert route("https://acme.zohorecruit.com/jobs/Careers/6485000/Senior-Dev") == (
        "zoho", {"page": "https://acme.zohorecruit.com/jobs/Careers/6485000/Senior-Dev", "id": "6485000"})


async def test_lever_eu_uses_the_eu_api():
    body = {"text": "Engineer", "descriptionPlain": "Build things in Python. " * 20, "createdAt": 1790000000000,
            "categories": {"location": "Berlin"}}
    async with client({"https://api.eu.lever.co/v0/postings/acme/1f2e-33": (200, body)}) as c:
        res = await fetch_job("https://jobs.eu.lever.co/acme/1f2e-33", client=c)
    assert res.source == "ats_api" and "Berlin" in res.text


async def test_bamboohr_api():
    body = {"result": {"jobOpening": {
        "jobOpeningName": "Support Engineer (EMEA)", "description": LONG, "datePosted": "2026-09-04",
        "atsLocation": {"city": "Berlin", "country": "Germany"}, "locationType": "1"}}}
    async with client({"https://acme.bamboohr.com/careers/251/detail": (200, body)}) as c:
        res = await fetch_job("https://acme.bamboohr.com/careers/251", client=c)
    assert res.source == "ats_api" and res.title == "Support Engineer (EMEA)" and res.postedDate == date(2026, 9, 4)
    assert "Berlin, Germany" in res.text and "Workplace type: remote" in res.text


async def test_bamboohr_gone_is_closed():
    async with client({"https://acme.bamboohr.com/careers/597/detail": (404, {"type": "not_found"})}) as c:
        with pytest.raises(FetchError, match="job closed"):
            await fetch_job("https://acme.bamboohr.com/careers/597", client=c)


async def test_zoho_embedded_jobs():
    jobs = [{"id": "6485000", "Posting_Title": "Senior Dev", "Job_Description": LONG, "City": "Kyiv",
             "Country": "Ukraine", "Remote_Job": True}]
    escaped = json.dumps(json.dumps(jobs))[1:-1].replace("'", "\'")
    page = f"<html><script>var jobs = JSON.parse('{escaped}');</script><body>menu</body></html>"
    async with client({"https://acme.zohorecruit.com/": (200, page)}) as c:
        res = await fetch_job("https://acme.zohorecruit.com/jobs/Careers/6485000/Senior-Dev", client=c)
    assert res.title == "Senior Dev" and "distributed systems" in res.text and "Kyiv" in res.text


async def test_gh_jid_page_with_embedded_greenhouse_board():
    # a company careers page that embeds its Greenhouse board: read the job from Greenhouse, not the job list
    listing = "<html><body>" + "<a>Open role</a>" * 60 + \
        '<script src="https://boards.greenhouse.io/embed/job_board/js?for=nebius"></script></body></html>'
    gh = {"title": "Senior Engineer", "content": "&lt;p&gt;" + "Build things in Python. " * 20 + "&lt;/p&gt;",
          "first_published": "2026-09-15T10:00:00-04:00", "location": {"name": "Remote - Europe"}}
    async with client({"https://careers.nebius.com/": (200, listing),
                       "https://boards-api.greenhouse.io/v1/boards/nebius/jobs/4950073101": (200, gh)}) as c:
        res = await fetch_job("https://careers.nebius.com/?gh_jid=4950073101", client=c)
    assert res.source == "ats_api" and res.title == "Senior Engineer" and "Remote - Europe" in res.text


async def test_workday_hidden_job_says_so():
    async with client({"https://acme.wd3.myworkdayjobs.com/wday/cxs/": (403, {"errorCode": "HTTP_403"}),
                       "https://acme.wd3.myworkdayjobs.com/": (200, "<html><body><div id=root></div></body></html>")}) as c:
        with pytest.raises(FetchError, match="Workday doesn't show this job"):
            await fetch_job("https://acme.wd3.myworkdayjobs.com/Careers/job/Remote/Engineer_R1", client=c)


GH_JOB = {"title": "Senior Engineer", "content": "&lt;p&gt;" + "Build things in Python. " * 20 + "&lt;/p&gt;",
          "first_published": "2026-09-15T10:00:00-04:00", "location": {"name": "Remote - Europe"}}


async def test_gh_jid_board_guessed_from_the_domain():
    # the board is only embedded by script after load: try the board named after the site
    page = "<html><body>" + "<a>Open role</a>" * 60 + "</body></html>"
    async with client({"https://toloka.ai/": (200, page),
                       "https://boards-api.greenhouse.io/v1/boards/toloka/jobs/4948089101": (200, GH_JOB)}) as c:
        res = await fetch_job("https://toloka.ai/careers/info?jid=4948089101&gh_jid=4948089101", client=c)
    assert res.source == "ats_api" and res.title == "Senior Engineer"


async def test_gh_jid_wrong_board_guess_reads_the_page():
    page = f"<html><body>{LONG}</body></html>"
    async with client({"https://careers.acme.io/": (200, page),
                       "https://boards-api.greenhouse.io/v1/boards/acme/jobs/77": (404, {"status": 404})}) as c:
        res = await fetch_job("https://careers.acme.io/jobs?gh_jid=77", client=c)
    assert res.source == "html" and "distributed systems" in res.text


async def test_gh_jid_job_app_embed():
    page = "<html><body>" + "<a>Open role</a>" * 60 + \
        '<iframe src="https://job-boards.eu.greenhouse.io/embed/job_app?for=tolokaai&token=4948089101"></iframe></body></html>'
    async with client({"https://toloka.ai/": (200, page),
                       "https://boards-api.greenhouse.io/v1/boards/tolokaai/jobs/4948089101": (200, GH_JOB)}) as c:
        res = await fetch_job("https://toloka.ai/careers/info?gh_jid=4948089101", client=c)
    assert res.source == "ats_api"


# ---------- structured vs. text-only (job_url_filter's "structured source" rule) ----------


async def test_text_only_page_is_not_structured():
    async with client({"https://careers.beta.io/": (200, f"<html><body>{LONG}</body></html>")}) as c:
        res = await fetch_job("https://careers.beta.io/jobs/1", client=c)
    assert res.source == "html" and res.structured is False


async def test_jsonld_page_and_ats_api_are_structured():
    ld = {"@type": "JobPosting", "title": "Data Engineer", "description": LONG}
    page = f'<html><script type="application/ld+json">{json.dumps(ld)}</script><body>short</body></html>'
    async with client({"https://careers.beta.io/": (200, page),
                       "https://boards-api.greenhouse.io/v1/boards/acme/jobs/9": (200, GH_JOB)}) as c:
        assert (await fetch_job("https://careers.beta.io/jobs/1", client=c)).structured is True
        assert (await fetch_job("https://boards.greenhouse.io/acme/jobs/9", client=c)).structured is True


def test_fetcher_version_bumped_for_structured_flag():
    assert fetch_mod.FETCHER_VERSION >= 4
