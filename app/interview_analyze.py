"""Interview Q&A extraction + scoring pipeline.

Triggered as an asyncio background task on interview create/update when a
transcript exists. Persists `InterviewQuestion` docs that the Analyze tab
aggregates over.

Same in-process pattern as resume jobs — survives single-instance lifecycle,
re-extraction needed on backend restart for any in-flight work.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import List

from app.config import get_settings
from app.models import Interview, InterviewQuestion

log = logging.getLogger(__name__)

_MAX_TRANSCRIPT_CHARS = 20_000


def _transcript_hash(transcript: str) -> str:
    return hashlib.sha256(transcript.encode("utf-8")).hexdigest()[:16]


def _strip_fences(s: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip(), flags=re.MULTILINE)


async def extract_and_score(interview: Interview) -> int:
    """Run the full pipeline for one interview. Returns count of questions
    persisted. No-op when transcript empty. Idempotent — replaces all prior
    questions for the interview if transcript hash differs."""
    transcript = (interview.transcript or "").strip()
    if not transcript:
        log.info("interview %s has no transcript; skipping extraction", interview.id)
        return 0

    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY missing; cannot extract for %s", interview.id)
        return 0

    tx_hash = _transcript_hash(transcript)
    existing = await InterviewQuestion.find_one(
        {"interviewId": interview.id, "transcriptVersion": tx_hash}
    )
    if existing is not None:
        log.info("interview %s already extracted at hash=%s; skipping", interview.id, tx_hash)
        return 0

    # Wipe any stale questions for this interview so we don't double-count.
    await InterviewQuestion.find({"interviewId": interview.id}).delete()

    # Cap transcript size to keep prompt bounded.
    snippet = transcript[:_MAX_TRANSCRIPT_CHARS]
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        snippet += "\n…(truncated)"

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    extract_prompt = (
        "Extract every distinct question the interviewer asked the candidate, with the candidate's "
        "spoken answer. Return STRICT JSON only.\n\n"
        'Schema: {"items": [{"question": "<text>", "candidateAnswer": "<text or empty>"}]}\n\n'
        "Rules:\n"
        "- Only interviewer-asked questions. Skip candidate's own questions.\n"
        "- Combine clarifying follow-ups into the parent question if they cover the same topic.\n"
        "- Empty candidateAnswer if the candidate didn't answer or transcript cut off.\n"
        "- Output ONLY the JSON object. No markdown, no commentary.\n\n"
        f"Transcript:\n{snippet}"
    )

    try:
        # Extraction is pure JSON shaping — cheaper model is fine.
        ext_res = await client.responses.create(
            model=settings.EXTRACTION_MODEL,
            input=extract_prompt,
        )
        ext_raw = ext_res.output_text or ""
    except Exception as exc:  # noqa: BLE001
        log.exception("extract failed for interview %s: %s", interview.id, exc)
        return 0

    try:
        parsed = json.loads(_strip_fences(ext_raw))
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            raise ValueError("missing items[]")
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("could not parse extraction JSON for %s: %s", interview.id, exc)
        return 0

    # Score in one batch call to save round-trips.
    qa_for_scoring = [
        {"i": idx, "q": (it.get("question") or "")[:1000], "a": (it.get("candidateAnswer") or "")[:2000]}
        for idx, it in enumerate(items)
        if (it.get("question") or "").strip()
    ]
    if not qa_for_scoring:
        log.info("no questions extracted for %s", interview.id)
        return 0

    score_prompt = (
        "Score each candidate answer 0-10 and write a one-line improvement tip per question. "
        "Use the rubric: 0-3 weak (missed concepts, low depth); 4-6 mixed (partial); 7-8 solid; "
        "9-10 strong (deep, structured, senior-level).\n\n"
        f"Stage: {interview.stage or 'unknown'}\n"
        f"Position: {interview.appliedPosition or 'unknown'}\n"
        f"Company: {interview.companyName or 'unknown'}\n\n"
        'Return STRICT JSON: {"scores": [{"i": <idx>, "score": <0-10>, "rationale": "<one-line>", "tip": "<one-line>"}]}\n\n'
        f"Q&A:\n{json.dumps(qa_for_scoring, ensure_ascii=False)}"
    )

    try:
        sc_res = await client.responses.create(
            model=settings.RESUME_OPENAI_MODEL,
            input=score_prompt,
        )
        sc_raw = sc_res.output_text or ""
        sc_parsed = json.loads(_strip_fences(sc_raw))
        scores_by_i = {
            int(s["i"]): s for s in (sc_parsed.get("scores") or []) if isinstance(s, dict) and "i" in s
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("score parsing failed for %s: %s", interview.id, exc)
        scores_by_i = {}

    docs: List[InterviewQuestion] = []
    for idx, it in enumerate(items):
        q = (it.get("question") or "").strip()
        if not q:
            continue
        a = (it.get("candidateAnswer") or "").strip()
        s = scores_by_i.get(idx, {})
        docs.append(
            InterviewQuestion(
                interviewId=interview.id,
                userId=interview.createdBy,
                accountId=interview.accountId if not isinstance(interview.accountId, str) else None,
                stage=interview.stage,
                companyName=interview.companyName,
                question=q[:2000],
                candidateAnswer=a[:5000],
                score=int(s["score"]) if isinstance(s.get("score"), (int, float)) else None,
                scoreRationale=(s.get("rationale") or None),
                improvementTip=(s.get("tip") or None),
                transcriptVersion=tx_hash,
            )
        )

    if docs:
        await InterviewQuestion.insert_many(docs)
        log.info("extracted %d questions for interview %s", len(docs), interview.id)
    return len(docs)
