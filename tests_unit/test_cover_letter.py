"""Cover letter pipeline: draft, critique, revise, deterministic checks (router stubbed)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest

from app import cover_letter as cl
from app.llm_router import LlmResult

OPUS = "anthropic:claude-opus-5-5"
FABLE = "anthropic:claude-fable-5-1"


def letter(company="Acme", words=200, extra=""):
    return f"Dear {company} team, " + ("skill " * words) + extra


def script(monkeypatch, texts, *, model_id=OPUS, cost=0.01, fallback_used=False):
    """Patch cover_letter.complete; each call pops the next text. Returns the recorded calls."""
    calls = []
    queue = list(texts)

    async def fake(task, prompt, user, *, model_id=None, validate=None):
        calls.append({"task": task, "prompt": prompt, "model_id": model_id})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return LlmResult(
            text=item,
            provider="anthropic",
            model="claude-opus-5-5",
            usage={"input": 100, "output": 50},
            fallback_used=fallback_used,
            fallback_reason="requested model failed" if fallback_used else None,
            model_id=model_id_used,
            cost_usd=cost,
        )

    model_id_used = model_id
    monkeypatch.setattr(cl, "complete", fake)
    return calls


async def generate(**over):
    kwargs = dict(
        resume_text="Jane: 8 years Python at Initech.",
        job_description="Senior Python engineer at Acme.",
        company="Acme",
        prompt_body="",
        user=None,
        model_id=OPUS,
    )
    kwargs.update(over)
    return await cl.generate_cover_letter(**kwargs)


# --- check_cover_letter -----------------------------------------------------


@pytest.mark.parametrize("bad", ["[Your Name]", "{{company}}", "<insert reason>"])
def test_check_flags_placeholders(bad):
    problems = cl.check_cover_letter(letter(extra=bad), "Acme", enforce_length=True)
    assert any("placeholder" in p for p in problems)


def test_check_flags_missing_company_case_insensitive():
    assert cl.check_cover_letter(letter(company="Other"), "Acme", enforce_length=True)
    assert cl.check_cover_letter(letter(company="ACME"), "acme", enforce_length=True) == []


def test_check_word_range_only_when_enforced():
    short = letter(words=5)
    assert any("words" in p for p in cl.check_cover_letter(short, "Acme", enforce_length=True))
    assert cl.check_cover_letter(short, "Acme", enforce_length=False) == []
    assert any("words" in p for p in cl.check_cover_letter(letter(words=600), "Acme", enforce_length=True))


def test_check_tolerates_legal_suffixes_in_company_name():
    assert cl.check_cover_letter(letter(company="Acme"), "Acme, Inc.", enforce_length=True) == []
    assert cl.check_cover_letter(letter(company="Acme"), "ACME LLC", enforce_length=True) == []
    assert cl.check_cover_letter(letter(company="Other"), "Acme, Inc.", enforce_length=True)


@pytest.mark.parametrize("fallback_name", ["boards.greenhouse.io", "jobs.lever.co", "www.acme.com"])
def test_check_skips_company_check_for_hostname_fallbacks(fallback_name):
    assert cl.check_cover_letter(letter(company="Acme"), fallback_name, enforce_length=True) == []


def test_check_does_not_flag_angle_operators_as_placeholders():
    text = letter(extra="I cut latency A <-> B and handled <3 years of backlog.")
    assert cl.check_cover_letter(text, "Acme", enforce_length=True) == []


# --- pipeline ---------------------------------------------------------------


async def test_critique_ok_skips_revise(monkeypatch):
    calls = script(monkeypatch, [letter(), "OK"])
    res = await generate()
    assert len(calls) == 2 and [c["task"] for c in calls] == ["cover_letter", "cover_letter"]
    assert res.text == letter().strip() and res.problems == []


async def test_critique_issues_trigger_revise(monkeypatch):
    revised = letter(extra="Tightened and specific.")
    calls = script(monkeypatch, [letter(), "- too generic: cite Kafka work", revised])
    res = await generate()
    assert len(calls) == 3
    assert "too generic: cite Kafka work" in calls[2]["prompt"]
    assert res.text == revised.strip()


async def test_deterministic_failure_retries_once_then_returns(monkeypatch):
    bad = letter(extra="Sincerely, [Your Name]")
    calls = script(monkeypatch, [letter(), "- fix tone", bad, bad])
    res = await generate()
    assert len(calls) == 4  # draft, critique, revise, one deterministic retry
    assert "placeholder" in calls[3]["prompt"]
    assert res.problems and "[Your Name]" in res.text  # returned, not raised


async def test_critique_failure_keeps_the_draft(monkeypatch):
    calls = script(monkeypatch, [letter(), RuntimeError("529 overloaded")])
    res = await generate()
    assert len(calls) == 2 and res.text == letter().strip()
    assert any("critique" in p for p in res.problems)


async def test_revise_failure_keeps_the_draft(monkeypatch):
    script(monkeypatch, [letter(), "- too generic", RuntimeError("529 overloaded")])
    res = await generate()
    assert res.text == letter().strip() and any("revise" in p for p in res.problems)


async def test_retry_failure_keeps_the_pre_retry_text(monkeypatch):
    bad = letter(extra="Sincerely, [Your Name]")
    script(monkeypatch, [letter(), "- fix tone", bad, RuntimeError("529 overloaded")])
    res = await generate()
    assert "[Your Name]" in res.text
    assert any("retry" in p for p in res.problems) and any("placeholder" in p for p in res.problems)


async def test_custom_prompt_disables_length_enforcement(monkeypatch):
    calls = script(monkeypatch, [letter(words=20), "OK"])
    res = await generate(prompt_body="Two sentences only.")
    assert len(calls) == 2 and res.problems == []


async def test_all_steps_use_the_model_that_answered_the_draft(monkeypatch):
    calls = script(monkeypatch, [letter(), "OK"], model_id=OPUS, fallback_used=True)
    res = await generate(model_id=FABLE)
    assert calls[0]["model_id"] == FABLE
    assert calls[1]["model_id"] == OPUS  # the model that actually answered the draft
    assert res.model_id == OPUS and res.fallback_used is True
    assert res.fallback_reason == "requested model failed"


async def test_cost_is_summed(monkeypatch):
    script(monkeypatch, [letter(), "- fix", letter(extra="v2")], cost=0.01)
    res = await generate()
    assert res.cost_usd == pytest.approx(0.03)


async def test_cost_is_none_when_prices_unknown(monkeypatch):
    script(monkeypatch, [letter(), "OK"], cost=None)
    assert (await generate()).cost_usd is None


async def test_hook_appears_in_draft_prompt(monkeypatch):
    calls = script(monkeypatch, [letter(), "OK"])
    await generate(hook="I mentored three interns on your stack")
    assert "I mentored three interns on your stack" in calls[0]["prompt"]


async def test_long_dashes_normalized_and_fences_stripped(monkeypatch):
    script(monkeypatch, ["```\n" + letter(extra="Fast — reliable") + "\n```", "OK"])
    res = await generate()
    assert "—" not in res.text and "Fast - reliable" in res.text and "```" not in res.text
