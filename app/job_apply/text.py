"""Text helpers shared by JD parsing, resume parsing and scoring."""
import re
import unicodedata
from functools import lru_cache
from typing import Iterable

_DASHES = re.compile(r"[‐-―−]")
_WS = re.compile(r"\s+")
# Tokens keep the symbols tech names rely on: c++, c#, node.js, .net, ci/cd.
_TOKEN = re.compile(r"\.?[a-z0-9][a-z0-9+#./_-]*")

STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because been before being below
    between both but by can could did do does doing down during each etc few for from further had has have
    having he her here hers herself him himself his how i if in into is it its itself just like may me might
    more most must my myself no nor not now of off on once only or other our ours ourselves out over own per
    same she should so some such than that the their theirs them themselves then there these they this those
    through to too under until up us very via was we well were what when where which while who whom why will
    with within without would you your yours yourself yourselves
    """.split()
)


def normalize(s: str) -> str:
    """NFKC, unified dashes, lowercase, single spaces."""
    s = unicodedata.normalize("NFKC", s or "")
    s = _DASHES.sub("-", s)
    return _WS.sub(" ", s).strip().lower()


def tokens(s: str) -> list[str]:
    out = []
    for m in _TOKEN.finditer(normalize(s)):
        tok = m.group(0).rstrip(".-/_")
        if tok:
            out.append(tok)
    return out


def singular(w: str) -> str:
    lw = w.lower()
    if len(w) <= 3 or lw.endswith("ss"):
        return w
    if lw.endswith("ies"):
        return w[:-3] + "y"
    if lw.endswith(("ches", "shes", "xes", "sses")):
        return w[:-2]
    if lw.endswith("s"):
        return w[:-1]
    return w


def _core(term: str) -> str:
    """Regex body for a term: flexible inner spacing, plural-tolerant last word."""
    words = term.strip().split()
    if not words:
        return r"(?!)"
    last = words[-1]
    if last[-1:].isalpha():
        base = singular(last)
        if base.lower().endswith("y") and base != last:
            last_pat = re.escape(base[:-1]) + r"(?:y|ies)"
        else:
            last_pat = re.escape(base) + r"(?:s|es)?"
    else:
        last_pat = re.escape(last)
    parts = [re.escape(w) for w in words[:-1]] + [last_pat]
    return r"[\s-]+".join(parts)


_BEFORE = r"(?<![A-Za-z0-9])"
_AFTER = r"(?![A-Za-z0-9+#])"


@lru_cache(maxsize=8192)
def term_regex(term: str, case_sensitive: bool = False) -> "re.Pattern[str]":
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(_BEFORE + "(?:" + _core(term) + ")" + _AFTER, flags)


@lru_cache(maxsize=8192)
def _alternation(variants: tuple[str, ...]) -> "re.Pattern[str]":
    uniq = sorted({v.strip().lower(): v.strip() for v in variants if v and v.strip()}.values(), key=len, reverse=True)
    body = "|".join(_core(v) for v in uniq) or r"(?!)"
    return re.compile(_BEFORE + "(?:" + body + ")" + _AFTER, re.IGNORECASE)


def count_mentions(text: str, variants: Iterable[str]) -> int:
    """Non-overlapping mentions of any variant."""
    return len(_alternation(tuple(variants)).findall(text or ""))


def any_variant_regex(variants: Iterable[str]) -> "re.Pattern[str]":
    return _alternation(tuple(variants))
