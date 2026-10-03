"""Cover letter generation: draft, critique, revise on one chosen model, then deterministic checks.

Quality matters most here, so the letter gets a second look from the same model before it ships.
Failures of the checks never fail the job; the best letter produced is returned with its problems.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from app.llm_router import LlmResult, complete
from app.models import UserExtra
from app.resume_service import _normalize_dashes, _strip_fences, trim_jd

log = logging.getLogger(__name__)

MIN_WORDS = 150
MAX_WORDS = 450

_DRAFT_SYSTEM = (
    "You write a personalized cover letter for a job application. The "
    "letter MUST be grounded in two inputs supplied below:\n"
    "  1. CANDIDATE RESUME — the tailored resume's plain text. Every fact "
    "the letter cites (experience, companies, years, technologies, "
    "achievements, metrics) MUST come from this resume. Do NOT invent "
    "roles, companies, dates, projects, or numbers that are not present.\n"
    "  2. JOB DESCRIPTION — the role's requirements and priorities. The "
    "letter must tie the candidate's resume facts to what this specific "
    "JD asks for (skills, domain, seniority, mission). Do not paste JD "
    "phrases verbatim.\n"
    "\n"
    "Output rules:\n"
    "- PLAIN TEXT only. No Markdown, no HTML, no code fences, no "
    "surrounding commentary.\n"
    "- 3-5 short paragraphs separated by a blank line, unless the custom "
    "prompt below overrides.\n"
    "- Address the hiring team naturally; do not invent a recruiter's "
    "name.\n"
    "- If the resume lacks evidence for a JD requirement, do not claim "
    "it — pivot to adjacent strengths.\n"
    "- Do NOT include personal contact details (phone number, email, "
    "mailing address, LinkedIn URL, portfolio URL, or similar) unless the "
    "custom prompt below explicitly asks for them. Candidate name is "
    "allowed when natural; everything else stays out by default.\n"
)

_RUBRIC = (
    "- Specific to THIS job description and company, not a letter that fits any role.\n"
    "- Does not just repeat resume bullets; connects them to what the role needs.\n"
    "- No filler or cliches (\"I am writing to express my interest\", \"passionate\", \"team player\").\n"
    "- Names the correct company and role; no placeholders.\n"
    "- Every claim is supported by the resume; nothing fabricated.\n"
    "- Appropriate length (3-5 short paragraphs) and a confident, natural tone.\n"
)

_PLACEHOLDER_RES = (
    re.compile(r"\[[^\]\n]{1,60}\]"),
    re.compile(r"\{\{.*?\}\}"),
    re.compile(r"<[^>\n]{1,60}>"),
    re.compile(r"\byour name\b", re.IGNORECASE),
)


@dataclass
class CoverLetterResult:
    text: str
    provider: str
    model: str
    model_id: str
    fallback_used: bool
    fallback_reason: Optional[str]
    cost_usd: Optional[float]
    problems: list[str] = field(default_factory=list)


def check_cover_letter(text: str, company: str, *, enforce_length: bool) -> list[str]:
    """Cheap deterministic checks; an empty list means the letter passes."""
    problems: list[str] = []
    if any(rx.search(text) for rx in _PLACEHOLDER_RES):
        problems.append("contains a placeholder such as [Your Name], {{...}} or <...>; replace it with real text")
    name = (company or "").strip()
    if name and name.lower() not in text.lower():
        problems.append(f"never mentions the company '{name}'")
    if enforce_length:
        words = len(text.split())
        if not MIN_WORDS <= words <= MAX_WORDS:
            problems.append(f"has {words} words; keep it between {MIN_WORDS} and {MAX_WORDS} words")
    return problems


def _context_blocks(resume_text: str, job_description: str, company: str) -> list[str]:
    blocks: list[str] = []
    if (company or "").strip():
        blocks.append(f"Target company: {company.strip()}")
    blocks.append(
        "=== CANDIDATE RESUME (source of all factual claims) ===\n"
        + ((resume_text or "").strip() or "(no resume text supplied)")
    )
    blocks.append(
        "=== JOB DESCRIPTION (what the letter must address) ===\n"
        + (trim_jd(job_description) or "(no JD supplied)")
    )
    return blocks


def build_draft_prompt(
    *, resume_text: str, job_description: str, company: str, prompt_body: str, hook: Optional[str]
) -> str:
    if not (resume_text or "").strip():
        log.warning("cover letter prompt missing resume text — output will be generic")
    if not (job_description or "").strip():
        log.warning("cover letter prompt missing job description — output will be generic")
    parts = [_DRAFT_SYSTEM]
    body = (prompt_body or "").strip()
    if body:
        parts.append(
            "CUSTOM PROMPT (candidate's style guidance — follow within the "
            "absolute rules above):\n" + body
        )
    if (hook or "").strip():
        parts.append(
            "ANGLE FROM THE CANDIDATE (work this in naturally, truthfully):\n" + hook.strip()
        )
    parts.extend(_context_blocks(resume_text, job_description, company))
    return "\n\n".join(parts)


def build_critique_prompt(*, draft: str, resume_text: str, job_description: str, company: str) -> str:
    return "\n\n".join(
        [
            "You are a strict hiring-manager editor reviewing a draft cover letter.",
            "Check it against this rubric:\n" + _RUBRIC,
            "If the letter already meets every point, reply with exactly: OK\n"
            "Otherwise reply with a short bullet list of the specific problems and what to change. "
            "No rewritten letter, no praise.",
            *_context_blocks(resume_text, job_description, company),
            "=== DRAFT COVER LETTER ===\n" + draft,
        ]
    )


def build_revise_prompt(
    *, draft: str, feedback: str, resume_text: str, job_description: str, company: str
) -> str:
    return "\n\n".join(
        [
            "Revise the cover letter below to fix every point of feedback. Keep it grounded in the "
            "resume (no invented facts). Output ONLY the final letter as plain text: no Markdown, "
            "no code fences, no commentary.",
            "=== FEEDBACK TO ADDRESS ===\n" + feedback.strip(),
            *_context_blocks(resume_text, job_description, company),
            "=== CURRENT LETTER ===\n" + draft,
        ]
    )


def _clean(text: str) -> str:
    return _normalize_dashes(_strip_fences(text))


async def generate_cover_letter(
    *,
    resume_text: str,
    job_description: str,
    company: str,
    prompt_body: str,
    user: Optional[UserExtra],
    model_id: Optional[str],
    hook: Optional[str] = None,
) -> CoverLetterResult:
    ctx = dict(resume_text=resume_text, job_description=job_description, company=company)
    results: list[LlmResult] = []

    draft = await complete(
        "cover_letter",
        build_draft_prompt(prompt_body=prompt_body, hook=hook, **ctx),
        user,
        model_id=model_id,
    )
    results.append(draft)
    producer = draft
    text = _clean(draft.text)
    # Every later step runs on the model that actually answered the draft.
    used_model_id = draft.model_id or model_id

    critique = await complete(
        "cover_letter",
        build_critique_prompt(draft=text, **ctx),
        user,
        model_id=used_model_id,
    )
    results.append(critique)
    feedback = critique.text.strip()
    if feedback.rstrip(".").strip().upper() != "OK":
        revised = await complete(
            "cover_letter",
            build_revise_prompt(draft=text, feedback=feedback, **ctx),
            user,
            model_id=used_model_id,
        )
        results.append(revised)
        producer = revised
        text = _clean(revised.text)

    enforce_length = not (prompt_body or "").strip()
    problems = check_cover_letter(text, company, enforce_length=enforce_length)
    if problems:
        retry = await complete(
            "cover_letter",
            build_revise_prompt(
                draft=text,
                feedback="Fix these problems:\n- " + "\n- ".join(problems),
                **ctx,
            ),
            user,
            model_id=used_model_id,
        )
        results.append(retry)
        producer = retry
        text = _clean(retry.text)
        problems = check_cover_letter(text, company, enforce_length=enforce_length)
        if problems:
            log.warning("cover letter still has problems after retry: %s", problems)

    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    reasons = [r.fallback_reason for r in results if r.fallback_reason]
    return CoverLetterResult(
        text=text,
        provider=producer.provider,
        model=producer.model,
        model_id=producer.model_id,
        fallback_used=any(r.fallback_used for r in results),
        fallback_reason=reasons[0] if reasons else None,
        cost_usd=sum(costs) if costs else None,
        problems=problems,
    )
