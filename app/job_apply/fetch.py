"""Fetch a job posting from its URL (spec §4.2): public ATS JSON APIs first, then the page's JSON-LD
JobPosting, then its visible text, then a headless browser for script-rendered pages."""
import asyncio
import html as html_lib
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Optional
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

# Bump when what counts as a usable page changes: cached pages read by an older fetcher are fetched again.
# 2: closed jobs (404/410, redirect to the job list, gone from the ATS) are no longer read as job text.
# 3: pages that say the job is gone ("no longer active", "job not found", "this job was removed"…).
FETCHER_VERSION = 3
MIN_TEXT = 200  # below this there's no usable job text
THIN_PAGE = 400  # below this a script-rendered page is retried in the browser
TIMEOUT = 20.0
BROWSER_CONCURRENCY = 2
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

# Sites that refuse automated access: never requested (job_url_filter's deny-list, plus LinkedIn).
_BLOCKED = re.compile(
    r"(?:^|\.)(?:linkedin\.com|indeed\.[a-z.]+|glassdoor\.[a-z.]+|ziprecruiter\.[a-z.]+|wellfound\.com|angel\.co)$", re.I
)
# A bot check or captcha served instead of the page (job_url_filter extractors.py).
_BOT_TITLE = re.compile(
    r"just a moment|human verification|attention required|access denied|are you a robot|security check|"
    r"verify you are human|pardon our interruption|captcha",
    re.I,
)
_BOT_MARKERS = ("g-recaptcha", "h-captcha", "cf-chl-", "captcha-delivery.com", "/_incapsula_resource")
_BLOCKING_STATUS = {401, 403, 405, 503, 999}
# A company careers page that embeds its Greenhouse board (job_url_filter extractors.py): with ?gh_jid=N the job
# is board X, id N — read it from Greenhouse instead of the page (which lists every opening).
_GH_EMBED = re.compile(
    r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/embed/(?:job_board(?:/js)?|job_app)\?for=([\w-]+)"
)
_GH_JID = re.compile(r"(?:^|&)gh_jid=(\d+)")
_ZOHO_JOBS = re.compile(r"var\s+jobs\s*=\s*JSON\.parse\('(.*?)'\)\s*;", re.DOTALL)
_JS_ESCAPE = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", re.DOTALL)
_JS_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_WORKDAY_SITE_HOST = re.compile(r"^wd\d+\.myworkdaysite\.com$")

# One retry for a rate limit, timeout or dropped connection; at most HOST_CONCURRENCY requests per site at once.
RETRY_DELAY = 2.0
HOST_CONCURRENCY = 4
_host_slots: dict[tuple[int, str], asyncio.Semaphore] = {}
_BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "br", "dt", "dd", "table"]
_DROP_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "svg", "form", "iframe"]

MAX_REDIRECTS = 5
_ALLOWED_PORTS = {None, 80, 443}

_browser_sem = asyncio.Semaphore(BROWSER_CONCURRENCY)


class FetchError(Exception):
    pass


class JobClosed(FetchError):
    """The posting is gone. Raised instead of reading whatever page the dead link now shows."""


_GONE = (404, 410)


@dataclass
class FetchResult:
    text: str
    source: Literal["ats_api", "html", "browser"]
    postedDate: Optional[date] = None
    title: str = ""
    company: str = ""
    location: str = ""
    closed: Optional[str] = None  # why the page says the job is gone (pages only)
    bot_check: bool = False  # the page is a bot check / captcha, not the site's content


# A page can load fine and still say the posting is gone (Teamtailor, Built In, Recruitee, Zoho, Deel, N-iX…).
# Phrased narrowly: ordinary job text ("you will no longer need…") must not match.
_GONE_TEXT = re.compile(
    r"\b(?:this|the) (?:job|position|role|vacancy|posting|opening)(?: posting)?(?: you(?: are|'re) looking for)? "
    r"(?:is|has been|was) (?:no longer (?:active|available|open)|closed|removed|filled|expired)\b"
    r"|\bno longer accepting (?:new )?(?:applications|candidates|applicants)\b|\bposition closed\b"
    r"|\bvacancy is (?:already )?closed\b|\bjob not found\b"
    r"|\bposition has been filled\b|\bjob (?:is )?no longer available\b",
    re.I,
)
_GONE_SCAN_CHARS = 8000  # banners sit at the top; scanning the whole text only adds false positives


