"""One job row × the selected resumes → scores, profile gates and one best resume per profile (spec §3.8)."""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from app.job_apply.gates import blocked, profile_gates
from app.job_apply.resume_parse import ParsedResume
from app.job_apply.schema import JobExtraction, ResumeScore
from app.job_apply.scoring import Idf, pick_best, score_resume


@dataclass
class ResumeInput:
    account_id: str
    resume_id: str
    filename: str
    uploaded_at: Optional[datetime]
    parsed: ParsedResume


@dataclass
class ProfileInfo:
    account_id: str
    name: str
    country: Optional[str]
    region: Optional[str]
    # No uploaded resumes selected but has an HTML template: not scored, its eligible jobs are tailored.
    tailor_only: bool = False


@dataclass
class RowResult:
    profile_gates: list[dict]
    scores: list[ResumeScore]
    best_by_profile: list[ResumeScore]
    top_score: Optional[int]
    # Tailor-only profiles this job is open to (job and profile gates pass).
    tailor_only: list[str] = field(default_factory=list)


def score_row(
    ex: JobExtraction,
    jd: str,
    *,
    job_blocked: bool,
    resumes: list[ResumeInput],
    profiles: dict[str, ProfileInfo],
    idf: Idf,
    run_date: date,
) -> RowResult:
    """Every resume is scored (so excluded rows stay inspectable); suggestions only for eligible profiles."""
    scores = [
        score_resume(
            ex, jd, r.parsed, idf,
            run_date=run_date, account_id=r.account_id, resume_id=r.resume_id,
            filename=r.filename, uploaded_at=r.uploaded_at,
        )
        for r in resumes
    ]
    order = list(dict.fromkeys(r.account_id for r in resumes))
    tailor_only = [acc for acc, info in profiles.items() if info.tailor_only and acc not in order]
    gates = {}
    for acc in tailor_only:
        info = profiles[acc]
        gates[acc] = profile_gates(ex, country=info.country, region=info.region)
    for acc in order:
        info = profiles.get(acc)
        gates[acc] = profile_gates(ex, country=info.country if info else None, region=info.region if info else None)

    top_by_profile = {acc: pick_best([s for s in scores if s.accountId == acc]) for acc in order}

    # Storage: term-by-term detail (the bulk of a row) is kept only for each profile's best resume,
    # also on excluded rows so "why" stays inspectable. Others keep components and knockouts.
    keep_terms = {s.resumeId for s in top_by_profile.values() if s is not None}
    scores = [s if s.resumeId in keep_terms else s.model_copy(update={"terms": []}) for s in scores]

    best: list[ResumeScore] = []
    if not job_blocked:
        for acc in order:
            top = top_by_profile[acc]
            if top is not None and not blocked(gates[acc]):
                best.append(top.model_copy(update={"terms": []}))  # summary copy; detail lives in `scores`
    best.sort(key=lambda s: s.total, reverse=True)
    return RowResult(
        profile_gates=[{"accountId": acc, "gates": gates[acc]} for acc in [*order, *tailor_only]],
        scores=scores,
        best_by_profile=best,
        top_score=best[0].total if best else None,
        tailor_only=[] if job_blocked else [acc for acc in tailor_only if not blocked(gates[acc])],
    )
