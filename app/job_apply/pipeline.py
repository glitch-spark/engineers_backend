"""One job row × the selected resumes → scores, profile gates and one best resume per profile (spec §3.8)."""
from dataclasses import dataclass
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


@dataclass
class RowResult:
    profile_gates: list[dict]
    scores: list[ResumeScore]
    best_by_profile: list[ResumeScore]
    top_score: Optional[int]


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
    gates = {}
    for acc in order:
        info = profiles.get(acc)
        gates[acc] = profile_gates(ex, country=info.country if info else None, region=info.region if info else None)

    best: list[ResumeScore] = []
    if not job_blocked:
        for acc in order:
            if blocked(gates[acc]):
                continue
            top = pick_best([s for s in scores if s.accountId == acc])
            if top is not None:
                best.append(top)
    best.sort(key=lambda s: s.total, reverse=True)
    return RowResult(
        profile_gates=[{"accountId": acc, "gates": gates[acc]} for acc in order],
        scores=scores,
        best_by_profile=best,
        top_score=best[0].total if best else None,
    )
