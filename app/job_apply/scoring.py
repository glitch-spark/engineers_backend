"""Scoring v2 (spec §3.1, §3.3–3.6, §3.8): pure functions, no I/O."""
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from rapidfuzz import fuzz, process

from app.job_apply.resume_parse import ParsedResume, experience_years
from app.job_apply.schema import ComponentScore, Degree, JobExtraction, MinYears, ResumeScore, Term, TermHit
from app.job_apply.sections import substantive_text
from app.job_apply.skills_dict import CASE_SENSITIVE, aliases_for
from app.job_apply.text import (
    SENIORITY_LEVEL,
    STOPWORDS,
    any_variant_regex,
    core_title,
    count_mentions,
    normalize,
    seniority_word,
    term_regex,
    tokens,
)

WEIGHTS: dict[str, int] = {"required": 32, "keywords": 27, "years": 13, "title": 11, "education": 9, "relevance": 8}
TIER_WEIGHT = {"required": 3.0, "core": 2.0, "mentioned": 1.5, "preferred": 1.0, "context": 0.5}
CREDIT = {"exact": 1.0, "variant": 0.85, "fuzzy": 0.70}
WHERE_MULT = {"recent": 1.0, "skills": 0.9, "old": 0.8}
K = 0.18
RECENT_YEARS = 5
FUZZY_MIN_RATIO = 90
FUZZY_MIN_LEN = 5


@dataclass
class WeightedTerm:
    term: Term
    weight: float
    variants: tuple[str, ...]


def _variants(t: Term) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for v in (t.term, *t.aliases, *aliases_for(t.term)):
        if v and v.strip():
            seen.setdefault(v.strip().lower(), v.strip())
    return tuple(seen.values())


def term_weights(ex: JobExtraction, jd: str) -> list[WeightedTerm]:
    out = []
    for t in ex.terms:
        variants = _variants(t)
        mentions = count_mentions(jd, variants)
        weight = TIER_WEIGHT[t.tier] + min(1.0, 0.25 * max(0, mentions - 1))
        if ex.title and any_variant_regex(variants).search(ex.title):
            weight += 1.0
        if t.soft:
            weight *= 0.5
        if t.auto:
            weight *= 0.5
        out.append(WeightedTerm(term=t, weight=weight, variants=variants))
    return out


# ---------- finding terms in the resume ----------


def _regex(v: str):
    return term_regex(v, v in CASE_SENSITIVE)


