"""Seeded JD library and document registration (no DB)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from collections import Counter
from types import SimpleNamespace

from app import models as models_pkg
from app.db import DOCUMENT_MODELS
from app.models import EvalJobDescription, ResumePromptEval
from app.models.prompt_eval import EVAL_ROLES, MAX_JDS_PER_RUN
from app.prompt_eval.seed_jds import SEED_JDS, seed_eval_jds, seed_key, seeds_for_role

ORIGINAL_ROLES = ("fullstack", "frontend", "backend", "ai", "ml", "llm", "data", "test")


def test_seed_has_at_least_five_jds_per_role_in_role_order():
    assert len(EVAL_ROLES) == 10 and EVAL_ROLES[-2:] == ("senior_swe", "staff_swe")
    per_role = Counter(s.role for s in SEED_JDS)
    assert set(per_role) == set(EVAL_ROLES)
    assert all(n >= 5 for n in per_role.values()), per_role
    assert [s.role for s in SEED_JDS] == sorted((s.role for s in SEED_JDS), key=EVAL_ROLES.index)
    assert len({s.key for s in SEED_JDS}) == len(SEED_JDS), "seed keys must be unique"
    assert len({s.title for s in SEED_JDS}) == len(SEED_JDS), "titles must be unique"
    assert MAX_JDS_PER_RUN >= len(SEED_JDS), "the whole library must fit in one run"


def test_original_roles_keep_their_short_seed_key_so_existing_databases_do_not_duplicate():
    for role in ORIGINAL_ROLES:
        first = seeds_for_role(role)[0]
        assert first.key == seed_key(role) == f"seed:{role}"
    for s in SEED_JDS:
        assert s.key.startswith(f"seed:{s.role}")


def test_seed_texts_are_realistic_postings():
    for seed in SEED_JDS:
        assert len(seed.text) >= 1500, seed.key
        assert "years" in seed.text, seed.key
        assert "Nice to have" in seed.text, seed.key
        assert any(h in seed.text for h in ("Requirements", "What we're looking for", "Responsibilities")), seed.key
        # Every seed carries a location / work-authorization line so the judge's ignore-location rule is exercised.
        assert "Location:" in seed.text, seed.key
        assert seed.text.startswith(seed.title.split(" (")[0]) and seed.company in seed.text, seed.key


class FakeColl:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.calls = []

    async def update_one(self, filt, update, upsert=False):
        self.calls.append((filt, update, upsert))
        key = filt["seedKey"]
        if key in self.existing:
            return SimpleNamespace(upserted_id=None)
        self.existing.add(key)
        return SimpleNamespace(upserted_id=key)


async def test_seed_is_an_idempotent_set_on_insert_upsert():
    coll = FakeColl()
    assert await seed_eval_jds(coll) == len(SEED_JDS)
    assert await seed_eval_jds(coll) == 0
    for filt, update, upsert in coll.calls:
        assert upsert is True
        assert set(update) == {"$setOnInsert"}, "must never overwrite an existing (possibly edited) seed"
        assert filt == {"seedKey": update["$setOnInsert"]["seedKey"]}
        assert update["$setOnInsert"]["archived"] is False
        assert update["$setOnInsert"]["createdBy"] is None


async def test_seed_only_adds_the_postings_an_older_database_lacks():
    coll = FakeColl(existing={seed_key(role) for role in ORIGINAL_ROLES})
    assert await seed_eval_jds(coll) == len(SEED_JDS) - len(ORIGINAL_ROLES)


def test_documents_are_registered():
    assert EvalJobDescription in DOCUMENT_MODELS and ResumePromptEval in DOCUMENT_MODELS
    assert "EvalJobDescription" in models_pkg.__all__ and "ResumePromptEval" in models_pkg.__all__
    assert EvalJobDescription.Settings.name == "eval_job_descriptions"
    assert ResumePromptEval.Settings.name == "resume_prompt_evals"
