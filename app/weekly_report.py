"""AI extraction for weekly plans: free-text plan/follow-up -> structured
metrics (target vs actual). Admin-triggered batch via the progress-report
endpoint. Cheap model (EXTRACTION_MODEL) — purely JSON shaping, no reasoning.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from openai import AsyncOpenAI

from app.config import get_settings
from app.models.weekly_plan import CORE_METRICS, WeeklyMetric

log = logging.getLogger(__name__)


_CORE_KEYS = {m["key"]: m["label"] for m in CORE_METRICS}

_PROMPT = (
    "Extract weekly job-search progress metrics from a candidate's free-text "
    "weekly plan and follow-up.\n"
    "\n"
    "TARGET numbers come from the PLAN text (what they intend to do this week).\n"
    "ACTUAL numbers come from the FOLLOW-UP text (what actually got done).\n"
    "\n"
    "Always include these core metrics (use 0 when a number isn't stated). "
    "Candidates phrase them MANY ways — map aggressively by meaning, not "
    "exact words:\n"
    "- bids: job applies, applications, 'bids', 'job bids', 'total bids', "
    "submissions. If a 'Total Bids: N' (or similar total) is given, use that "
    "N — do NOT also sum per-country / per-profile sub-lines.\n"
    "- interviews: ALWAYS emit key='interviews' with target=0 and actual=0. "
    "Do NOT parse interview counts from the text — the app fills those from "
    "the interview board (stage badges) after extraction.\n"
    "- resume: resume or profile work — updates, maintenance, or NEW profiles "
    "created (e.g. 'Brazil: two profiles, Argentina: 1 profile' => count the "
    "profiles).\n"
    "- outreach: founder / co-founder / recruiter / networking activity. "
    "Capture it EITHER as a count OR as hours, whichever the text states — "
    "ONE metric, do not split. If a number of messages/contacts/connects is "
    "given, unit='count' (that number, daily x5 if per-day). If only time is "
    "given (e.g. 'looking for co-founders 1hr/day'), unit='hours' (hours/day "
    "x5). Prefer count when both appear.\n"
    "Also add any OTHER clearly-quantified goal the candidate mentions "
    "(e.g. 'finish 2 projects', '10 LeetCode', 'review 5 recordings') as "
    "extra metrics with a short label and a sensible key (lowercase, no "
    "spaces).\n"
    "\n"
    "Infer integers from natural language ('a few'~3, 'a couple'~2) only when "
    "obvious; otherwise 0. Never invent goals not implied by the text.\n"
    "\n"
    "DAILY RATES: when a number is stated per-day (e.g. '30 connects/day', "
    "'daily 30', '50/day'), the WEEKLY total = daily rate x 5. The work week "
    "is Monday-Friday (5 days), NOT 7. Example: 'LinkedIn daily 30 connects' "
    "=> weekly target 150.\n"
    "\n"
    "UNITS — each metric has a 'unit': 'count' or 'hours'.\n"
    "- COUNT (default): discrete things — bids, interviews, profiles, "
    "outreaches, messages. The 4 core metrics are ALWAYS count.\n"
    "- HOURS: time commitments stated in hours or minutes (e.g. "
    "'1 hour/day', '30-40 min daily', '2h reviewing'). Convert to weekly "
    "HOURS: hours_per_day x 5 (minutes/60 first; for a range use the "
    "midpoint). Example: '30-40 min daily review' => ~3 hours/week "
    "(0.58h x 5 ~= 3). Emit time goals as their OWN metric with unit "
    "'hours' — do NOT fold them into a count core metric.\n"
    "\n"
    'Return STRICT JSON only, no prose, no code fences:\n'
    '{"metrics":[{"key":"bids","label":"Job applies","target":<int>,"actual":<int>,"unit":"count"}, ...]}\n'
)


def _strip_fences(s: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", (s or "").strip(), flags=re.MULTILINE).strip()


async def extract_metrics(plan_text: str, followup_text: str) -> list[WeeklyMetric]:
    """Parse plan + follow-up free text into WeeklyMetric list. On any
    failure returns the 4 core metrics zeroed, so the report never breaks."""
    settings = get_settings()
    fallback = [WeeklyMetric(key=k, label=lbl, target=0, actual=0) for k, lbl in _CORE_KEYS.items()]

    if not settings.OPENAI_API_KEY:
        return fallback
    plan_text = (plan_text or "").strip()
    followup_text = (followup_text or "").strip()
    if not plan_text and not followup_text:
        return fallback

    prompt = (
        _PROMPT
        + "\n=== PLAN (targets) ===\n" + (plan_text or "(none)")
        + "\n\n=== FOLLOW-UP (actuals) ===\n" + (followup_text or "(none)")
    )
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    try:
        resp = await client.responses.create(model=settings.EXTRACTION_MODEL, input=prompt)
        parsed = json.loads(_strip_fences(resp.output_text or ""))
        rows = parsed.get("metrics") if isinstance(parsed, dict) else None
        if not isinstance(rows, list):
            return fallback
    except Exception as exc:  # noqa: BLE001
        log.warning("weekly metric extraction failed: %s", exc)
        return fallback

    out: dict[str, WeeklyMetric] = {}
    # Seed cores so they always appear (extraction may omit some). Cores are
    # always count.
    for k, lbl in _CORE_KEYS.items():
        out[k] = WeeklyMetric(key=k, label=lbl, target=0, actual=0, unit="count")
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = str(r.get("key") or "").strip().lower().replace(" ", "_")
        label = str(r.get("label") or "").strip()
        if not key or not label:
            continue
        try:
            target = max(0, int(r.get("target") or 0))
            actual = max(0, int(r.get("actual") or 0))
        except (TypeError, ValueError):
            target, actual = 0, 0
        # bids/interviews/resume are always count; outreach + extras honor
        # the model's unit (outreach can be count OR hours).
        forced_count = key in ("bids", "interviews", "resume")
        unit = "count" if forced_count else ("hours" if str(r.get("unit") or "").strip().lower() == "hours" else "count")
        out[key] = WeeklyMetric(key=key, label=_CORE_KEYS.get(key, label), target=target, actual=actual, unit=unit)
    # Interview counts come from the interview board (stage badges), never from text.
    out["interviews"] = WeeklyMetric(key="interviews", label=_CORE_KEYS["interviews"], target=0, actual=0, unit="count")
    # Preserve core order first, then extras.
    ordered = [out[k] for k in _CORE_KEYS if k in out]
    ordered += [v for k, v in out.items() if k not in _CORE_KEYS]
    return ordered
