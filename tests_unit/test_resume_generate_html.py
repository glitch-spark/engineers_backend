"""generate_resume_html: the LLM half of the pipeline without PDF/S3 (no DB, no model)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import resume_service
from app.llm_router import LlmResult

TEMPLATE = "<html><head><style>.x{}</style></head><body><p>OLD</p></body></html>"


def account(template=TEMPLATE):
    return SimpleNamespace(styleTemplate=template, name="p", id="a", pageFormat="A4")


async def test_generate_resume_html_returns_shell_plus_body_and_the_llm_result(monkeypatch):
    seen = {}

    async def fake_output(prompt, user, template, model_id=None):
        seen.update(prompt=prompt, model_id=model_id)
        return LlmResult(
            text="<body><p>NEW</p></body>", provider="anthropic", model="claude-sonnet-5-5",
            usage={"input": 0, "output": 0}, fallback_used=False,
            model_id="anthropic:claude-sonnet-5-5", cost_usd=0.0, cached=True,
        )

    monkeypatch.setattr(resume_service, "_resume_llm_output", fake_output)
    html, result = await resume_service.generate_resume_html(
        account(), "JD text", prompt_body="Be concise", model_id="anthropic:claude-sonnet-5-5",
    )
    assert "<style>.x{}</style>" in html and "NEW" in html and "OLD" not in html
    assert result.cached is True and result.model_id == "anthropic:claude-sonnet-5-5"
    assert "JD text" in seen["prompt"] and "Be concise" in seen["prompt"]
    assert seen["model_id"] == "anthropic:claude-sonnet-5-5"


async def test_generate_resume_html_requires_a_template():
    with pytest.raises(HTTPException) as exc:
        await resume_service.generate_resume_html(account(template="  "), "JD")
    assert exc.value.status_code == 400


def test_plain_text_from_html_collapses_blank_lines():
    text = resume_service.plain_text_from_html("<body><h1>A</h1>\n\n\n\n<p>B</p><p>C</p></body>")
    assert "\n\n\n" not in text
    assert text.startswith("A") and "B" in text and text.endswith("C")
