"""Job description → JobExtraction: one LLM call per job, with a rule-based fallback (spec §3.2)."""
import asyncio
import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Literal, Optional

from app.job_apply.schema import (
    Clearance,
    Degree,
    JobExtraction,
    Location,
    MinYears,
    Term,
    WorkAuth,
)
from app.job_apply.gates import normalize_country, region_key
from app.job_apply.sections import _heading_kind, line_kind, split_sections
from app.job_apply.skills_dict import SKILLS, SOFT_SKILLS, canonical, find_skills, is_soft
from app.job_apply.text import SENIORITY_LEVEL, normalize, seniority_word

log = logging.getLogger(__name__)

PROMPT_VERSION = "job-extract-v7"
# A model pick shorter than this share of the rules filter's text has dropped requirements; use the rules instead.
TAILOR_MIN_SHARE = 0.2
MAX_JD_CHARS = 30_000
MAX_REQUIRED = 15
MAX_TERMS = 40

_TIERS = ("required", "core", "mentioned", "preferred", "context")
_TIER_RANK = {t: i for i, t in enumerate(_TIERS)}  # lower = stronger
_WORK_MODES = ("remote", "hybrid", "onsite", "unknown")
_SENIORITY_ALIASES = {"lead": "staff", "head": "director", "vp": "director", "associate": "junior", "entry": "junior"}

