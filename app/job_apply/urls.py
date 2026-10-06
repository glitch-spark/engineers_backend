"""Clean a job URL down to the form that opens the same job description (ported from job_url_filter's
normalize.py, with its test table). Every rule is conservative: when a part might identify the job, it stays.
Apply pages map back to the job page; tracking parameters go; known job boards get their canonical form.

Unlike job_url_filter the scheme is kept (some sites are http-only) and only the scheme's own default port is
dropped. Stored URLs (rows, applied history, posting cache) are in this form: bump URL_VERSION when the rules change
so the startup migration re-cleans them (app/db.py).
"""
import re
from functools import lru_cache
from typing import Optional
from urllib.parse import unquote, urlsplit

URL_VERSION = 2  # 1: the old sheet.normalize_url (tracking params, trailing slash); 2: these rules

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_JSESSION_RE = re.compile(r";jsessionid=[^/]*", re.IGNORECASE)
_SLASHES_RE = re.compile(r"/{2,}")
_WORKDAY_LOCALE_RE = re.compile(r"^/[a-z]{2}-[a-z]{2}(?=/)", re.IGNORECASE)
_LINKEDIN_VIEW_RE = re.compile(r"^/(?:comm/)?jobs/view/(?:[^/]*-)?(\d+)/?$")
_INDEED_HOST_RE = re.compile(r"(?:^|\.)indeed\.")
_ICIMS_APPLY_RE = re.compile(r"^(/jobs/\d+/[^/]+)/(?:login|apply|candidate)$", re.IGNORECASE)
_DEFAULT_PORT = {"http": 80, "https": 443}

# Registered domain (or host) -> platform. A host matches a domain when it equals it or is a subdomain of it.
_DOMAINS = {
    "linkedin.com": "linkedin",
    "myworkdayjobs.com": "workday",
    "myworkdaysite.com": "workday",
    "icims.com": "icims",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "job-boards.eu.greenhouse.io": "greenhouse",
    "workable.com": "workable",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "careers.smartrecruiters.com": "smartrecruiters",
    "app.careerpuck.com": "careerpuck",
    "jobs.jobvite.com": "jobvite",
    "bamboohr.com": "bamboohr",
    "ats.rippling.com": "rippling",
    "zohorecruit.com": "zoho",
    "zohorecruit.eu": "zoho",
    "zohorecruit.in": "zoho",
    "zohorecruit.com.au": "zoho",
    "oraclecloud.com": "oracle",
    "join.com": "join",
}

# Platforms whose job pages are fully identified by the path: on a job page the whole query string is unneeded.
_JOB_PAGE = {
    "workday": re.compile(r"/job/"),
    "icims": re.compile(r"^/jobs/\d+/"),
    "lever": re.compile(r"^/[^/]+/[^/]+"),
    "ashby": re.compile(r"^/[^/]+/[^/]+"),
    "greenhouse": re.compile(r"^/[^/]+/jobs/\d+$"),
    "workable": re.compile(r"/j/|^/view/"),
    "smartrecruiters": re.compile(r"^/(?!oneclick-ui/)[^/]+/[^/]+"),
    "careerpuck": re.compile(r"/job/"),
    "jobvite": re.compile(r"/job/"),
    "bamboohr": re.compile(r"^/careers/\d+"),
    "rippling": re.compile(r"/jobs/[^/]+"),
    "zoho": re.compile(r"/jobs/careers/\d+"),
    "oracle": re.compile(r"/CandidateExperience/.*/job/\d+", re.IGNORECASE),
    "join": re.compile(r"^/companies/[^/]+/\d+"),
}

# Trailing path segments that open the application form instead of the job description.
_APPLY_SEGMENTS = frozenset({
    "apply", "application", "apply-now", "applynow", "applymanually", "autofillwithresume", "usemylastapplication",
})

# Query keys (lowercase) that only track where the click came from, on any site.
_TRACKING_KEYS = frozenset({
    "gclid", "gbraid", "wbraid", "dclid", "fbclid", "msclkid", "yclid",
    "mc_cid", "mc_eid", "_hsenc", "_hsmi", "mkt_tok",
    "source", "src", "gh_src", "ref", "referrer", "ref_src", "refid",
    "trk", "trkinfo", "trackingid", "trid",
    "lever-source", "lever-origin", "lever-via",
    "__jvst", "__jvsd",
    "apply", "application",
})


@lru_cache(maxsize=4096)
def platform(host: str) -> Optional[str]:
    """Job board a host belongs to ('greenhouse', 'workday', 'indeed'…), or None."""
    host = (host or "").lower()
    if _INDEED_HOST_RE.search(host):
        return "indeed"
    labels = host.split(".")
    for i in range(len(labels) - 1):
        found = _DOMAINS.get(".".join(labels[i:]))
        if found:
            return found
    return None