# ---------- URL safety (user-supplied URLs are fetched from our servers) ----------


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    return addr.is_global and not addr.is_multicast


async def check_url(url: str) -> None:
    """Refuse anything but http(s) on the default ports to a public address, so a job sheet can't make
    the server read internal services (localhost, private networks, cloud metadata)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("blocked URL: only http(s) links are fetched")
    try:
        port = parts.port
    except ValueError:
        raise FetchError("blocked URL: bad port")
    if port not in _ALLOWED_PORTS:
        raise FetchError("blocked URL: non-standard port")
    host = parts.hostname
    try:
        ips = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            ips = await _resolve(host)
        except OSError:
            raise FetchError("could not resolve host")
    if not ips or not all(_public_ip(ip) for ip in ips):
        raise FetchError("blocked URL: private or reserved address")


def _host_slot(url: str) -> asyncio.Semaphore:
    key = (id(asyncio.get_running_loop()), (urlsplit(url).hostname or "").lower())
    if key not in _host_slots and len(_host_slots) > 2000:
        _host_slots.clear()  # bounded: one entry per site seen; dropping them only loosens the limit briefly
    return _host_slots.setdefault(key, asyncio.Semaphore(HOST_CONCURRENCY))


async def _send(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """GET with at most HOST_CONCURRENCY in flight per site, retried once after RETRY_DELAY on a rate limit,
    timeout or dropped connection (the slot is free while waiting)."""
    for attempt in (1, 2):
        async with _host_slot(url):
            try:
                resp = await client.get(url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == 2:
                    raise
                resp = None
        if resp is not None and (resp.status_code != 429 or attempt == 2):
            return resp
        await asyncio.sleep(RETRY_DELAY)
    raise AssertionError("unreachable")


async def _get_page(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET with redirects followed by hand, so every hop is checked."""
    for _ in range(MAX_REDIRECTS + 1):
        await check_url(url)
        resp = await _send(client, url, headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=False)
        if resp.is_redirect and resp.headers.get("location"):
            url = urljoin(url, resp.headers["location"])
            continue
        return resp
    raise FetchError("too many redirects")


def _lands_on_job_list(original: str, final: str) -> bool:
    """A closed job's link often redirects to the company's job list: Greenhouse adds ?error=true, others drop the
    job's part of the path (/careers/597 -> /careers, or the site root)."""
    end = urlsplit(final)
    if "error=true" in end.query:
        return True
    start_segs = [s for s in urlsplit(original).path.split("/") if s]
    end_segs = [s for s in end.path.split("/") if s]
    return len(end_segs) < len(start_segs) and start_segs[: len(end_segs)] == end_segs


# ---------- routing ----------