_PROMPT = """You extract structured screening data from ONE job posting. Reply with a single JSON object only,
no prose, no markdown fences. Today's date is {run_date}.

JSON shape (use null when the posting doesn't say):
{{
  "title": "job title as posted",
  "company": "company name",
  "seniority": "intern|junior|mid|senior|staff|principal|director|null",
  "terms": [{{"term": "PostgreSQL", "aliases": ["Postgres"], "tier": "required|core|mentioned|preferred|context", "soft": false}}],
  "minYears": {{"value": 5, "required": true}},
  "degree": {{"level": 2, "required": true, "equivalentExperience": false}},
  "workMode": "remote|hybrid|onsite|unknown",
  "allowedLocations": [{{"kind": "country|region", "value": "US"}}],
  "remoteAreas": ["Austin Metro", "North Carolina"],
  "workAuthorization": {{"restricted": true, "countries": ["US"], "note": "US citizens only"}},
  "timezoneNote": "4h overlap with EST",
  "clearance": {{"level": "required|preferred|none", "note": "Active Secret"}},
  "postedDate": "YYYY-MM-DD",
  "isJobPosting": true,
  "tailorLines": [1, 2, 3]
}}

Rules:
- tailorLines: the [n] numbers of the posting lines a resume writer needs: the job title, section headings above
  kept lines, requirements, responsibilities, skills, tools, technologies, domain and product work, and preferred
  technical items. Leave out company story and mission, benefits, salary, perks, legal / EEO text, how to apply,
  location, work mode (remote / hybrid / on-site, days in office), time zone, travel, security clearance, citizenship,
  visa or work authorization, and posted / closing dates. Lines are copied as written, so pick whole lines.
- terms: skills, tools, technologies, methods or domains a resume screen would look for. Not generic words.
  At most {max_required} "required" terms and {max_terms} terms in total. Give common aliases/abbreviations.
- tier comes from the section: Requirements/Qualifications/About you = required; Responsibilities/What you'll do = core;
  text with no heading = mentioned; Preferred/Nice to have/Bonus = preferred; About the company = context.
  A line saying "preferred", "a plus", "bonus", "ideally" is preferred. A line saying "required", "must",
  "minimum", "at least" or "N years" is required, even inside Responsibilities. Ignore benefits and legal text.
- soft: true for soft skills (communication, leadership, teamwork...).
- minYears: the minimum years of experience asked. A required figure beats a preferred one.
- degree.level: 1 associate/diploma, 2 bachelor, 3 master, 4 doctorate; the LOWEST degree accepted.
- isJobPosting: false when the text is not one specific job: an error or "not found" page, a cookie or consent
  notice, a list of many openings, a careers landing page, or site navigation only. Then the other fields may be empty.
- workMode: "remote with occasional travel or offsites" is remote. A role with a remote option ("work from our hub
  or remotely within Europe", "onsite, hybrid or remote") is remote. "Remote but must live within commuting distance
  of an office" or "N days in office" is hybrid. In-office/on-site only is onsite.
  If the posting doesn't say where the work is done, workMode is "unknown": a "Location:" line or a city alone
  does not make a job onsite.
- remoteAreas: when the posting limits where a remote hire may live to particular cities, metro areas, states or
  provinces ("Remote - Austin Metro", "must reside in the Seattle or Los Angeles metro areas", "remote from Green Bay,
  Madison, Milwaukee or Chicago only"), list those areas as written; allowedLocations still gets their country.
  Empty when a whole country or more is open.
- allowedLocations: where a remote hire may live. Take the first of these that the posting has:
  1. Text on where hires may live or be based ("must be based in the US or EU", "can be done globally", "open to
     candidates in the EMEA time zone"). List every country and region it allows. "Global", "globally", "anywhere"
     and "international" are Worldwide. A time-zone window is the region it covers ("GMT +/-3" or "CET +/-2" is EMEA).
  2. Otherwise the "Location:" line or the title ("Remote - UK" is GB, "Location: Bulgaria" is BG).
  Never use where the company or its offices are. Keep a country a country; don't widen it into a region.
  Examples: "Remote – USA, Canada, Europe or Australia" -> US, CA, Europe, AU. "Open to North America & Western EU"
  -> North America, Europe. "Europe and the Americas only" -> Europe, Americas. "Remote Global" or "can be done
  globally" -> Worldwide. "Location: Europe" with nothing else -> Europe (not Worldwide). "Within UTC to UTC+8"
  -> EMEA, APAC. "Location: Remote" with nothing else -> empty list.
  Regions: Worldwide, Americas, North America, South America, Latam, EMEA, Europe, European Union, EU, Africa, APAC,
  Asia. Keep "South America" and "European Union" as written (not Latam / Europe); "America" or "the Americas" is
  Americas. Countries as ISO-3166 alpha-2 codes. Empty list only when the posting gives no location at all.
- workAuthorization.restricted: true for "citizens only", "must be authorized to work in X without sponsorship",
  "no visa sponsorship"; countries are ISO-2 codes.
- clearance.level "required" covers "must hold" AND "must be able to obtain" a security clearance.
- postedDate: when the job was posted or published; resolve relative phrases ("Posted 2 weeks ago") against today's
  date. Never an archived, closing, expiry, deadline or updated date, nor a date from company news (founding,
  funding, launches), and never today's date unless the posting says it was posted today. null if not stated.

Job posting:
<<<
{jd}
>>>"""


def jd_lines(jd: str) -> list[str]:
    """The posting's non-empty lines, as numbered in the prompt ([1] is the first)."""
    return [line.strip() for line in (jd or "")[:MAX_JD_CHARS].splitlines() if line.strip()]


def build_prompt(jd: str, run_date: date) -> str:
    numbered = "\n".join(f"[{i}] {line}" for i, line in enumerate(jd_lines(jd), 1))
    return _PROMPT.format(run_date=run_date.isoformat(), max_required=MAX_REQUIRED, max_terms=MAX_TERMS, jd=numbered)


def jd_hash(jd: str) -> str:
    return hashlib.sha256(normalize(jd).encode("utf-8")).hexdigest()


# ---------- LLM output parsing ----------


