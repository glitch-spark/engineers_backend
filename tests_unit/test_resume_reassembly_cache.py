"""Unit tests for the resume API-usage reductions:

- body-only output + server-side reassembly (`_reassemble_body`), which makes
  the template's <style>/<head> impossible to drift;
- body-only *input* via `body_html_for_llm` so CSS never enters the LLM prompt;
- the content-hash cache key (`_resume_cache_key`);
- the cache-friendly prompt ordering (`_build_resume_prompt`): stable
  body before the per-generation custom prompt and JD;
- the cache TTL and isFallback flag on ResumeLLMCache.

These are all pure functions — no Mongo, no OpenAI. `_build_resume_prompt`
reads settings, so we ensure required env vars exist (dummy values; nothing
connects) before importing app modules.
"""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.models.resume_cache import CACHE_TTL_SECONDS, ResumeLLMCache  # noqa: E402
from app.resume_service import (  # noqa: E402
    _body_inner_html,
    _build_resume_prompt,
    _reassemble_body,
    _resume_cache_key,
    trim_jd,
)

TEMPLATE = (
    "<!DOCTYPE html><html><head><style>.x{color:red}</style></head>"
    '<body class="page"><h1>OLD NAME</h1><p>old summary</p></body></html>'
)


# --- _body_inner_html ------------------------------------------------------

def test_body_inner_extracts_inner_html():
    assert _body_inner_html("<body class='a'>X<b>Y</b></body>") == "X<b>Y</b>"


def test_body_inner_none_for_bare_fragment():
    assert _body_inner_html("<h1>no body wrapper</h1>") is None


def test_body_inner_none_when_unclosed():
    assert _body_inner_html("<body>missing close") is None


# --- _reassemble_body ------------------------------------------------------

def _assert_tailored(result: str) -> None:
    # Template shell + style survive byte-for-byte; content is swapped.
    assert result.startswith("<!DOCTYPE html>")
    assert ".x{color:red}" in result          # template style preserved
    assert "NEW NAME" in result and "new summary" in result
    assert "OLD NAME" not in result


def test_reassemble_from_full_document_discards_model_head_and_style():
    model_out = (
        "<!DOCTYPE html><html><head><style>.HACKED{color:blue}</style></head>"
        '<body class="evil"><h1>NEW NAME</h1><p>new summary</p></body></html>'
    )
    res = _reassemble_body(TEMPLATE, model_out)
    _assert_tailored(res)
    assert "HACKED" not in res          # model's style cannot drift in
    assert 'class="page"' in res        # template's body attrs win
    assert "evil" not in res


def test_reassemble_from_body_only():
    res = _reassemble_body(TEMPLATE, "<body><h1>NEW NAME</h1><p>new summary</p></body>")
    _assert_tailored(res)


def test_reassemble_from_bare_fragment():
    res = _reassemble_body(TEMPLATE, "<h1>NEW NAME</h1><p>new summary</p>")
    _assert_tailored(res)


def test_reassemble_falls_back_when_template_has_no_body():
    # Nothing safe to splice into → return the model output untouched.
    assert _reassemble_body("<div>no body</div>", "<body>x</body>") == "<body>x</body>"


# --- _resume_cache_key -----------------------------------------------------

def test_cache_key_is_deterministic():
    assert _resume_cache_key("prompt", "openai:gpt-5-mini:low") == _resume_cache_key(
        "prompt", "openai:gpt-5-mini:low"
    )


def test_cache_key_varies_with_every_input():
    base = _resume_cache_key("prompt", "openai:gpt-5-mini:low")
    assert _resume_cache_key("prompt2", "openai:gpt-5-mini:low") != base
    assert _resume_cache_key("prompt", "free:minimaxai/minimax-m3:8192") != base
    assert _resume_cache_key("prompt", "openai:gpt-5-mini:medium") != base


def test_cache_key_no_field_bleed():
    assert _resume_cache_key("b", "lane:a") != _resume_cache_key("", "lane:ab")


# --- _build_resume_prompt: body-only input + cache-prefix ordering ---------

def test_prompt_orders_template_before_custom_prompt_and_jd():
    prompt = _build_resume_prompt(
        account=None,
        job_description="JD_MARKER requirements here",
        template_html="<body>TEMPLATE_MARKER</body>",
        prompt_body="CUSTOM_MARKER guidance",
    )
    i_template = prompt.index("TEMPLATE_MARKER")
    i_custom = prompt.index("CUSTOM_MARKER")
    i_jd = prompt.index("JD_MARKER")
    # Stable body markup must precede the per-generation custom prompt and JD so
    # it stays in OpenAI's cacheable prefix.
    assert i_template < i_custom < i_jd
    assert "RESUME BODY HTML" in prompt


def test_prompt_omits_custom_block_when_empty():
    prompt = _build_resume_prompt(
        account=None,
        job_description="JD here",
        template_html="<body>T</body>",
        prompt_body=None,
    )
    assert "CUSTOM PROMPT" not in prompt


def test_prompt_strips_style_and_head_from_template():
    """CSS must not enter the LLM prompt — only body inner HTML."""
    prompt = _build_resume_prompt(
        account=None,
        job_description="JD here",
        template_html=TEMPLATE,
        prompt_body=None,
    )
    assert ".x{color:red}" not in prompt
    assert "<!DOCTYPE" not in prompt
    # System rules mention <style>/<head> by name; assert template CSS/head
    # payload is absent rather than the instruction text.
    assert "color:red" not in prompt
    assert "OLD NAME" in prompt
    assert "old summary" in prompt
    assert "RESUME BODY HTML" in prompt


def test_prompt_keeps_body_structure_classes():
    prompt = _build_resume_prompt(
        account=None,
        job_description="JD",
        template_html='<!DOCTYPE html><html><head><style>.a{}</style></head>'
                      '<body><div class="section" id="exp"><p>text</p></div></body></html>',
    )
    assert 'class="section"' in prompt
    assert 'id="exp"' in prompt
    assert ".a{}" not in prompt


# --- trim_jd ---------------------------------------------------------------

def test_trim_jd_passthrough_when_under_cap():
    assert trim_jd("short jd") == "short jd"


def test_trim_jd_caps_long_jd(monkeypatch):
    from app import config as config_mod
    settings = config_mod.get_settings()
    monkeypatch.setattr(settings, "RESUME_JD_MAX_CHARS", 50)
    long_jd = "A" * 100
    out = trim_jd(long_jd)
    assert len(out) < len(long_jd)
    assert out.startswith("A" * 50)
    assert "truncated" in out


# --- ResumeLLMCache model ---------------------------------------------------

def test_cache_ttl_is_seven_days():
    assert CACHE_TTL_SECONDS == 7 * 24 * 3600


def test_cache_model_has_is_fallback_field_with_default_false():
    field = ResumeLLMCache.model_fields["isFallback"]
    assert field.default is False


def test_cache_has_ttl_index():
    indexes = ResumeLLMCache.Settings.indexes
    ttl_indexes = [
        idx for idx in indexes
        if getattr(idx, "document", {}).get("expireAfterSeconds") is not None
    ]
    assert len(ttl_indexes) >= 1, "Expected a TTL index on ResumeLLMCache"
