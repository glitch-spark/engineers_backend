"""Split a job description into headed sections and classify each line's importance.

Section kinds map to the draft's importance table: required (Requirements, Qualifications, About you),
core (Responsibilities, What you'll do), mentioned (text with no recognised heading), preferred
(Nice to have, Bonus), context (About the company) and ignored (Benefits, legal, EEO).
"""
import re
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["required", "core", "mentioned", "preferred", "context", "ignored"]

# Order matters: the first matching pattern wins ("about the role" before "about").
_HEADINGS: list[tuple[Kind, str]] = [
    ("core", r"about (?:the|this) (?:role|position|job|opportunity)|the role|your role|in this role|the opportunity"
             r"|(?:key |main |primary |core )?responsibilities|duties|what you(?:'| a|’)?ll do|what you will do"
             r"|what you(?:'ll| will) be doing|day[- ]to[- ]day|your (?:impact|mission)|the job|job description"
             r"|role description|you will|you'll"),
    ("preferred", r"(?:preferred|desired|bonus|additional|nice[- ]to[- ]have)(?: (?:qualifications|skills|experience|points))?"
                  r"|nice[- ]to[- ]haves|pluses|bonus points|good to have|it(?:'s| is|’s) a plus if(?: you have)?"
                  r"|extra credit|ideally"),
    ("required", r"(?:minimum |basic |required |key |essential )?(?:qualifications|requirements)"
                 r"|required (?:skills|experience)|skills(?: (?:and|&) (?:experience|qualifications))?"
                 r"|what you(?:'|’)?ll (?:bring|need)|what you bring|what you need|what we(?:'re| are)? looking for"
                 r"|who you are|about you|you have|you are|your profile|your background|must[- ]haves?"
                 r"|experience(?: (?:and|&) skills)?|technical skills|your skills|ideal candidate|the ideal candidate"),
    ("ignored", r"(?:our |the )?benefits(?: (?:and|&) perks)?|perks(?: (?:and|&) benefits)?|what we offer|we offer"
                r"|compensation(?: (?:and|&) benefits)?|salary(?: range)?|pay(?: range| transparency)?"
                r"|equal (?:employment )?opportunity(?: employer)?|eeo(?: statement)?|diversity(?: (?:and|&) inclusion)?"
                r"|accommodations?|privacy(?: notice| policy)?|legal|disclaimer|why join us|why (?:work|you'll love working) (?:here|with us)"
                r"|how to apply|application process|interview process|our hiring process"),
    ("context", r"about(?: us| the company| the team| our team)?|who we are|our (?:mission|story|company|team|culture|values)"
                r"|company (?:overview|description)|the company|the team|overview"),
]
_HEADING_RES = [(k, re.compile(rf"^(?:{p})$", re.I)) for k, p in _HEADINGS]
_HEADING_PREFIX_RES = [(k, re.compile(rf"^(?:{p})\b", re.I)) for k, p in _HEADINGS]

_MARKUP = re.compile(r"^\s*(?:#{1,6}\s*|\*\*|__)|(?:\*\*|__)\s*$")
_PREFERRED_WORDS = re.compile(
    r"\b(?:preferred|a plus|is a plus|plus\b(?= if)|bonus|ideally|nice[- ]to[- ]have|desirable|advantageous"
    r"|good to have|would be great|would be a plus|not required but)\b",
    re.I,
)
_REQUIRED_WORDS = re.compile(r"\b(?:required|requires?|must|essential|mandatory|minimum|at least|\d+\s*\+?\s*years?)\b", re.I)


@dataclass
class Section:
    kind: Kind
    heading: str
    lines: list[str] = field(default_factory=list)


def _heading_kind(raw: str) -> Kind | None:
    line = raw.strip()
    if not line or len(line) > 70 or line.startswith(("-", "•", "*  ", "* ")):
        return None
    marked = bool(_MARKUP.search(line)) or line.endswith(":")
    text = _MARKUP.sub("", line).strip().rstrip(":").strip()
    text = re.sub(r"\s+", " ", text.replace("’", "'"))
    if not text or text.endswith("."):
        return None
    for kind, rx in _HEADING_RES:
        if rx.match(text):
            return kind
    # "Requirements & Qualifications", "About Acme", "What you'll do at Acme:" — only with a heading marker
    # or a short line, so prose like "Experience with Python" isn't mistaken for a heading.
    if marked or len(text.split()) <= 4:
        for kind, rx in _HEADING_PREFIX_RES:
            if rx.match(text) and (marked or kind in ("ignored", "context") and len(text.split()) <= 3):
                return kind
    return None


def split_sections(jd: str) -> list[Section]:
    sections: list[Section] = []
    current: Section | None = None
    for raw in (jd or "").splitlines():
        kind = _heading_kind(raw)
        if kind is not None:
            current = Section(kind=kind, heading=raw.strip())
            sections.append(current)
            continue
        if not raw.strip():
            continue
        if current is None:
            current = Section(kind="mentioned", heading="")
            sections.append(current)
        current.lines.append(raw.strip())
    return sections


def line_kind(line: str, section: Kind) -> Kind:
    """A line's own wording can override its section (draft §2)."""
    if section in ("context", "ignored"):
        return section
    if _PREFERRED_WORDS.search(line):
        return "preferred"
    if section in ("core", "mentioned") and _REQUIRED_WORDS.search(line):
        return "required"
    return section


def substantive_text(jd: str) -> str:
    """JD text without About-the-company, Benefits and legal sections."""
    return "\n".join(
        line for s in split_sections(jd) if s.kind not in ("context", "ignored") for line in s.lines
    )