def _str(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _str_list(v: Any) -> list[str]:
    """LLMs sometimes return a bare string where a list was asked for."""
    items = [v] if isinstance(v, str) else v if isinstance(v, list) else []
    return [x.strip() for x in items if isinstance(x, str) and x.strip()]


def _opt_str(v: Any) -> Optional[str]:
    return _str(v) or None


def _iso_date(v: Any) -> Optional[date]:
    try:
        return date.fromisoformat(_str(v)[:10])
    except ValueError:
        return None


def _seniority(v: Any) -> Optional[str]:
    s = _str(v).lower()
    s = _SENIORITY_ALIASES.get(s, s)
    return s if s in SENIORITY_LEVEL else None


def _terms(items: Any) -> list[Term]:
    required, others, seen = [], [], set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        name = _str(it.get("term"))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        tier = _str(it.get("tier")).lower()
        aliases = _str_list(it.get("aliases"))
        term = Term(
            term=name,
            aliases=aliases,
            tier=tier if tier in _TIERS else "mentioned",
            soft=bool(it.get("soft")) or is_soft(name),
        )
        (required if term.tier == "required" else others).append(term)
    return (required[:MAX_REQUIRED] + others)[:MAX_TERMS]


def _picked_lines(picks: Any, jd: str) -> Optional[str]:
    """The posting lines the model numbered in `tailorLines`, verbatim and in posting order."""
    lines = jd_lines(jd)
    wanted = set()
    for p in picks if isinstance(picks, list) else []:
        n = p if isinstance(p, int) and not isinstance(p, bool) else int(p) if isinstance(p, str) and p.strip().isdigit() else 0
        if 1 <= n <= len(lines):
            wanted.add(n)
    return "\n".join(lines[n - 1] for n in sorted(wanted)) or None


def parse_extraction(raw: str, jd: Optional[str] = None) -> JobExtraction:
    """`jd`: the posting the prompt numbered, to turn `tailorLines` back into text."""
    from app.llm_router import _try_parse_jsonish

    obj = _try_parse_jsonish(raw or "")
    if not isinstance(obj, dict):
        raise ValueError("job extraction is not a JSON object")
    # A page that isn't a job (error page, job list) legitimately has no title or terms.
    if not _str(obj.get("title")) and not obj.get("terms") and obj.get("isJobPosting") is not False:
        raise ValueError("job extraction has neither a title nor terms")

    years = obj.get("minYears")
    min_years = None
    if isinstance(years, dict) and isinstance(years.get("value"), (int, float)) and years["value"] > 0:
        min_years = MinYears(value=float(years["value"]), required=years.get("required") is not False)

    deg = obj.get("degree")
    degree = None
    if isinstance(deg, dict) and isinstance(deg.get("level"), int) and 1 <= deg["level"] <= 4:
        degree = Degree(
            level=deg["level"],
            required=deg.get("required") is not False,
            equivalentExperience=bool(deg.get("equivalentExperience")),
        )

    locations = []
    for loc in obj.get("allowedLocations") or []:
        if isinstance(loc, dict) and _str(loc.get("kind")).lower() in ("country", "region") and _str(loc.get("value")):
            kind = _str(loc["kind"]).lower()
            value = _str(loc["value"])
            if kind == "country":
                code = normalize_country(value)
                if code:
                    value = code
                elif region_key(value):
                    kind = "region"  # e.g. "Europe" given as a country
            locations.append(Location(kind=kind, value=value))

    wa = obj.get("workAuthorization")
    work_auth = None
    if isinstance(wa, dict):
        work_auth = WorkAuth(
            restricted=bool(wa.get("restricted")),
            countries=[c for c in (normalize_country(x) for x in _str_list(wa.get("countries"))) if c],
            note=_opt_str(wa.get("note")),
        )

    cl = obj.get("clearance")
    level = _str(cl.get("level")).lower() if isinstance(cl, dict) else ""
    clearance = Clearance(
        level=level if level in ("required", "preferred") else "none",
        note=_opt_str(cl.get("note")) if isinstance(cl, dict) else None,
    )

    mode = _str(obj.get("workMode")).lower().replace("-", "").replace(" ", "")
    mode = {"onsite": "onsite", "inoffice": "onsite", "office": "onsite"}.get(mode, mode)

    return JobExtraction(
        title=_str(obj.get("title")),
        company=_str(obj.get("company")),
        seniority=_seniority(obj.get("seniority")),
        terms=_terms(obj.get("terms")),
        minYears=min_years,
        degree=degree,
        workMode=mode if mode in _WORK_MODES else "unknown",
        allowedLocations=locations,
        remoteAreas=_str_list(obj.get("remoteAreas")),
        workAuthorization=work_auth,
        timezoneNote=_opt_str(obj.get("timezoneNote")),
        clearance=clearance,
        postedDate=_iso_date(obj.get("postedDate")),
        isJobPosting=obj.get("isJobPosting") is not False,
        tailorJd=_picked_lines(obj.get("tailorLines"), jd) if jd else None,
    )


# ---------- rule-based fallback ----------

_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)\b", re.I)
_DEGREE_WORDS = [
    (4, re.compile(r"\b(?:ph\.?\s?d|doctorate|doctoral)\b", re.I)),
    (3, re.compile(r"\bmaster(?:'?s)?\b|(?<![A-Za-z])(?:MS|MSc|MBA)(?![A-Za-z])")),
    (2, re.compile(r"\bbachelor(?:'?s)?\b|\bundergraduate\b|(?<![A-Za-z])(?:BS|BA|BSc|B\.S\.|B\.A\.)(?![A-Za-z])", re.I)),
    (1, re.compile(r"\bassociate(?:'?s)? degree\b|\bdiploma\b", re.I)),
]
_EQUIVALENT = re.compile(r"or equivalent|equivalent (?:practical |work |professional )?experience", re.I)
_CLEARANCE = re.compile(
    r"security clearance|\bclearance\b|TS/SCI|top secret|\bsecret clearance\b|public trust|polygraph", re.I
)
_PREF_WORDS = re.compile(r"preferred|a plus|bonus|ideally|nice to have", re.I)
_NOT_REMOTE = re.compile(r"\bnot (?:a )?remote\b|\bnon-remote\b|\bno remote\b", re.I)
_HYBRID = re.compile(
    r"\bhybrid\b|\d+\s*(?:days?|x)\s*(?:a|per|/)?\s*week\s*(?:in|at)\s*(?:the\s*|our\s*)?office"
    r"|\d+\s*days?\s*(?:in|at)\s*(?:the\s*|our\s*)?office|commut(?:e|ing|able) distance",
    re.I,
)
_ONSITE = re.compile(r"\bon[- ]?site\b|\bin[- ]office\b|\bin[- ]person\b|\boffice[- ]based\b", re.I)
_REMOTE = re.compile(r"\bremote\b|work from (?:home|anywhere)|\bWFH\b|fully distributed|distributed team", re.I)
# "in our London office", "from the office": where the work happens, not an office stipend
_OFFICE = re.compile(r"\b(?:in|at|from|to) (?:our|the|one of our) (?:[\w&.,'-]+ ){0,4}offices?\b", re.I)
_REL_POSTED = re.compile(r"(\d+)\+?\s*(day|week|month)s?\s+ago", re.I)
# "Archived 1 month ago" / "Updated 2 days ago" are not when the job was posted.
_NOT_POSTED = re.compile(r"(?:archived|closed|expired|updated|deadline)\W*(?:\w+\W+){0,2}$", re.I)
_POSTED_TODAY = re.compile(r"\bposted (?:today|just now)\b|\bjust posted\b", re.I)
_POSTED_YESTERDAY = re.compile(r"\bposted yesterday\b", re.I)
_POSTED_ISO = re.compile(r"(?:posted|published|date posted)[^\n\d]{0,25}(\d{4}-\d{2}-\d{2})", re.I)
_POSTED_TEXT = re.compile(
    r"(?:posted|published|date posted)(?:\s+on)?[:\s]+([A-Z][a-z]{2,8}\.? \d{1,2},? \d{4}|\d{1,2} [A-Z][a-z]{2,8}\.? \d{4})",
    re.I,
)
_LOCATION_RULES: list[tuple[re.Pattern, Location]] = [
    (re.compile(r"\b(?:work|remote|hire) (?:from )?anywhere\b|\bworldwide\b|\bglobal(?:ly)? remote\b", re.I), Location(kind="region", value="Worldwide")),
    (re.compile(r"\b(?:US|USA|U\.S\.|United States)(?:[- ]based)? only\b|\bremote[- ,(]+(?:US|USA|U\.S\.|United States)\b|\b(?:within|in|from) the (?:US|U\.S\.|United States)\b"), Location(kind="country", value="US")),
    (re.compile(r"\bCanada only\b|\bremote[- ,(]+Canada\b|\b(?:within|in|from) Canada\b"), Location(kind="country", value="CA")),
    (re.compile(r"\bLATAM\b|\bLatin America\b", re.I), Location(kind="region", value="Latam")),
    (re.compile(r"\bSouth America\b", re.I), Location(kind="region", value="South America")),
    (re.compile(r"\bEMEA\b"), Location(kind="region", value="EMEA")),
    (re.compile(r"\b(?:within|in|from) the European Union\b|\bEuropean Union only\b"), Location(kind="region", value="European Union")),
    (re.compile(r"\bremote[- ,(]+Europe\b|\b(?:within|in|from) Europe\b|\bEurope only\b|\bEU only\b"), Location(kind="region", value="Europe")),
    (re.compile(r"\bAPAC\b"), Location(kind="region", value="APAC")),
    (re.compile(r"\bNorth America\b"), Location(kind="region", value="North America")),
    (re.compile(r"\bAmericas\b"), Location(kind="region", value="Americas")),
]
_WORK_AUTH = re.compile(
    r"(?:US|U\.S\.) citizens? only|must be (?:a )?(?:US|U\.S\.) citizen|citizenship (?:is )?required"
    r"|authorized to work in the (?:US|U\.S\.|United States)(?: without (?:visa )?sponsorship)?"
    r"|(?:no|not able to provide|cannot provide|unable to (?:offer|provide)) (?:visa )?sponsorship",
    re.I,
)
_US_MENTION = re.compile(r"\b(?:US|U\.S\.|United States)\b")
_TIMEZONE = re.compile(r"[^.\n]{0,60}\b(?:EST|EDT|PST|PDT|CET|CEST|UTC|GMT|ET|PT|CT|MT)\b[^.\n]{0,40}")
_CAP_WORD = re.compile(r"(?<![\w.])([A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)*(?:\.[a-z]+)?)(?![\w])")
_AUTO_SKIP = frozenset(
    w.lower()
    for w in (
        "remote hybrid onsite united states kingdom america americas europe canada latam english spanish portuguese "
        "german french monday tuesday wednesday thursday friday saturday sunday january february march april may "
        "june july august september october november december inc llc ltd corp team company role job you we our "
        "they this that the and for with will our your their benefits apply equal opportunity north south east west "
        "senior junior lead staff principal engineer engineering developer manager the a an in on at as is are"
    ).split()
)
_MONTHS_FMT = ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%b. %d, %Y", "%d %B %Y", "%d %b %Y")


