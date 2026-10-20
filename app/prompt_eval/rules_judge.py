"""The rules judge: scores a generated resume without a model call per resume.

Match is the Job Applies score (app/job_apply/scoring.py) against a reading of the job description. The
reading costs one cheap extraction call per job description and is stored on it, so later runs reuse it.
Believability starts at 100 and loses capped points for skills no role backs up, repeated terms, text
copied from the posting, the same stack in every role, and timeline problems. Every part is returned with
its score, weight and a plain reason, so the UI can show why the numbers are what they are.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from app import ai_usage
from app.job_apply import extract as extract_mod
from app.job_apply.resume_parse import ParsedResume, parse_resume
from app.job_apply.schema import JobExtraction, ResumeScore, TermHit
from app.job_apply.scoring import Idf, build_idf, score_resume, term_weights
from app.job_apply.sections import substantive_text
from app.job_apply.text import any_variant_regex, count_mentions, tokens
from app.model_catalog import spec_for_api_model, usage_cost
from app.models import EvalJobDescription, UserExtra
from app.models.prompt_eval import JudgeVerdict, ScorePart

log = logging.getLogger(__name__)

MATCH_LABELS = {
    "required": "Required terms",
    "keywords": "All keywords",
    "years": "Years of experience",
    "title": "Job title",
    "education": "Education",
    "relevance": "Wording overlap",
}
WHERE_LABEL = {"recent": "recent role", "skills": "skills or summary", "old": "older role"}
HOW_LABEL = {"variant": "alias", "fuzzy": "close spelling"}

# Believability checks: the most points each can take off (they sum to 100).
UNBACKED_MAX = 30
REPEAT_MAX = 20
COPY_MAX = 25
SAME_STACK_MAX = 15
TIMELINE_MAX = 10

REPEAT_LIMIT = 6  # mentions of one job term before it reads as stuffing
REPEAT_STEP = 5  # points per over-repeated term
SHINGLE = 8  # words in a run that counts as copied text
COPY_FULL_SHARE = 0.2  # this share of copied runs takes the full COPY_MAX
SAME_STACK_SHARE = 0.8
SAME_STACK_MIN_ROLES = 3
OVERLAP_DAYS = 31  # overlap tolerated between consecutive roles
TIMELINE_STEP = 5

MAX_LIST = 10
MAX_MISSING = 8
MAX_STRENGTHS = 6


# ---------- the job description reading ----------


def reading_key(text: str) -> str:
    return f"{extract_mod.jd_hash(text)}:{extract_mod.PROMPT_VERSION}"


async def jd_reading(
    jd: EvalJobDescription, user: Optional[UserExtra], *, run_date: date, extract=None,
) -> tuple[JobExtraction, Optional[float], bool]:
    """(reading, cost of this call, reused). A stored reading for the same text and extraction prompt is reused;
    otherwise one extraction call, stored when the model answered (a rules fallback is not stored, so a later run
    can still get the model's reading)."""
    key = reading_key(jd.text)
    if jd.extraction is not None and jd.extractionKey == key:
        return jd.extraction, 0.0, True

    spent: list[Optional[float]] = []

    async def priced(prompt, u, **kw):
        from app.llm_router import complete_with_fallback

        res = await complete_with_fallback(prompt, u, **kw)
        spec = spec_for_api_model(res.model)
        spent.append(usage_cost(spec, res.usage) if spec else None)
        return res

    with ai_usage.usage_scope(user_id=getattr(user, "id", None), feature="eval_extract", ref_id=jd.id):
        ex, source = await (extract or extract_mod.extract_job)(jd.text, user=user, run_date=run_date, llm=priced)
    if source == "llm":
        await EvalJobDescription.get_motor_collection().update_one(
            {"_id": jd.id},
            {"$set": {"extraction": ex.model_dump(mode="json"), "extractionKey": key}},
        )
        jd.extraction, jd.extractionKey = ex, key
    cost = sum(c for c in spent if c is not None) if any(c is not None for c in spent) else None
    return ex, cost, False


def build_run_idf(texts: list[str]) -> Idf:
    """Term rarity across the run's job descriptions, the way a Job Applies run builds it."""
    return build_idf([substantive_text(t) or t for t in texts if t])


# ---------- match ----------


def match_parts(score: ResumeScore) -> list[ScorePart]:
    return [
        ScorePart(key=k, label=MATCH_LABELS.get(k, k), score=round(c.score, 1), weight=c.weight, detail=c.detail)
        for k, c in score.components.items()
    ]


# ---------- believability ----------


def _part(key: str, label: str, penalty: int, cap: int, detail: str) -> ScorePart:
    return ScorePart(key=key, label=label, score=round(100.0 * (cap - penalty) / cap, 1), weight=cap, detail=detail)


def _unbacked(hits: list[TermHit], variants: dict[str, tuple[str, ...]], parsed: ParsedResume) -> tuple[int, str]:
    found = [h for h in hits if h.credit > 0]
    if not parsed.roles:
        return 0, "no dated roles found, so this was not checked"
    if not found:
        return 0, "no job terms found to check"
    roles_text = " \n".join(r.text for r in parsed.roles)
    unbacked = [h.term for h in found if not any_variant_regex(variants[h.term]).search(roles_text)]
    penalty = round(UNBACKED_MAX * len(unbacked) / len(found))
    if not unbacked:
        return 0, f"all {len(found)} matched job terms appear in a role"
    shown = ", ".join(unbacked[:5]) + ("…" if len(unbacked) > 5 else "")
    return penalty, f"{len(unbacked)} of {len(found)} matched job terms appear in no role: {shown}"


def _repeated(variants: dict[str, tuple[str, ...]], text: str) -> tuple[int, str]:
    over = sorted(
        ((term, n) for term, v in variants.items() if (n := count_mentions(text, v)) > REPEAT_LIMIT),
        key=lambda x: -x[1],
    )
    if not over:
        return 0, f"no job term appears more than {REPEAT_LIMIT} times"
    penalty = min(REPEAT_MAX, REPEAT_STEP * len(over))
    return penalty, "repeated: " + ", ".join(f"{t} ×{n}" for t, n in over[:5])


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = tokens(text)
    return {tuple(words[i:i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def _copied(resume_text: str, jd: str) -> tuple[int, str]:
    mine = _shingles(resume_text)
    if not mine:
        return 0, "resume too short to check"
    share = len(mine & _shingles(jd)) / len(mine)
    penalty = min(COPY_MAX, round(COPY_MAX * share / COPY_FULL_SHARE))
    if not penalty:
        return 0, f"{share:.0%} of {SHINGLE}-word runs copied from the posting"
    return penalty, f"{share:.0%} of the resume's {SHINGLE}-word runs are copied word for word from the posting"


def _same_stack(ex: JobExtraction, variants: dict[str, tuple[str, ...]], parsed: ParsedResume) -> tuple[int, str]:
    required = [t.term for t in ex.terms if t.tier == "required" and not t.soft]
    if len(parsed.roles) < SAME_STACK_MIN_ROLES or len(required) < 2:
        return 0, f"needs {SAME_STACK_MIN_ROLES}+ roles and 2+ required terms to check"

    def share(text: str) -> float:
        return sum(1 for t in required if any_variant_regex(variants[t]).search(text)) / len(required)
    if all(share(r.text) >= SAME_STACK_SHARE for r in parsed.roles):
        return SAME_STACK_MAX, f"all {len(parsed.roles)} roles name at least {SAME_STACK_SHARE:.0%} of the required stack"
    return 0, "roles use different stacks"


def _timeline(parsed: ParsedResume, run_date: date) -> tuple[int, str]:
    roles = sorted(parsed.roles, key=lambda r: r.start)
    if not roles:
        return 0, "no dated roles found"
    issues: list[str] = []
    for prev, cur in zip(roles, roles[1:]):
        prev_end = prev.end or run_date
        if (prev_end - cur.start).days > OVERLAP_DAYS:
            issues.append(f"roles overlap ({prev.start:%b %Y} and {cur.start:%b %Y})")
    issues += [f"a role ends in the future ({r.end:%b %Y})" for r in roles if r.end and r.end > run_date]
    if not issues:
        return 0, "dates run in order with no overlaps"
    return min(TIMELINE_MAX, TIMELINE_STEP * len(issues)), "; ".join(issues[:3])


def believability(
    ex: JobExtraction, jd: str, parsed: ParsedResume, resume_text: str, hits: list[TermHit], run_date: date,
) -> tuple[int, list[ScorePart]]:
    variants = {w.term.term: w.variants for w in term_weights(ex, jd)}
    checks = [
        ("unbacked", "Skills backed by roles", UNBACKED_MAX, _unbacked(hits, variants, parsed)),
        ("repeated", "Repeated terms", REPEAT_MAX, _repeated(variants, resume_text)),
        ("copied", "Copied posting text", COPY_MAX, _copied(resume_text, jd)),
        ("same_stack", "Varied stack across roles", SAME_STACK_MAX, _same_stack(ex, variants, parsed)),
        ("timeline", "Timeline", TIMELINE_MAX, _timeline(parsed, run_date)),
    ]
    parts = [_part(key, label, penalty, cap, detail) for key, label, cap, (penalty, detail) in checks]
    total = 100 - sum(penalty for _, _, _, (penalty, _) in checks)
    return max(0, total), parts


# ---------- the verdict ----------


def _strength(h: TermHit) -> str:
    how = HOW_LABEL.get(h.match)
    return f"{h.term}: {WHERE_LABEL.get(h.where, h.where)}" + (f" ({how})" if how else "")


def _summary(match: int, band: str, mparts: list[ScorePart], belief: int, bparts: list[ScorePart]) -> str:
    first = f"Match {match} ({band})."
    if mparts:
        best = max(mparts, key=lambda p: p.score)
        worst = min(mparts, key=lambda p: p.score)
        first = f"Match {match} ({band}): {best.label.lower()} is strongest at {best.score:.0f}"
        first += f", {worst.label.lower()} is weakest at {worst.score:.0f}." if worst is not best else "."
    hurt = [p for p in bparts if p.score < 100]
    if hurt:
        worst_b = max(hurt, key=lambda p: p.weight * (100 - p.score))
        second = f"Believability {belief}: the biggest loss is {worst_b.label.lower()} ({worst_b.detail})."
    else:
        second = f"Believability {belief}: no stuffing, copying, or timeline problems found."
    return f"{first} {second}"


def judge_rules(
    *, resume_text: str, jd_text: str, extraction: JobExtraction, idf: Idf, run_date: date,
) -> JudgeVerdict:
    parsed = parse_resume(resume_text or "", run_date)
    score = score_resume(
        extraction, jd_text, parsed, idf,
        run_date=run_date, account_id="", resume_id="", filename="", uploaded_at=None,
    )
    mparts = match_parts(score)
    belief, bparts = believability(extraction, jd_text, parsed, resume_text or "", score.terms, run_date)
    by_weight = sorted(score.terms, key=lambda h: -h.weight)
    red = list(score.knockouts) + [f"{p.label}: {p.detail}" for p in bparts if p.score < 100]
    return JudgeVerdict(
        matchScore=score.total,
        believabilityScore=belief,
        matchNotes=[f"{p.label} {p.score:.0f} (weight {p.weight}): {p.detail}" for p in mparts][:MAX_LIST],
        believabilityNotes=[f"{p.label} {p.score:.0f} (weight {p.weight}): {p.detail}" for p in bparts][:MAX_LIST],
        missingRequirements=[f"{h.term} ({h.tier})" for h in by_weight if h.credit == 0][:MAX_MISSING],
        redFlags=red[:MAX_LIST],
        strengths=[_strength(h) for h in by_weight if h.credit > 0][:MAX_STRENGTHS],
        summary=_summary(score.total, score.band, mparts, belief, bparts),
        matchParts=mparts,
        believabilityParts=bparts,
    )