def _fuzzy_found(variants: tuple[str, ...], text: str) -> bool:
    toks = tokens(text)
    if not toks:
        return False
    grams = {1: toks}
    for v in variants:
        if len(v) < FUZZY_MIN_LEN:
            continue
        n = len(v.split())
        if n not in grams:
            grams[n] = [" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)]
        if grams[n] and process.extractOne(v.lower(), grams[n], scorer=fuzz.ratio, score_cutoff=FUZZY_MIN_RATIO):
            return True
    return False


def _match_in(wt: WeightedTerm, text: str) -> Optional[str]:
    if not text:
        return None
    if _regex(wt.term.term).search(text):
        return "exact"
    if any(_regex(v).search(text) for v in wt.variants[1:]):
        return "variant"
    if _fuzzy_found(wt.variants, text):
        return "fuzzy"
    return None


def _evidence(r: ParsedResume, run_date: date) -> list[tuple[str, str]]:
    """(where, text) pairs: recent role bodies, old role bodies, and everything outside role bodies."""
    cutoff = date(run_date.year - RECENT_YEARS, run_date.month, 1)
    recent = " \n".join(x.text for x in r.roles if x.end is None or x.end >= cutoff)
    old = " \n".join(x.text for x in r.roles if x.end is not None and x.end < cutoff)
    rest = r.text
    for x in r.roles:
        if x.text:
            rest = rest.replace(x.text, " ")
    return [("recent", recent), ("skills", rest), ("old", old)]


def match_term(wt: WeightedTerm, r: ParsedResume, run_date: date, evidence: Optional[list] = None) -> TermHit:
    best = (0.0, "missing", "none")
    for where, text in evidence or _evidence(r, run_date):
        how = _match_in(wt, text)
        if how:
            credit = CREDIT[how] * WHERE_MULT[where]
            if credit > best[0]:
                best = (credit, how, where)
    return TermHit(
        term=wt.term.term, tier=wt.term.tier, weight=wt.weight, credit=round(best[0], 4), match=best[1], where=best[2]
    )


def weighted_share(hits: list[TermHit]) -> Optional[float]:
    total = sum(h.weight for h in hits)
    if not hits or total <= 0:
        return None
    return 100.0 * sum(h.weight * h.credit for h in hits) / total


# ---------- components ----------


def years_score(req: Optional[MinYears], have: Optional[float]) -> Optional[float]:
    if req is None or req.value <= 0:
        return None
    if have is None:
        return 50.0
    if have >= req.value:
        return 100.0
    return max(0.0, 100.0 - 120.0 * (req.value - have) / req.value)


def seniority_level(title: str) -> Optional[int]:
    word = seniority_word(title)
    return SENIORITY_LEVEL[word] if word else None


def title_score(ex: JobExtraction, r: ParsedResume) -> tuple[Optional[float], str]:
    core = core_title(ex.title)
    if not core:
        return None, "no job title"
    candidates = [r.headline] + [x.window for x in r.roles]
    if term_regex(core).search(normalize(r.text)):
        sim, closest = 100.0, core
    else:
        sim, closest = 0.0, ""
        for c in candidates:
            s = fuzz.token_set_ratio(core, core_title(c))
            if s > sim:
                sim, closest = float(s), c
    job_level = SENIORITY_LEVEL.get(ex.seniority or "") if ex.seniority else seniority_level(ex.title)
    resume_level = seniority_level(r.roles[0].window) if r.roles else None
    if resume_level is None:
        resume_level = seniority_level(r.headline)
    factor = 1.0
    if job_level is not None and resume_level is not None:
        diff = abs(job_level - resume_level)
        factor = 1.0 if diff <= 1 else 0.8 if diff == 2 else 0.6
    detail = f"similarity {sim:.0f}" + (f" × seniority {factor}" if factor != 1.0 else "")
    if closest and closest != core:
        detail += f"; closest: {closest[:60]}"
    return sim * factor, detail


def education_score(req: Optional[Degree], have: Optional[int]) -> Optional[float]:
    if req is None:
        return None
    lenient = req.equivalentExperience or not req.required
    if have is None:
        return 40.0 if req.equivalentExperience else 20.0
    gap = req.level - have
    if gap <= 0:
        return 100.0
    if gap == 1:
        return 75.0 if lenient else 60.0
    return 40.0 if lenient else 25.0


# ---------- content relevance (TF-IDF over the run's JDs) ----------


@dataclass
class Idf:
    idf: dict[str, float]
    default: float


def _features(text: str) -> list[str]:
    words = [t for t in tokens(text) if t not in STOPWORDS and not t.isdigit() and len(t) > 1]
    return words + [f"{a} {b}" for a, b in zip(words, words[1:])]


def build_idf(docs: list[str]) -> Idf:
    n = len(docs)
    df: Counter[str] = Counter()
    for d in docs:
        df.update(set(_features(d)))
    idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}
    return Idf(idf=idf, default=math.log(1 + n) + 1)


def _vector(text: str, idf: Idf) -> dict[str, float]:
    counts = Counter(_features(text))
    return {t: (1 + math.log(c)) * idf.idf.get(t, idf.default) for t, c in counts.items()}