def _strongest(a: str, b: str) -> str:
    return a if _TIER_RANK[a] <= _TIER_RANK[b] else b


def _posted_date(text: str, run_date: date) -> Optional[date]:
    if _POSTED_TODAY.search(text):
        return run_date
    if _POSTED_YESTERDAY.search(text):
        return run_date - timedelta(days=1)
    if m := _POSTED_ISO.search(text):
        d = _iso_date(m.group(1))
        if d and d <= run_date:
            return d
    if m := _POSTED_TEXT.search(text):
        raw = re.sub(r"\bSept\b", "Sep", re.sub(r"\s+", " ", m.group(1)), flags=re.I)
        for fmt in _MONTHS_FMT:
            try:
                d = datetime.strptime(raw, fmt).date()
                if d <= run_date:
                    return d
            except ValueError:
                continue
    for m in _REL_POSTED.finditer(text):
        if _NOT_POSTED.search(text[max(0, m.start() - 30):m.start()]):
            continue
        n, unit = int(m.group(1)), m.group(2).lower()
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return run_date - timedelta(days=days)
    return None


def _stated_mode(mode: str, text: str) -> str:
    """On-site or hybrid only when the posting says so: a model guessing from a bare "Location: USA" would reject a
    remote job, so an unsupported guess becomes unknown ("Needs a check")."""
    office = bool(_OFFICE.search(text) or _ONSITE.search(text))
    if mode == "onsite" and not (office or _NOT_REMOTE.search(text) or _HYBRID.search(text)):
        return "unknown"
    if mode == "hybrid" and not (office or _HYBRID.search(text)):
        return "unknown"
    return mode


