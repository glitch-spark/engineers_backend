"""Cover letter generation: draft, critique, revise on one chosen model, then deterministic checks.

Quality matters most here, so the letter gets a second look from the same model before it ships.
Failures of the checks never fail the job; the best letter produced is returned with its problems.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from app.llm_router import LlmResult, complete
from app.models import UserExtra
from app.resume_service import _normalize_dashes, _strip_fences, trim_jd

log = logging.getLogger(__name__)

MIN_WORDS = 150
MAX_WORDS = 450
# Per-answer cap on the screening Q&A fed to the draft, to bound its token cost.
SCREENING_ANSWER_MAX_CHARS = 600

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
    re.compile(r"<[A-Za-z][^>\n]{0,58}>"),
    re.compile(r"\byour name\b", re.IGNORECASE),
)


_LEGAL_SUFFIXES = frozenset(
    {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
     "gmbh", "plc", "lp", "llp", "ag", "sa", "bv", "pty"}
)
_HOSTNAME_RE = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+$", re.IGNORECASE)


def _company_core(company: str) -> str:
    """The name a letter would actually write: no legal suffix ("Acme, Inc." -> "acme").

    Empty for hostnames: Job Applies falls back to the site's host when a posting has no company,
    and the letter should not be pushed to mention that."""
    name = (company or "").strip()
    if not name or _HOSTNAME_RE.match(name):
        return ""
    words = re.findall(r"[\w&'-]+", name.lower())
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


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
    core = _company_core(company)
    if core and core not in text.lower():
        problems.append(f"never mentions the company '{(company or '').strip()}'")
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


def _screening_block(screening_pairs: Optional[Sequence[Any]]) -> Optional[str]:
    """The job's screening Q&A as tone/angle guidance only; None when there is none."""
    lines: list[str] = []
    for p in screening_pairs or []:
        q = (p.get("question") if isinstance(p, dict) else getattr(p, "question", "")) or ""
        a = (p.get("answer") if isinstance(p, dict) else getattr(p, "answer", "")) or ""
        if q.strip() and a.strip():
            lines.append(f"Q: {q.strip()}\nA: {a.strip()[:SCREENING_ANSWER_MAX_CHARS]}")
    if not lines:
        return None
    return (
        "SCREENING Q&A (the candidate's own answers for this application — use ONLY to inform the "
        "letter's tone, motivation and angle; do NOT cite any fact from it that the resume does "
        "not support):\n" + "\n\n".join(lines)
    )


def build_draft_prompt(
    *,
    resume_text: str,
    job_description: str,
    company: str,
    prompt_body: str,
    hook: Optional[str],
    screening_pairs: Optional[Sequence[Any]] = None,
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
    screening = _screening_block(screening_pairs)
    if screening:
        parts.append(screening)
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
    screening_pairs: Optional[Sequence[Any]] = None,
) -> CoverLetterResult:
    ctx = dict(resume_text=resume_text, job_description=job_description, company=company)
    results: list[LlmResult] = []
    step_errors: list[str] = []

    # Screening Q&A goes to the draft only: it steers tone, so the review steps don't need (or pay for) it.
    draft = await complete(
        "cover_letter",
        build_draft_prompt(prompt_body=prompt_body, hook=hook, screening_pairs=screening_pairs, **ctx),
        user,
        model_id=model_id,
    )
    results.append(draft)
    producer = draft
    text = _clean(draft.text)
    # Every later step runs on the model that actually answered the draft.
    used_model_id = draft.model_id or model_id

    async def attempt(label: str, prompt: str) -> Optional[LlmResult]:
        """A review step. If every model fails, keep the best letter so far instead of losing it."""
        try:
            res = await complete("cover_letter", prompt, user, model_id=used_model_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("cover letter %s step failed: %s", label, exc)
            step_errors.append(f"{label} step failed: {str(exc)[:200]}")
            return None
        results.append(res)
        return res

    critique = await attempt("critique", build_critique_prompt(draft=text, **ctx))
    if critique is not None and critique.text.strip().rstrip(".").strip().upper() != "OK":
        revised = await attempt(
            "revise", build_revise_prompt(draft=text, feedback=critique.text.strip(), **ctx)
        )
        if revised is not None:
            producer = revised
            text = _clean(revised.text)

    enforce_length = not (prompt_body or "").strip()
    problems = check_cover_letter(text, company, enforce_length=enforce_length)
    if problems:
        retry = await attempt(
            "retry",
            build_revise_prompt(
                draft=text,
                feedback="Fix these problems:\n- " + "\n- ".join(problems),
                **ctx,
            ),
        )
        if retry is not None:
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
        problems=[*step_errors, *problems],
    )