def relevance(resume_text: str, jd_text: str, idf: Idf, k: Optional[float] = None) -> tuple[float, float]:
    k = K if k is None else k  # read at call time so calibration can tune the module constant
    a, b = _vector(resume_text, idf), _vector(jd_text, idf)
    if not a or not b:
        return 0.0, 0.0
    dot = sum(v * b.get(t, 0.0) for t, v in a.items())
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    sim = dot / norm if norm else 0.0
    return 100.0 * (1 - math.exp(-sim / k)), sim


# ---------- combining ----------


def combine(parts: dict[str, Optional[float]], weights: dict[str, int] = WEIGHTS) -> int:
    used = {k: v for k, v in parts.items() if v is not None and k in weights}
    total_w = sum(weights[k] for k in used)
    if not total_w:
        return 0
    return int(math.floor(sum(weights[k] * v for k, v in used.items()) / total_w + 0.5))


def band(total: int) -> str:
    if total >= 80:
        return "strong"
    if total >= 65:
        return "good"
    if total >= 50:
        return "fair"
    return "weak"


def knockouts(
    ex: JobExtraction, hits: list[TermHit], have_years: Optional[float], have_degree: Optional[int]
) -> list[str]:
    soft = {t.term for t in ex.terms if t.soft}
    out = [f"Missing required: {h.term}" for h in hits if h.tier == "required" and h.credit == 0 and h.term not in soft]
    if ex.minYears and ex.minYears.required and have_years is not None and have_years < ex.minYears.value - 0.5:
        out.append(f"Experience {have_years:.1f} yrs, {ex.minYears.value:g}+ required")
    deg = ex.degree
    if deg and deg.required and not deg.equivalentExperience and (have_degree is None or have_degree < deg.level):
        out.append("Degree below the required level")
    return out


def score_resume(
    ex: JobExtraction,
    jd: str,
    r: ParsedResume,
    idf: Idf,
    *,
    run_date: date,
    account_id: str,
    resume_id: str,
    filename: str,
    uploaded_at: Optional[datetime],
) -> ResumeScore:
    evidence = _evidence(r, run_date)
    hits = [match_term(wt, r, run_date, evidence) for wt in term_weights(ex, jd)]
    req_hits = [h for h in hits if h.tier == "required"]
    have_years = experience_years(r.roles, run_date)
    if have_years is None:
        have_years = r.stated_years
    title, title_detail = title_score(ex, r)
    rel, sim = relevance(r.text, substantive_text(jd) or jd, idf)

    def found(hs: list[TermHit]) -> str:
        return f"{sum(1 for h in hs if h.credit > 0)} of {len(hs)}"

    parts: dict[str, tuple[Optional[float], str]] = {
        "required": (weighted_share(req_hits), f"{found(req_hits)} required terms found"),
        "keywords": (weighted_share(hits), f"{found(hits)} terms found"),
        "years": (
            years_score(ex.minYears, have_years),
            f"{ex.minYears.value:g}+ asked; {have_years:.1f} found" if ex.minYears and have_years is not None
            else (f"{ex.minYears.value:g}+ asked; no dated roles" if ex.minYears else ""),
        ),
        "title": (title, title_detail),
        "education": (education_score(ex.degree, r.degree_level), "" if ex.degree is None else f"level {r.degree_level or 0} vs {ex.degree.level}"),
        "relevance": (rel, f"cosine {sim:.2f}"),
    }
    components = {
        k: ComponentScore(score=round(v, 1), weight=WEIGHTS[k], detail=d) for k, (v, d) in parts.items() if v is not None
    }
    total = combine({k: v for k, (v, _) in parts.items()})
    return ResumeScore(
        accountId=account_id,
        resumeId=resume_id,
        filename=filename,
        total=total,
        band=band(total),
        components=components,
        terms=hits,
        knockouts=knockouts(ex, hits, have_years, r.degree_level),
        uploadedAt=uploaded_at,
    )


def pick_best(scores: list[ResumeScore]) -> Optional[ResumeScore]:
    if not scores:
        return None
    return max(scores, key=lambda s: (s.total, -len(s.knockouts), s.uploadedAt or datetime.min))