def _work_mode(text: str) -> str:
    if _NOT_REMOTE.search(text):
        return "onsite"
    if _HYBRID.search(text):
        return "hybrid"
    if _REMOTE.search(text):
        return "remote"
    if _ONSITE.search(text):
        return "onsite"
    return "unknown"


def _auto_terms(line: str) -> list[str]:
    out = []
    for m in _CAP_WORD.finditer(line):
        word = m.group(1)
        before = line[: m.start()].rstrip()
        if not before or before[-1] in ".!?:;•-*(\"'":
            continue  # sentence start: capitalised for grammar, not because it's a name
        if len(word) < 3 or word.lower() in _AUTO_SKIP or canonical(word):
            continue
        out.append(word)
    return out


# Lines about where, when and who may work, not what the work is. Narrower than the work-mode rules above so
# "hybrid cloud", "remote sensing" or "distributed teams" stay in.
_TAILOR_DROP = (
    _CLEARANCE, _WORK_AUTH, _TIMEZONE, _NOT_REMOTE, _ONSITE, _OFFICE,
    re.compile(r"\bhybrid\b(?!\s+(?:cloud|apps?|mobile|search|models?|architectures?|infrastructure|environments?|workloads?))", re.I),
    re.compile(r"\d+\s*(?:days?|x)\s*(?:a|per|/)?\s*(?:week\s*)?(?:in|at)\s*(?:the\s*|our\s*)?office|commut(?:e|ing|able) distance", re.I),
    re.compile(r"\bremote\b(?!\s+(?:sensing|control|monitoring|access|debugging|procedure))|work from (?:home|anywhere)|\bWFH\b", re.I),
    re.compile(r"\b(?:posted|published|date posted|closing date|apply by)\b|\b\d+\+?\s*(?:day|week|month)s?\s+ago\b", re.I),
    re.compile(r"^(?:location|company|workplace(?: type)?|work location|job location|salary|compensation|pay range)\s*:", re.I),
    *(rx for rx, _ in _LOCATION_RULES),
)
_TAILOR_KINDS = frozenset({"required", "core", "mentioned", "preferred"})


