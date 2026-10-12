"""The judge: one fresh-context LLM call that scores a generated resume against a job description.

Two independent 0-100 scores: job match (location and work authorization ignored) and believability
(does it read like a real engineer's own resume). The answer is a single JSON object, parsed
leniently; `parse_verdict` doubles as the router's `validate` so a model that returns garbage is
skipped for the next one in the chain. Verdicts are cached in Redis under the prompt version + model
lane + full prompt, so the same resume/JD pair is never judged twice by the same model.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app import redis_cache
from app.llm_router import LlmResult, _try_parse_jsonish, complete
from app.model_catalog import cache_lane, fallback_chain
from app.models import UserExtra
from app.models.prompt_eval import JudgeVerdict
from app.resume_service import trim_jd

log = logging.getLogger(__name__)

# Bump when the rubric or JSON shape changes: cached verdicts from the old prompt are then ignored.
JUDGE_PROMPT_VERSION = "resume-judge-v1"
MAX_RESUME_CHARS = 20_000
MAX_LIST_ITEMS = 10
MAX_ITEM_CHARS = 300
MAX_SUMMARY_CHARS = 600

_PROMPT = """You are a senior technical recruiter and hiring manager reviewing ONE candidate resume against ONE job
description for a {role} role. Reply with a single JSON object only, no prose, no markdown fences.

Score two independent things.

1. matchScore (0-100): how well the resume's skills, experience depth, seniority, domain and
   accomplishments fit what this job asks for.
   - IGNORE location, geography, time zone, relocation, remote/hybrid/onsite policy, citizenship,
     visa and work-authorization requirements entirely. They must not raise or lower the score and
     must not appear in missingRequirements.
   - Weigh required qualifications far above nice-to-haves. Years of experience and the core stack
     matter most; adjacent technologies count partially.
   - 90-100: would be shortlisted immediately; 70-89: strong, a few gaps; 50-69: partial fit;
     below 50: wrong role or level.

2. believabilityScore (0-100): does this read like a real engineer's own resume rather than a
   document rewritten to match the posting? Judge:
   - Timeline consistency: dates, tenure and seniority progress plausibly; titles fit tenure.
   - Plausible scope: metrics and claims (users, latency, revenue, team size) fit the company and
     role; no outsized or round-number brag that nothing else supports.
   - Keyword stuffing: skills or JD phrases pasted without evidence in the bullets; the same term
     repeated unnaturally; a skills list far wider than the experience shows.
   - Fabrication signals: every job happens to use exactly the posting's stack; generic bullets that
     could describe any job; contradictions between summary, bullets and skills.
   - Natural phrasing: concrete, varied, human wording vs. template-like or marketing language.
   - 90-100: nothing suspicious; 70-89: minor polish signs; 50-69: clearly tailored, some doubtful
     claims; below 50: reads as fabricated or stuffed.

Do not reward length. Do not penalize the candidate for missing contact details or formatting.

JSON shape (all keys required; lists may be empty; each list at most 8 short items):
{{
  "matchScore": 0,
  "believabilityScore": 0,
  "matchNotes": ["why the match score is what it is"],
  "believabilityNotes": ["specific signals that raised or lowered believability"],
  "missingRequirements": ["required items the resume does not show (never location or work authorization)"],
  "redFlags": ["claims or patterns a careful reviewer would question"],
  "strengths": ["the strongest evidence for this role"],
  "summary": "two sentences: fit and credibility"
}}

Job description:
<<<
{jd}
>>>

