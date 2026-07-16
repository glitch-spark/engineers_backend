"""CSS isolation: LLM never sees styles; uploaded CSS is reattached by construction."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.html_sanitize import (  # noqa: E402
    body_html_for_llm,
    body_styles_vs_head,
    strip_style_tags,
    wrap_body_for_llm,
)
from app.resume_service import (  # noqa: E402
    _build_resume_prompt,
    _finalize_resume_html,
    _normalize_dashes_body_only,
    _validate_llm_html,
)


HEAD_STYLE_TEMPLATE = (
    "<!DOCTYPE html><html><head><style>.x{color:red}</style></head>"
    '<body class="page"><h1>OLD NAME</h1><p>old — summary</p></body></html>'
)

BODY_STYLE_TEMPLATE = (
    "<!DOCTYPE html><html><head></head>"
    '<body class="page"><style>.y{color:blue}</style>'
    "<h1>OLD NAME</h1><p>old summary</p></body></html>"
)

EM_DASH_IN_CSS_TEMPLATE = (
    "<!DOCTYPE html><html><head>"
    "<style>.x{content:'A — B'}</style></head>"
    "<body><h1>OLD — NAME</h1></body></html>"
)


def test_body_html_for_llm_strips_head_and_body_styles():
    assert "<style>" not in body_html_for_llm(HEAD_STYLE_TEMPLATE)
    assert ".x{color:red}" not in body_html_for_llm(HEAD_STYLE_TEMPLATE)
    assert "OLD NAME" in body_html_for_llm(HEAD_STYLE_TEMPLATE)

    assert "<style>" not in body_html_for_llm(BODY_STYLE_TEMPLATE)
    assert ".y{color:blue}" not in body_html_for_llm(BODY_STYLE_TEMPLATE)
    assert "OLD NAME" in body_html_for_llm(BODY_STYLE_TEMPLATE)


def test_prompt_contains_no_style_when_using_llm_body():
    llm_body = wrap_body_for_llm(body_html_for_llm(HEAD_STYLE_TEMPLATE))
    assert "<style>" not in llm_body
    assert ".x{color:red}" not in llm_body

    prompt = _build_resume_prompt(
        account=None,
        job_description="JD_MARKER",
        template_html=llm_body,
        prompt_body="CUSTOM_MARKER",
    )
    # Template CSS must not appear in the prompt; system rules may mention <style>.
    assert ".x{color:red}" not in prompt
    assert "OLD NAME" in prompt
    assert prompt.index("OLD NAME") < prompt.index("CUSTOM_MARKER") < prompt.index("JD_MARKER")


def test_finalize_head_style_preserved_model_hack_discarded():
    model = (
        "<!DOCTYPE html><html><head><style>.HACKED{color:green}</style></head>"
        "<body><h1>NEW NAME</h1><p>new summary</p></body></html>"
    )
    out = _finalize_resume_html(HEAD_STYLE_TEMPLATE, model)
    _validate_llm_html(out)
    assert ".x{color:red}" in out
    assert "HACKED" not in out
    assert "NEW NAME" in out
    assert "OLD NAME" not in out


def test_finalize_body_style_reinjected():
    model = "<body><h1>NEW NAME</h1><p>new summary</p></body>"
    out = _finalize_resume_html(BODY_STYLE_TEMPLATE, model)
    _validate_llm_html(out)
    assert "<style>.y{color:blue}</style>" in out
    assert "NEW NAME" in out
    # Style should sit inside body (after opening tag).
    body_idx = out.lower().index("<body")
    style_idx = out.lower().index("<style")
    assert style_idx > body_idx


def test_ensure_body_styles_idempotent_when_already_present():
    out = _finalize_resume_html(
        BODY_STYLE_TEMPLATE,
        "<body><style>.y{color:blue}</style><h1>NEW</h1></body>",
    )
    # Exactly one copy of the original style tag content.
    assert out.count("<style>.y{color:blue}</style>") == 1


def test_strip_style_tags_removes_all():
    html = "<div><style>a{}</style><p>x</p><style>b{}</style></div>"
    assert strip_style_tags(html) == "<div><p>x</p></div>"


def test_body_styles_vs_head_split():
    both = (
        "<html><head><style>.h{}</style></head>"
        "<body><style>.b{}</style><p>x</p></body></html>"
    )
    body_styles, head_styles = body_styles_vs_head(both)
    assert len(head_styles) == 1 and ".h{}" in head_styles[0]
    assert len(body_styles) == 1 and ".b{}" in body_styles[0]


def test_em_dash_in_css_survives_body_dashes_normalized():
    out = _finalize_resume_html(EM_DASH_IN_CSS_TEMPLATE, "<body><h1>NEW — NAME</h1></body>")
    _validate_llm_html(out)
    assert "A — B" in out  # CSS em-dash preserved
    assert "NEW - NAME" in out  # body em-dash normalized
    assert "NEW — NAME" not in out


def test_normalize_dashes_body_only_leaves_head_alone():
    html = (
        "<html><head><style>.x{content:'A — B'}</style></head>"
        "<body>C — D</body></html>"
    )
    out = _normalize_dashes_body_only(html)
    assert "A — B" in out
    assert "C - D" in out


def test_validate_accepts_style_mismatch_by_construction():
    # Structural validate only — no style-equality 502.
    _validate_llm_html(
        "<!DOCTYPE html><html><head></head><body><p>ok</p></body></html>"
    )