def tailor_text(jd: str) -> Optional[str]:
    """The posting's title and technical / product lines, verbatim, with their headings: what a resume writer needs.
    Drops company story, benefits, legal text and logistics (location, work mode, time zone, clearance, work
    authorization, posted dates). None when nothing is left."""
    jd = (jd or "")[:MAX_JD_CHARS]
    out: list[str] = []
    for section in split_sections(jd):
        if section.kind not in _TAILOR_KINDS:
            continue
        kept = [line for line in section.lines if not any(rx.search(line) for rx in _TAILOR_DROP)]
        if kept:
            out.extend([section.heading, *kept] if section.heading else kept)
    title = next((line.strip() for line in jd.splitlines() if line.strip()), "")
    if title and title not in out[:1] and _heading_kind(title) is None:
        out.insert(0, title)
    return "\n".join(out) or None


def rules_extract(jd: str, run_date: date) -> JobExtraction:
    jd = jd or ""
    sections = split_sections(jd)
    terms: dict[str, Term] = {}
    years_req: list[float] = []
    years_pref: list[float] = []
    degree: Optional[Degree] = None
    clearance = Clearance()
    auto_count = 0

    first_line = next((l.strip() for l in jd.splitlines() if l.strip()), "")
    title = first_line if len(first_line) <= 80 and not first_line.endswith(".") else ""

    for section in sections:
        if section.kind == "ignored":
            continue
        for line in section.lines:
            kind = line_kind(line, section.kind)
            for canon in find_skills(line):
                prev = terms.get(canon.lower())
                tier = _strongest(kind, prev.tier) if prev else kind
                terms[canon.lower()] = Term(term=canon, aliases=list(SKILLS[canon]), tier=tier, soft=False)
            low = line.lower()
            for soft in SOFT_SKILLS:
                if re.search(rf"\b{re.escape(soft)}\b", low) and soft not in terms:
                    terms[soft] = Term(term=soft, tier=kind, soft=True)
            if section.kind != "context" and auto_count < 10:
                for word in _auto_terms(line):
                    if word.lower() not in terms:
                        terms[word.lower()] = Term(term=word, tier=kind, auto=True)
                        auto_count += 1
            if "experience" in low or "years" in low:
                for m in _YEARS.finditer(line):
                    (years_pref if kind == "preferred" else years_req).append(float(m.group(1)))
            if degree is None:
                levels = [lvl for lvl, rx in _DEGREE_WORDS if rx.search(line)]
                if levels and re.search(r"degree|bachelor|master|ph\.?d|doctor|diploma|\bBS\b|\bMS\b|\bBA\b", line, re.I):
                    degree = Degree(
                        level=min(levels),
                        required=kind != "preferred",
                        equivalentExperience=bool(_EQUIVALENT.search(line)),
                    )
            if _CLEARANCE.search(line):
                level = "preferred" if kind == "preferred" or _PREF_WORDS.search(line) else "required"
                if clearance.level != "required":
                    clearance = Clearance(level=level, note=line[:120])

    min_years = None
    if years_req:
        min_years = MinYears(value=max(years_req), required=True)
    elif years_pref:
        min_years = MinYears(value=max(years_pref), required=False)

    locations = [loc for rx, loc in _LOCATION_RULES if rx.search(jd)]
    work_auth = None
    if _WORK_AUTH.search(jd):
        work_auth = WorkAuth(restricted=True, countries=["US"] if _US_MENTION.search(jd) else [], note=_WORK_AUTH.search(jd).group(0))
    tz = _TIMEZONE.search(jd)

    return JobExtraction(
        title=title,
        seniority=seniority_word(title),
        terms=list(terms.values())[:MAX_TERMS],
        minYears=min_years,
        degree=degree,
        workMode=_work_mode(jd),
        allowedLocations=locations,
        workAuthorization=work_auth,
        timezoneNote=tz.group(0).strip() if tz else None,
        clearance=clearance,
        postedDate=_posted_date(jd, run_date),
        tailorJd=tailor_text(jd),
    )