Candidate resume (plain text):
<<<
{resume}
>>>"""


def build_judge_prompt(*, resume_text: str, job_description: str, role: str) -> str:
    return _PROMPT.format(
        role=(role or "software engineering").strip() or "software engineering",
        jd=trim_jd(job_description or ""),
        resume=(resume_text or "")[:MAX_RESUME_CHARS],
    )


# ---------- parsing ----------

_SCORE_ALIASES = {
    "matchScore": ("matchScore", "match_score", "match", "fit", "fitScore"),
    "believabilityScore": ("believabilityScore", "believability_score", "believability", "credibility", "credibilityScore"),
}
_LIST_ALIASES = {
    "matchNotes": ("matchNotes", "match_notes"),
    "believabilityNotes": ("believabilityNotes", "believability_notes"),
    "missingRequirements": ("missingRequirements", "missing_requirements", "missing", "gaps"),
    "redFlags": ("redFlags", "red_flags", "concerns"),
    "strengths": ("strengths", "strength"),
}


def _first(obj: dict, names: tuple[str, ...]) -> Any:
    for name in names:
        if name in obj:
            return obj[name]
    return None


def _score(v: Any) -> Optional[int]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        n = float(v)
    elif isinstance(v, str):
        s = v.strip().rstrip("%").strip()
        try:
            n = float(s)
        except ValueError:
            return None
    else:
        return None
    return max(0, min(100, int(round(n))))


def _text(v: Any) -> str:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        for key in ("text", "note", "item", "reason"):
            if isinstance(v.get(key), str):
                return v[key].strip()
        return next((x.strip() for x in v.values() if isinstance(x, str) and x.strip()), "")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return ""


def _str_list(v: Any) -> list[str]:
    items = [v] if isinstance(v, (str, dict)) else v if isinstance(v, list) else []
    out = []
    for item in items:
        text = _text(item)
        if text:
            out.append(text[:MAX_ITEM_CHARS])
        if len(out) >= MAX_LIST_ITEMS:
            break
    return out


def parse_verdict(raw: str) -> JudgeVerdict:
    """Lenient parse of the judge's answer. Raises ValueError when it is not a JSON object or either
    score is missing, which the router treats as a failed attempt."""
    obj = _try_parse_jsonish(raw or "")
    if not isinstance(obj, dict):
        raise ValueError("judge output is not a JSON object")
    match = _score(_first(obj, _SCORE_ALIASES["matchScore"]))
    believability = _score(_first(obj, _SCORE_ALIASES["believabilityScore"]))
    if match is None or believability is None:
        raise ValueError("judge output lacks matchScore/believabilityScore")
    lists = {key: _str_list(_first(obj, names)) for key, names in _LIST_ALIASES.items()}
    return JudgeVerdict(
        matchScore=match,
        believabilityScore=believability,
        summary=_text(obj.get("summary"))[:MAX_SUMMARY_CHARS],
        **lists,
    )


def _validate(raw: str) -> None:
    parse_verdict(raw)


# ---------- entry point ----------


def _cache_key(prompt: str, lane: str) -> str:
    return redis_cache.cache_key("judge", JUDGE_PROMPT_VERSION, lane, prompt)


async def judge_resume(
    *,
    resume_text: str,
    job_description: str,
    role: str,
    user: Optional[UserExtra],
    model_id: Optional[str],
) -> tuple[JudgeVerdict, LlmResult]:
    """Score one resume against one JD on the judge model (fresh context). Cached per model lane."""
    prompt = build_judge_prompt(resume_text=resume_text, job_description=job_description, role=role)
    chain = fallback_chain("judge", model_id, user)
    preferred = chain[0] if chain else None
    if preferred is not None:
        hit = await redis_cache.get_json(_cache_key(prompt, cache_lane(preferred, "judge", user)))
        if hit and isinstance(hit.get("verdict"), dict):
            try:
                verdict = JudgeVerdict(**hit["verdict"])
            except Exception as exc:  # noqa: BLE001 — an old-shape value is just a miss
                log.warning("cached judge verdict ignored: %s", exc)
            else:
                log.info("judge redis HIT model=%s", hit.get("model"))
                return verdict, LlmResult(
                    text="", provider=preferred.provider, model=hit.get("model") or preferred.api_model,
                    usage={"input": 0, "output": 0}, fallback_used=False, model_id=preferred.id,
                    cost_usd=0.0, cached=True,
                )

    result = await complete("judge", prompt, user, model_id=model_id, validate=_validate)
    verdict = parse_verdict(result.text)

    answered = next((s for s in chain if s.id == result.model_id), None)
    if answered is not None:
        await redis_cache.set_json(
            _cache_key(prompt, cache_lane(answered, "judge", user)),
            {
                "verdict": verdict.model_dump(),
                "model": result.model,
                "model_id": result.model_id,
                "provider": result.provider,
                "fallback": result.fallback_used,
            },
        )
    return verdict, result