def _split_query(query: str) -> list[tuple[str, str, str]]:
    """Split into (lowercase key, decoded value, raw piece); raw pieces keep their encoding."""
    params = []
    for piece in query.split("&"):
        if not piece:
            continue
        key, _, value = piece.partition("=")
        params.append((unquote(key).lower(), unquote(value), piece))
    return params


def _value_in_path(value: str, path: str) -> bool:
    """True when `value` appears in `path` as a whole token (not inside a longer word)."""
    if len(value) < 4:
        return False
    start = path.find(value)
    while start != -1:
        end = start + len(value)
        if (start == 0 or not path[start - 1].isalnum()) and (end == len(path) or not path[end].isalnum()):
            return True
        start = path.find(value, start + 1)
    return False


def normalize_url(url: str) -> str:
    """The shortest form of `url` that opens the same job description. Non-URLs come back unchanged."""
    raw = (url or "").strip()
    if not _SCHEME_RE.match(raw):
        if not raw.lower().startswith("www."):
            return url
        raw = "https://" + raw

    try:
        parts = urlsplit(raw)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return url
    if not host:
        return url

    scheme = parts.scheme.lower()
    host = host.rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, _DEFAULT_PORT.get(scheme)) else f"{host}:{port}"
    site = platform(host)

    path = parts.path
    if ";" in path:
        path = _JSESSION_RE.sub("", path)
    if "//" in path:
        path = _SLASHES_RE.sub("/", path)
    params = _split_query(parts.query) if parts.query else []
    fragment = parts.fragment if parts.fragment[:1] in ("/", "!") else ""

    # --- Platforms with a canonical job URL ---
    if site == "linkedin":
        match = _LINKEDIN_VIEW_RE.match(path)
        job_id = match.group(1) if match else next(
            (v for k, v, _ in params if k == "currentjobid" and v.isdigit()), None
        )
        if job_id:
            return f"https://www.linkedin.com/jobs/view/{job_id}"
    elif site == "indeed":
        jk = next((piece.partition("=")[2] for k, v, piece in params if k in ("jk", "vjk") and v), None)
        if jk:
            return f"https://{netloc}/viewjob?jk={jk}"
    elif site == "greenhouse" and path.rstrip("/") == "/embed/job_app":
        values = {k: v for k, v, _ in params}
        board, token = values.get("for"), values.get("token")
        if board and token and token.isdigit():
            return f"https://job-boards.greenhouse.io/{board}/jobs/{token}"
    elif site == "icims":
        path = _ICIMS_APPLY_RE.sub(r"\1/job", path.rstrip("/"))
    elif site == "workday":
        path = _WORKDAY_LOCALE_RE.sub("", path)

    # --- Apply pages and trailing slash ---
    if len(path) > 1:
        segments = path.rstrip("/").split("/")
        # Keep at least two real segments, so a bare /careers/apply stays as it is.
        while len(segments) > 3 and segments[-1].lower() in _APPLY_SEGMENTS:
            segments.pop()
        path = "/".join(segments)

    # --- Query string ---
    if params:
        job_page = _JOB_PAGE.get(site)
        if job_page and job_page.search(path):
            params = []
        else:
            params = [
                (k, v, piece)
                for k, v, piece in params
                if not k.startswith("utm_")
                and k not in _TRACKING_KEYS
                and not (k.endswith("id") and _value_in_path(v, path))
            ]

    result = f"{scheme}://{netloc}{path}"
    if params:
        result += "?" + "&".join(piece for _, _, piece in params)
    if fragment:
        result += "#" + fragment
    return result


# http(s):// or www. followed by anything up to whitespace, quotes or angle brackets (job_url_filter extract.py).
_FIND_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?'\""
_CLOSERS = {")": "(", "]": "[", "}": "{"}


def _trim(url: str) -> str:
    """Drop trailing punctuation, and closing brackets that have no opener in the URL."""
    while url:
        last = url[-1]
        if last in _TRAILING_PUNCT:
            url = url[:-1]
        elif last in _CLOSERS and url.count(last) > url.count(_CLOSERS[last]):
            url = url[:-1]
        else:
            break
    return url


def find_urls(text: str) -> list[str]:
    """Every URL in free text, in order, without trailing punctuation; "www." links get https://."""
    if not text or ("://" not in text and "w." not in text and "W." not in text):
        return []
    found = []
    for match in _FIND_RE.findall(text):
        url = _trim(match)
        if url:
            found.append(url if _SCHEME_RE.match(url) else "https://" + url)
    return found


def normalize_all(urls: list[str]) -> tuple[list[str], int]:
    """Normalize every URL in order; return the results and how many changed."""
    cache: dict[str, str] = {}
    normalized: list[str] = []
    changed = 0
    for url in urls:
        result = cache.get(url)
        if result is None:
            result = cache[url] = normalize_url(url)
        if result != url:
            changed += 1
        normalized.append(result)
    return normalized, changed
