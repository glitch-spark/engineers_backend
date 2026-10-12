"""Built-in sample job descriptions, five per role, inserted once on boot.

They are realistic enough to exercise the judge: each has required vs. nice-to-have sections, a years
figure, a core stack, and a location / work-authorization line (which the judge must ignore). Inserted
with `$setOnInsert` under a stable `seedKey`, so a user's edits or archiving are never overwritten.
One module per role under this package; `SEED_JDS` is the concatenation in role order.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.models.prompt_eval import EVAL_ROLES, EvalJobDescription
from app.prompt_eval.seed_jds._base import SeedJd, key_for
from app.prompt_eval.seed_jds import (
    ai,
    backend,
    data,
    frontend,
    fullstack,
    llm,
    ml,
    senior_swe,
    staff_swe,
    test,
)

__all__ = ["SeedJd", "SEED_JDS", "seed_key", "key_for", "seed_eval_jds", "seed_by_role", "seeds_for_role"]

_BY_ROLE: dict[str, tuple[SeedJd, ...]] = {
    "fullstack": fullstack.JDS,
    "frontend": frontend.JDS,
    "backend": backend.JDS,
    "ai": ai.JDS,
    "ml": ml.JDS,
    "llm": llm.JDS,
    "data": data.JDS,
    "test": test.JDS,
    "senior_swe": senior_swe.JDS,
    "staff_swe": staff_swe.JDS,
}

SEED_JDS: tuple[SeedJd, ...] = tuple(jd for role in EVAL_ROLES for jd in _BY_ROLE[role])


def seed_key(role: str) -> str:
    """The key of a role's first (original) seed posting."""
    return f"seed:{role}"


def seeds_for_role(role: str) -> tuple[SeedJd, ...]:
    return _BY_ROLE.get(role, ())


def seed_by_role(role: str) -> Optional[SeedJd]:
    jds = seeds_for_role(role)
    return jds[0] if jds else None


async def seed_eval_jds(coll=None) -> int:
    """Insert any seed JD that is not there yet; never overwrite. Returns the number inserted."""
    if coll is None:
        coll = EvalJobDescription.get_motor_collection()
    inserted = 0
    now = datetime.utcnow()
    for seed in SEED_JDS:
        result = await coll.update_one(
            {"seedKey": seed.key},
            {
                "$setOnInsert": {
                    "role": seed.role,
                    "title": seed.title,
                    "company": seed.company,
                    "text": seed.text,
                    "seedKey": seed.key,
                    "createdBy": None,
                    "archived": False,
                    "archivedAt": None,
                    "createdAt": now,
                    "updatedAt": now,
                }
            },
            upsert=True,
        )
        if getattr(result, "upserted_id", None) is not None:
            inserted += 1
    return inserted
