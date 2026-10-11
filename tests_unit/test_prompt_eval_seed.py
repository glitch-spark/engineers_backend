"""Seeded JD library and document registration (no DB)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

from app import models as models_pkg
from app.db import DOCUMENT_MODELS
from app.models import EvalJobDescription, ResumePromptEval
from app.models.prompt_eval import EVAL_ROLES, MAX_JDS_PER_RUN
from app.prompt_eval.seed_jds import SEED_JDS, seed_eval_jds, seed_key


def test_seed_has_exactly_one_jd_per_role():
    assert len(SEED_JDS) == len(EVAL_ROLES) == 8
    assert {s.role for s in SEED_JDS} == set(EVAL_ROLES)
    assert len({s.title for s in SEED_JDS}) == 8
    assert MAX_JDS_PER_RUN >= len(SEED_JDS)


def test_seed_texts_are_realistic_postings():
    for seed in SEED_JDS:
        assert len(seed.text) >= 1500, seed.role
        assert "years" in seed.text, seed.role
        assert "Nice to have" in seed.text, seed.role
        assert "Requirements" in seed.text or "What we're looking for" in seed.text, seed.role
        # Every seed carries a location / work-authorization line so the judge's ignore-location rule is exercised.
        assert "Location:" in seed.text, seed.role
        assert seed.text.startswith(seed.title.split(" (")[0]) and seed.company in seed.text


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
    assert await seed_eval_jds(coll) == 8
    assert await seed_eval_jds(coll) == 0
    for filt, update, upsert in coll.calls:
        assert upsert is True
        assert set(update) == {"$setOnInsert"}, "must never overwrite an existing (possibly edited) seed"
        assert filt == {"seedKey": seed_key(update["$setOnInsert"]["role"])}
        assert update["$setOnInsert"]["archived"] is False
        assert update["$setOnInsert"]["createdBy"] is None


async def test_seed_skips_rows_that_already_exist():
    coll = FakeColl(existing={seed_key("ai"), seed_key("data")})
    assert await seed_eval_jds(coll) == 6


def test_documents_are_registered():
    assert EvalJobDescription in DOCUMENT_MODELS and ResumePromptEval in DOCUMENT_MODELS
    assert "EvalJobDescription" in models_pkg.__all__ and "ResumePromptEval" in models_pkg.__all__
    assert EvalJobDescription.Settings.name == "eval_job_descriptions"
    assert ResumePromptEval.Settings.name == "resume_prompt_evals"