# ---------- entry point ----------


def _validate(raw: str) -> None:
    parse_extraction(raw)


async def extract_job(
    jd: str, *, user, run_date: date, llm=None
) -> tuple[JobExtraction, Literal["llm", "rules"]]:
    """LLM extraction (OpenAI via llm_router); rule-based fallback on any failure."""
    if llm is None:
        from app.llm_router import complete_with_fallback as llm
    try:
        res = await llm(build_prompt(jd, run_date), user, validate=_validate, purpose="job_extract")
        ex = parse_extraction(res.text, jd=jd)
        rules_jd = tailor_text(jd)
        if not ex.tailorJd or (rules_jd and len(ex.tailorJd) < TAILOR_MIN_SHARE * len(rules_jd)):
            ex.tailorJd = rules_jd  # missing or implausibly short pick: the rules keep every technical line
        posted = ex.postedDate
        if posted and (posted > run_date or (posted == run_date and _posted_date(jd, run_date) != run_date)):
            ex.postedDate = None  # models answer "today" (from the prompt) when the posting gives no date
        if ex.postedDate is None:
            ex.postedDate = _posted_date(jd, run_date)
        ex.workMode = _stated_mode(ex.workMode, jd)
        return ex, "llm"
    except Exception as exc:  # noqa: BLE001 — any LLM failure falls back to rules
        log.warning("job extraction via LLM failed, using rules: %s", exc)
        return await asyncio.to_thread(rules_extract, jd, run_date), "rules"
