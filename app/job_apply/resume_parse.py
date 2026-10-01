"""Resume text → sections, dated roles, degree and stated years.

Resumes arrive in two shapes: markdown/txt with real lines and headings, and PDF text where each page
is one long line ("--- Page N --- JANE DOE ... EXPERIENCE Acme ..."). Headings are therefore found
both at line starts and as ALL-CAPS keywords inside running text.
"""
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

_PAGE_MARKER = re.compile(r"-{3}\s*Page\s+\d+\s*-{3}", re.I)

_GROUPS: dict[str, list[str]] = {
    "experience": [
        "professional experience", "work experience", "relevant experience", "experience", "employment history",
        "employment", "work history", "career history",
    ],
    "skills": [
        "technical skills", "core skills", "key skills", "skills", "technologies", "tech stack", "core competencies",
        "competencies", "tools",
    ],
    "education": ["education", "academic background"],
    "summary": ["professional summary", "summary", "profile", "about me", "objective"],
    "other": ["projects", "certifications", "certificates", "publications", "awards", "languages", "interests", "volunteer"],
}


def _alts(words: list[str]) -> str:
    return "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in sorted(words, key=len, reverse=True))


_LINE_HEADINGS = [
    (g, re.compile(rf"^[ \t]*(?:#{{1,6}}[ \t]*|\*\*|__)?(?:{_alts(ws)})[ \t]*:?[ \t]*(?:\*\*|__)?[ \t]*$", re.I | re.M))
    for g, ws in _GROUPS.items()
]
# Title-Case headings ("Experience", "Technical Skills") inside flat PDF text; filtered by context in _headings.
_TITLE_HEADINGS = [
    (g, re.compile(rf"(?<![A-Za-z])(?:{_alts([' '.join(x.capitalize() for x in w.split()) for w in ws])})(?![A-Za-z])"))
    for g, ws in _GROUPS.items()
]
_CAPS_HEADINGS = [
    (g, re.compile(rf"(?<![A-Za-z])(?:{_alts([w.upper() for w in ws])})(?![A-Za-z])"))
    for g, ws in _GROUPS.items()
]

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12,
}
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DATE = rf"(?:{_MONTH}\s*['’]?\d{{4}}|(?:0?[1-9]|1[0-2])/\d{{4}}|(?:19|20)\d{{2}})"
_PRESENT = r"(?:present|current(?:ly)?|now|today|ongoing|date)"
_RANGE = re.compile(
    rf"(?<![\w/])(?P<start>{_DATE})\s*(?:-|–|—|~|to|until|through)\s*(?P<end>{_DATE}|{_PRESENT})(?![\w/])",
    re.I,
)

_STATED_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?:\s+of)?(?:\s+[\w-]+){0,3}?\s+experience", re.I)
_DEGREE_LONG = [
    (4, re.compile(r"\b(?:ph\.?\s?d|doctorate|doctor of)\b", re.I)),
    (3, re.compile(r"\bmaster(?:'?s)?\s+(?:of|in|degree)\b|\bmaster'?s\b|\bmasters\b", re.I)),
    (2, re.compile(r"\bbachelor(?:'?s)?\b|\bundergraduate degree\b", re.I)),
    (1, re.compile(r"\bassociate(?:'?s)? degree\b|\bdiploma\b", re.I)),
]
_NB = r"(?![A-Za-z])"
_DEGREE_ABBR = [
    (4, re.compile(rf"(?<![A-Za-z])(?:Ph\.?D|D\.?Phil|Ed\.?D){_NB}")),
    (3, re.compile(rf"(?<![A-Za-z])(?:M\.?S\.?c?|MBA|M\.?Eng|M\.?Tech|MCA|M\.?A\.){_NB}")),
    (2, re.compile(rf"(?<![A-Za-z])(?:B\.?S\.?c?|B\.?A\.|BA|B\.?Eng|B\.?Tech|BCA|B\.?E\.|B\.?Com){_NB}")),
    (1, re.compile(rf"(?<![A-Za-z])(?:A\.?A\.?S?|A\.?S\.)" + _NB)),
]