def route(url: str) -> tuple[str, dict]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    if _BLOCKED.search(host):
        return "blocked", {}
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io") and len(segs) >= 3 and segs[1] == "jobs":
        return "greenhouse", {"board": segs[0], "id": segs[2]}
    if host == "app.careerpuck.com" and len(segs) >= 4 and segs[0] == "job-board" and segs[2] == "job":
        return "greenhouse", {"board": segs[1], "id": segs[3]}  # CareerPuck boards are Greenhouse boards
    if host == "jobs.lever.co" and len(segs) >= 2:
        return "lever", {"company": segs[0], "id": segs[1]}
    if host == "jobs.eu.lever.co" and len(segs) >= 2:
        return "lever", {"company": segs[0], "id": segs[1], "eu": True}
    if host == "jobs.ashbyhq.com" and len(segs) >= 2:
        return "ashby", {"org": segs[0], "id": segs[1]}
    if host == "apply.workable.com" and len(segs) >= 3 and segs[1] == "j":
        return "workable", {"account": segs[0], "shortcode": segs[2]}
    if host.endswith(".workable.com") and host != "apply.workable.com" and len(segs) >= 2 and segs[0] == "j":
        return "workable", {"account": host.split(".")[0], "shortcode": segs[1]}
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and len(segs) >= 2 and (m := re.match(r"(\d+)", segs[1])):
        return "smartrecruiters", {"company": segs[0], "id": m.group(1)}
    if host.endswith(".myworkdayjobs.com") and "job" in segs:
        i = segs.index("job")
        site_segs = [s for s in segs[:i] if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
        if site_segs and i + 1 < len(segs):
            return "workday", {
                "host": host, "tenant": host.split(".")[0], "site": site_segs[-1], "path": "/".join(segs[i + 1:]),
            }
    if _WORKDAY_SITE_HOST.match(host):
        segs_nl = [s for s in segs if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
        if len(segs_nl) >= 5 and segs_nl[0] == "recruiting" and segs_nl[3] == "job":
            return "workday", {"host": host, "tenant": segs_nl[1], "site": segs_nl[2], "path": "/".join(segs_nl[4:])}
    if host.endswith(".bamboohr.com") and len(segs) >= 2 and segs[0] == "careers" and segs[1].isdigit():
        return "bamboohr", {"host": host, "id": segs[1]}
    if ".zohorecruit." in host and len(segs) >= 3 and [s.lower() for s in segs[:2]] == ["jobs", "careers"] and segs[2].isdigit():
        return "zoho", {"page": url, "id": segs[2]}
    return "generic", {}


# ---------- HTML helpers ----------


def html_to_text(markup: "str | BeautifulSoup") -> str:
    soup = markup if isinstance(markup, BeautifulSoup) else BeautifulSoup(markup or "", "html.parser")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert(0, "- ")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in soup.get_text().splitlines())
    return "\n".join(line for line in lines if line and line != "-")


def _is_posting(obj: Any) -> bool:
    t = obj.get("@type") if isinstance(obj, dict) else None
    return t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t)


def _find_posting(obj: Any) -> Optional[dict]:
    if _is_posting(obj):
        return obj
    if isinstance(obj, list):
        for item in obj:
            if found := _find_posting(item):
                return found
    if isinstance(obj, dict) and "@graph" in obj:
        return _find_posting(obj["@graph"])
    return None


