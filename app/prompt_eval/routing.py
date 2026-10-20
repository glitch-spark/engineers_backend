"""Which of a profile's resume units each eval job description gets, the way Job Applies picks one.

Keyword based and free: the job's roles come from its title and skills (`job_roles`), and the unit whose role marks
overlap them most wins (`suggest_unit`), else the default unit. Skills come from the JD's stored rules-judge reading
when it is current, otherwise from the rule-based extractor (no model call). A hand-picked unit overrides the pick.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional, Sequence

from app.job_apply.extract import rules_extract
from app.job_apply.prompt_suggest import job_roles, suggest_unit
from app.job_apply.schema import Term
from app.models.account import ResumePrompt
from app.models.prompt_eval import UnitPick
from app.prompt_eval.rules_judge import reading_key
from app.resume_prompts import prompts_view

NO_ROLE_REASON = "no role keywords found; default unit"


@dataclass(frozen=True)
class Route:
    jdId: str
    unitId: str
    unitName: str
    pick: UnitPick
    reason: str


def jd_terms(jd: Any, run_date: date) -> list[Term]:
    if getattr(jd, "extraction", None) is not None and getattr(jd, "extractionKey", None) == reading_key(jd.text):
        return list(jd.extraction.terms)
    return list(rules_extract(jd.text or "", run_date).terms)


def route_jds(
    account: Any, jds: Sequence[Any], overrides: Optional[dict[str, str]], run_date: date,
) -> tuple[list[Route], list[ResumePrompt], str]:
    """(one route per JD in `jds` order, the profile's units, the default unit id). Overrides naming a unit the
    profile no longer has are ignored."""
    units, default_id = prompts_view(account)
    by_id = {u.id: u for u in units}
    overrides = overrides or {}
    routes: list[Route] = []
    for jd in jds:
        key = str(jd.id)
        chosen = overrides.get(key)
        if chosen in by_id:
            routes.append(Route(key, chosen, by_id[chosen].name, "manual", "picked by hand"))
            continue
        match = job_roles(jd.title or "", jd_terms(jd, run_date))
        unit_id = suggest_unit(units, default_id, match.roles)
        reason = match.reason or NO_ROLE_REASON
        if match.roles and unit_id == default_id and not set(by_id[unit_id].roles or []) & set(match.roles):
            reason += " · no unit is marked for these roles; default unit"
        routes.append(Route(key, unit_id, by_id[unit_id].name, "auto", reason))
    return routes, units, default_id