# Phrases with "master" that aren't degrees.
_NOT_DEGREE = re.compile(r"\bscrum\s+master\b|\bmaster\s+data\b|\bmaster\s+(?:class|plan|branch|node|list|key)\b", re.I)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\(?\d[\d\s().-]{8,}\d")


@dataclass
class Role:
    window: str  # text around the date range; holds the title and company
    start: date
    end: Optional[date]  # None = present
    text: str  # what the role says: from its date range to the next role


@dataclass
class ParsedResume:
    text: str
    headline: str
    summary: str = ""
    skills: str = ""
    roles: list[Role] = field(default_factory=list)
    degree_level: Optional[int] = None
    stated_years: Optional[float] = None


@dataclass
class Health:
    score: int
    issues: list[str]


def _parse_date(s: str, *, is_end: bool) -> Optional[date]:
    s = s.strip().lower().rstrip(".")
    if m := re.match(r"(0?[1-9]|1[0-2])/(\d{4})$", s):
        return date(int(m.group(2)), int(m.group(1)), 1)
    if m := re.match(r"((?:19|20)\d{2})$", s):
        return date(int(m.group(1)), 1, 1)
    if m := re.match(r"([a-z]+)\.?\s*['’]?(\d{4})$", s):
        month = _MONTHS.get(m.group(1)[:3])
        if month:
            return date(int(m.group(2)), month, 1)
    return None


def parse_date_ranges(text: str, run_date: date) -> list[tuple[date, Optional[date], int, int]]:
    out = []
    for m in _RANGE.finditer(text or ""):
        start = _parse_date(m.group("start"), is_end=False)
        end_raw = m.group("end")
        end = None if re.fullmatch(_PRESENT, end_raw.strip(), re.I) else _parse_date(end_raw, is_end=True)
        if start is None or start.year < 1970 or start > run_date:
            continue
        if end is not None and (end < start or end.year > run_date.year + 1):
            continue
        out.append((start, end, m.start(), m.end()))
    return out


def _title_heading_ok(text: str, start: int, end: int) -> bool:
    """A Title-Case keyword in running text is a heading unless it reads as prose ("5 years of Experience")."""
    before = text[:start].rstrip()
    prev_word = before.split()[-1] if before.split() else ""
    if before.endswith((",", "&", "/", "-")) or re.fullmatch(r"[a-z]+", prev_word):
        return False
    nxt = text[end:].lstrip(" :")[:1]
    return not nxt.islower()


def _headings(text: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for group, rx in _LINE_HEADINGS + _CAPS_HEADINGS:
        for m in rx.finditer(text):
            found.append((m.start(), m.end(), group))
    if text.count("\n") < 5:  # flat PDF text: no line structure to lean on
        for group, rx in _TITLE_HEADINGS:
            for m in rx.finditer(text):
                if _title_heading_ok(text, m.start(), m.end()):
                    found.append((m.start(), m.end(), group))
    found.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    merged: list[tuple[int, int, str]] = []
    for h in found:
        if merged and h[0] < merged[-1][1]:
            continue  # overlaps a heading already kept
        merged.append(h)
    return merged


def _regions(text: str, heads: list[tuple[int, int, str]]) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    for i, (_, end, group) in enumerate(heads):
        stop = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        out.setdefault(group, []).append((end, stop))
    return out


def _roles(text: str, spans: list[tuple[int, int]], run_date: date) -> list[Role]:
    roles: list[Role] = []
    for lo, hi in spans:
        region = text[lo:hi]
        ranges = parse_date_ranges(region, run_date)
        for i, (start, end, s, e) in enumerate(ranges):
            prev_end = ranges[i - 1][3] if i else 0
            nxt = ranges[i + 1][2] if i + 1 < len(ranges) else len(region)
            head_from = max(prev_end, s - 80)
            if i:  # the previous role's text ends at its last newline; start the window there too
                nl = region.rfind("\n", prev_end, s)
                if nl != -1:
                    head_from = max(head_from, nl)
            body = region[e:nxt]
            if i + 1 < len(ranges):
                nl = body.rfind("\n")
                if nl != -1:
                    body = body[:nl]
                else:
                    # Flat text: the next role's company/title sits just before its dates.
                    body = _drop_trailing_header(body)
            window = (region[head_from:s] + " " + region[e:e + 40]).strip()
            roles.append(Role(window=window, start=start, end=end, text=body.strip()))
    roles.sort(key=lambda r: r.start, reverse=True)
    return roles


def _drop_trailing_header(body: str) -> str:
    """In flat PDF text, cut after the last sentence end so the next role's header isn't included."""
    cut = max(body.rfind(". "), body.rfind("; "), body.rfind("• "))
    return body[: cut + 1] if cut != -1 else body


def _complement(length: int, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[0, length) minus `spans`: with no Experience heading, search for roles outside education/projects."""
    out, pos = [], 0
    for a, b in sorted(spans):
        if a > pos:
            out.append((pos, a))
        pos = max(pos, b)
    if pos < length:
        out.append((pos, length))
    return out or [(0, length)]


def _degree(text: str, education: str) -> Optional[int]:
    text, education = _NOT_DEGREE.sub(" ", text), _NOT_DEGREE.sub(" ", education)
    levels = [lvl for lvl, rx in _DEGREE_LONG if rx.search(education or text)]
    if education:
        levels += [lvl for lvl, rx in _DEGREE_ABBR if rx.search(education)]
    return max(levels) if levels else None


def parse_resume(markdown: str, run_date: date) -> ParsedResume:
    text = _PAGE_MARKER.sub(" ", markdown or "").strip()
    heads = _headings(text)
    regions = _regions(text, heads)

    def joined(group: str) -> str:
        return " ".join(text[a:b].strip() for a, b in regions.get(group, []))

    head_end = heads[0][0] if heads else len(text)
    headline = re.sub(r"[#*_]+", " ", text[:head_end])
    headline = re.sub(r"\s+", " ", headline).strip()[:200]
    exp_spans = regions.get("experience") or _complement(
        len(text), regions.get("education", []) + regions.get("other", [])
    )
    education = joined("education")
    stated = _STATED_YEARS.search(text)
    return ParsedResume(
        text=text,
        headline=headline,
        summary=joined("summary"),
        skills=joined("skills"),
        roles=_roles(text, exp_spans, run_date),
        degree_level=_degree(text, education),
        stated_years=float(stated.group(1)) if stated else None,
    )


def experience_years(roles: list[Role], run_date: date) -> Optional[float]:
    if not roles:
        return None
    spans = sorted((r.start, r.end or run_date) for r in roles)
    total_days = 0
    cur_start, cur_end = spans[0]
    for s, e in spans[1:]:
        if s <= cur_end:
            cur_end = max(cur_end, e)
        else:
            total_days += (cur_end - cur_start).days
            cur_start, cur_end = s, e
    total_days += (cur_end - cur_start).days
    return round(total_days / 365.25, 2)


def health(markdown: str) -> Health:
    """Text-detectable parseability checks (spec §3.1 badge). High 20, medium 8, low 3."""
    pages = len(_PAGE_MARKER.findall(markdown or ""))
    text = _PAGE_MARKER.sub(" ", markdown or "")
    groups = {g for _, _, g in _headings(text)}
    checks = [
        ("too_little_text", 20, len(text.split()) < 50),
        ("no_email", 20, not _EMAIL.search(text)),
        ("no_phone", 8, not _PHONE.search(text)),
        ("missing_headings", 8, not {"experience", "education", "skills"} <= groups),
        ("over_two_pages", 3, pages > 2),
    ]
    issues = [name for name, _, bad in checks if bad]
    score = max(0, 100 - sum(cost for _, cost, bad in checks if bad))
    return Health(score=score, issues=issues)