def parse_jsonld(markup: "str | BeautifulSoup") -> Optional[dict]:
    soup = markup if isinstance(markup, BeautifulSoup) else BeautifulSoup(markup or "", "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if found := _find_posting(data):
            return found
    return None


def _date(value: Any) -> Optional[date]:
    if isinstance(value, (int, float)) and value > 0:  # epoch milliseconds (Lever)
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _compose(body: str, *, title="", company="", location="", remote: Optional[bool] = None, workplace="") -> str:
    head = [title] if title else []
    if company:
        head.append(f"Company: {company}")
    if location:
        head.append(f"Location: {location}")
    if workplace:
        head.append(f"Workplace type: {workplace}")
    elif remote:
        head.append("Workplace type: remote")
    return ("\n".join(head) + "\n\n" + body).strip() if head else body.strip()


def _names(value: Any) -> str:
    items = value if isinstance(value, list) else [value]
    out = []
    for it in items:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            name = it.get("name")
            addr = it.get("address")
            if not name and isinstance(addr, dict):
                name = addr.get("addressLocality") or addr.get("addressCountry")
            if isinstance(name, str) and name:
                out.append(name)
    return ", ".join(out)


# ---------- ATS APIs ----------


async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    resp = await _send(client, url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


async def _greenhouse(client, p) -> FetchResult:
    d = await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{p['board']}/jobs/{p['id']}")
    body = html_to_text(html_lib.unescape(d.get("content") or ""))
    loc = (d.get("location") or {}).get("name", "")
    title, company = d.get("title", ""), d.get("company_name", "")
    return FetchResult(
        text=_compose(body, title=title, company=company, location=loc), source="ats_api",
        postedDate=_date(d.get("first_published")) or _date(d.get("updated_at")), title=title, company=company, location=loc,
    )


async def _lever(client, p) -> FetchResult:
    api = "api.eu.lever.co" if p.get("eu") else "api.lever.co"
    d = await _get_json(client, f"https://{api}/v0/postings/{p['company']}/{p['id']}")
    parts = [d.get("descriptionPlain") or html_to_text(d.get("description") or "")]
    for lst in d.get("lists") or []:
        parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content') or '')}")
    parts.append(d.get("additionalPlain") or "")
    loc = (d.get("categories") or {}).get("location", "")
    title = d.get("text", "")
    return FetchResult(
        text=_compose("\n\n".join(x for x in parts if x), title=title, location=loc, workplace=d.get("workplaceType") or ""),
        source="ats_api", postedDate=_date(d.get("createdAt")), title=title, location=loc,
    )


async def _ashby(client, p) -> FetchResult:
    d = await _get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{p['org']}?includeCompensation=false")
    job = next((j for j in d.get("jobs") or [] if j.get("id") == p["id"]), None)
    if job is None:
        raise JobClosed("job closed (no longer on the Ashby board)")
    body = job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml") or "")
    loc = job.get("location") or ""
    title = job.get("title", "")
    return FetchResult(
        text=_compose(body, title=title, location=loc, remote=job.get("isRemote"), workplace=(job.get("workplaceType") or "").lower()),
        source="ats_api", postedDate=_date(job.get("publishedAt")), title=title, location=loc,
    )


async def _workable(client, p) -> FetchResult:
    d = await _get_json(client, f"https://apply.workable.com/api/v2/accounts/{p['account']}/jobs/{p['shortcode']}")
    body = "\n\n".join(html_to_text(d.get(k) or "") for k in ("description", "requirements", "benefits"))
    loc = ""
    if isinstance(d.get("location"), dict):
        loc = ", ".join(x for x in (d["location"].get("city"), d["location"].get("country")) if x)
    title = d.get("title", "")
    return FetchResult(
        text=_compose(body, title=title, location=loc, remote=d.get("remote")), source="ats_api",
        postedDate=_date(d.get("published")), title=title, location=loc,
    )


async def _smartrecruiters(client, p) -> FetchResult:
    d = await _get_json(client, f"https://api.smartrecruiters.com/v1/companies/{p['company']}/postings/{p['id']}")
    sections = ((d.get("jobAd") or {}).get("sections")) or {}
    body = "\n\n".join(
        f"{s.get('title', '')}\n{html_to_text(s.get('text') or '')}" for s in sections.values() if isinstance(s, dict)
    )
    loc_d = d.get("location") or {}
    loc = ", ".join(x for x in (loc_d.get("city"), loc_d.get("country")) if x)
    title = d.get("name", "")
    company = (d.get("company") or {}).get("name", "")
    return FetchResult(
        text=_compose(body, title=title, company=company, location=loc, remote=loc_d.get("remote")), source="ats_api",
        postedDate=_date(d.get("releasedDate")), title=title, company=company, location=loc,
    )


async def _workday(client, p) -> FetchResult:
    d = await _get_json(client, f"https://{p['host']}/wday/cxs/{p['tenant']}/{p['site']}/job/{p['path']}")
    info = d.get("jobPostingInfo") or {}
    loc = info.get("location") or ""
    title = info.get("title", "")
    return FetchResult(
        text=_compose(html_to_text(info.get("jobDescription") or ""), title=title, location=loc, workplace=(info.get("remoteType") or "").lower()),
        source="ats_api", postedDate=_date(info.get("startDate")), title=title, location=loc,
    )


_BAMBOO_MODE = {"1": "remote", "2": "hybrid"}  # BambooHR locationType ("0" / missing = on-site or unknown)


async def _bamboohr(client, p) -> FetchResult:
    d = await _get_json(client, f"https://{p['host']}/careers/{p['id']}/detail")
    job = (d.get("result") or {}).get("jobOpening") if isinstance(d, dict) else None
    if not isinstance(job, dict):
        raise JobClosed("job closed (no longer on the BambooHR careers page)")
    title = (job.get("jobOpeningName") or "").strip()
    where = job.get("atsLocation") or job.get("location") or {}
    loc = ", ".join(x for x in (where.get("city"), where.get("country") or where.get("addressCountry")) if x)
    return FetchResult(
        text=_compose(html_to_text(job.get("description") or ""), title=title, location=loc,
                      workplace=_BAMBOO_MODE.get(str(job.get("locationType") or ""), "")),
        source="ats_api", postedDate=_date(job.get("datePosted")), title=title, location=loc,
    )


def _js_unescape(text: str) -> str:
    def repl(m: re.Match) -> str:
        esc = m.group(1)
        if esc[0] in "xu" and len(esc) > 1:
            return chr(int(esc[1:], 16))
        return _JS_SIMPLE.get(esc, esc)

    return _JS_ESCAPE.sub(repl, text)


async def _zoho(client, p) -> FetchResult:
    """Zoho Recruit embeds its openings in the careers page as `var jobs = JSON.parse('…')`."""
    resp = await _get_page(client, p["page"])
    resp.raise_for_status()
    m = _ZOHO_JOBS.search(resp.text)
    if not m:
        raise FetchError("no Zoho job data on the page")
    records = json.loads(_js_unescape(m.group(1)), strict=False)
    job = next((r for r in records if isinstance(r, dict) and str(r.get("id")) == p["id"]), None)
    if job is None:
        raise JobClosed("job closed (no longer on the Zoho careers page)")
    title = html_lib.unescape(str(job.get("Posting_Title") or "")).strip()
    loc = ", ".join(str(x) for x in (job.get("City"), job.get("Country")) if x)
    return FetchResult(
        text=_compose(html_to_text(job.get("Job_Description") or ""), title=title, location=loc,
                      remote=bool(job.get("Remote_Job"))),
        source="ats_api", title=title, location=loc,
    )


_ATS = {
    "greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "workable": _workable,
    "smartrecruiters": _smartrecruiters, "workday": _workday, "bamboohr": _bamboohr, "zoho": _zoho,
}


def _domain_label(url: str) -> str:
    """careers.nebius.com -> "nebius", toloka.ai -> "toloka" (the usual Greenhouse board name)."""
    labels = (urlsplit(url).hostname or "").lower().split(".")
    return labels[-2] if len(labels) >= 2 else ""


async def _embedded_greenhouse(client, url: str, markup: str) -> Optional[FetchResult]:
    """`?gh_jid=N` on a page that embeds Greenhouse board X: the job is X/N on Greenhouse. When the page names no
    board (it is embedded by script after load) the board named after the site is tried; a wrong guess just
    falls through to reading the page, it never closes the job."""
    jid = _GH_JID.search(urlsplit(url).query)
    if not jid:
        return None
    named = _GH_EMBED.search(markup or "")
    board = named.group(1) if named else _domain_label(url)
    if not board:
        return None
    try:
        res = await _greenhouse(client, {"board": board, "id": jid.group(1)})
    except httpx.HTTPStatusError as exc:
        if named and exc.response.status_code in _GONE:
            raise JobClosed("job closed (the greenhouse API no longer lists it)") from exc
        return None
    except (httpx.HTTPError, ValueError):
        return None
    return res if len(res.text) >= MIN_TEXT else None


# ---------- generic pages ----------


def _from_page(markup: str, source: Literal["html", "browser"]) -> FetchResult:
    """CPU-bound (HTML parsing): callers run it in a thread. The page is parsed once."""
    soup = BeautifulSoup(markup or "", "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if _BOT_TITLE.search(title) or any(m in (markup or "") for m in _BOT_MARKERS):
        return FetchResult(text="", source=source, bot_check=True)
    ld = parse_jsonld(soup)
    closed = _gone_reason(soup, ld)
    if closed:
        return FetchResult(text="", source=source, closed=closed)
    if ld:
        body = html_to_text(html_lib.unescape(ld.get("description") or ""))
        org = ld.get("hiringOrganization")
        company = org.get("name", "") if isinstance(org, dict) else ""
        remote = str(ld.get("jobLocationType") or "").upper() == "TELECOMMUTE"
        loc = _names(ld.get("applicantLocationRequirements")) or _names(ld.get("jobLocation"))
        if len(body) >= MIN_TEXT:
            title = ld.get("title", "") if isinstance(ld.get("title"), str) else ""
            return FetchResult(
                text=_compose(body, title=title, company=company, location=loc, remote=remote), source=source,
                postedDate=_date(ld.get("datePosted")), title=title, company=company, location=loc,
            )
        posted = _date(ld.get("datePosted"))
    else:
        posted = None
    return FetchResult(text=html_to_text(soup), source=source, postedDate=posted)


def _gone_reason(soup: BeautifulSoup, ld: Optional[dict]) -> Optional[str]:
    """Why a loaded page says the job is gone, or None. Reads the page before html_to_text drops nodes.
    JobPosting.validThrough is not used: boards like Built In fill it in as posted + 30 days on jobs still open."""
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    head = " ".join(soup.get_text(" ", strip=True).split())[:_GONE_SCAN_CHARS]
    m = _GONE_TEXT.search(f"{title} {head}")
    return f"page says: {m.group(0)}" if m else None


def _status_problem(status: int) -> str:
    if status in _BLOCKING_STATUS:
        return f"site blocks automated access (HTTP {status})"
    if status == 429:
        return "rate-limited, try again later"
    return f"the site answered HTTP {status}"


async def browser_html(url: str) -> str:
    """Render a script-heavy page with headless Chromium (at most BROWSER_CONCURRENCY at once)."""
    from playwright.async_api import async_playwright

    await check_url(url)

    async def guard(route) -> None:
        try:
            await check_url(route.request.url)
        except FetchError:
            await route.abort()
            return
        await route.continue_()

    async with _browser_sem:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page(user_agent=UA)
                await page.route("**/*", guard)  # every request the page makes, redirects included
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                return await page.content()
            finally:
                await browser.close()


async def fetch_job(
    url: str,
    *,
    client: httpx.AsyncClient,
    browser: Optional[Callable[[str], Awaitable[str]]] = None,
) -> FetchResult:
    kind, params = route(url)
    if kind == "blocked":
        raise FetchError(f"site blocks automated access ({urlsplit(url).hostname})")
    if kind in _ATS:
        try:
            res = await _ATS[kind](client, params)
            if len(res.text) >= MIN_TEXT:
                return res
        except JobClosed:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in _GONE:
                raise JobClosed(f"job closed (the {kind} API no longer lists it)") from exc
            if kind == "workday" and exc.response.status_code in (401, 403):
                # Workday answers 403 for postings it no longer shows; the page is an empty app shell.
                raise FetchError("Workday doesn't show this job any more (it may be closed)") from exc
        except Exception:  # noqa: BLE001 — any other ATS API problem falls back to the page itself
            pass

    await check_url(url)
    result: Optional[FetchResult] = None
    problem: Optional[str] = None  # why the page couldn't be read, for the "Not fetched" list
    try:
        resp = await _get_page(client, url)
        if resp.status_code in _GONE:
            raise JobClosed(f"job closed (HTTP {resp.status_code})")
        if _lands_on_job_list(url, str(resp.url)):
            raise JobClosed("job closed (the link now opens the job list)")
        if resp.status_code < 400:
            if embedded := await _embedded_greenhouse(client, url, resp.text):
                return embedded
            result = await asyncio.to_thread(_from_page, resp.text, "html")
        else:
            problem = _status_problem(resp.status_code)
    except httpx.TimeoutException:
        problem = "timed out"
    except httpx.HTTPError:
        problem = "couldn't reach the site"
    if result is not None and result.closed:
        raise JobClosed(f"job closed ({result.closed})")
    if result is not None and result.bot_check:
        result, problem = None, "blocked by bot protection"

    if browser is not None and (result is None or len(result.text) < THIN_PAGE):
        rendered: Optional[FetchResult] = None
        markup: Optional[str] = None
        try:
            markup = await browser(url)
        except Exception:  # noqa: BLE001 — browser failure leaves the plain-HTML result
            pass
        if markup is not None:
            if embedded := await _embedded_greenhouse(client, url, markup):
                return embedded
            rendered = await asyncio.to_thread(_from_page, markup, "browser")
        if rendered is not None and rendered.closed:
            raise JobClosed(f"job closed ({rendered.closed})")
        if rendered is not None and rendered.bot_check:
            rendered, problem = None, problem or "blocked by bot protection"
        if rendered is not None and (result is None or len(rendered.text) > len(result.text)):
            result = rendered

    if result is None:
        raise FetchError(problem or "could not load the page")
    if len(result.text) < MIN_TEXT:
        raise FetchError("no job text on the page")
    return result
