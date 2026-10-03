"""Resume generation pipeline: LLM rewrites the user's resume body text,
Playwright renders to PDF, optional upload to S3.

The LLM receives style-stripped body HTML only (CSS never enters the model
context). The template's DOCTYPE/<head>/<style>/<link> shell — and any
<body>-origin <style> tags — are kept server-side and reattached after the
rewrite (see `_finalize_resume_html`), so uploaded styles cannot drift.
Playwright then renders the reassembled HTML to PDF, with optional upload
to S3.

Identical regenerations skip the LLM call via a content-hash cache
(`ResumeLLMCache`); see `generate_resume`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from fastapi import HTTPException, status
from playwright.async_api import async_playwright

from app.config import get_settings
from app.html_sanitize import (
    body_html_for_llm,
    body_styles_vs_head,
    strip_style_tags,
    wrap_body_for_llm,
)
from app.llm_router import LlmResult, complete
from app.model_catalog import cache_lane, fallback_chain
from app.models import Account, ResumeLLMCache, UserExtra

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

# Hard rules the LLM must obey. Wrapped around the user's custom prompt so a
# user override cannot accidentally break the strict template-preservation
# contract — the template is the candidate's real data; only fields the
# prompt asks for are rewritten.
_SYSTEM_RULES = (
    "You are editing a resume HTML body IN PLACE. The input is the "
    "candidate's resume body with CSS already removed — styles are restored "
    "server-side and must not appear in your output. The body already "
    "contains the candidate's real data (name, contact, education, company "
    "names, dates, locations, project titles). The candidate's custom prompt "
    "below tells you which fields to rewrite for this particular job — "
    "typically the headline / title, the per-company role label, achievement "
    "bullets, and the summary.\n"
    "\n"
    "ABSOLUTE RULES — these override anything in the custom prompt:\n"
    "1. Output ONLY the page's <body> element — everything from the opening "
    "<body ...> tag through the closing </body>, inclusive. Do NOT output the "
    "DOCTYPE, <html>, <head>, <style>, or <link> tags. CSS is not in the "
    "input and must not be invented or emitted; it is re-attached "
    "automatically. Preserve every element inside <body> BYTE-FOR-BYTE except "
    "for the specific text nodes the custom prompt asks you to rewrite.\n"
    "2. Within the body, do NOT modify, reorder, rename, add, or remove: "
    "class names, ids, inline style attributes, href targets, font links, or "
    "DOM structure.\n"
    "3. ALLOWED to rewrite by default: the headline / title text, "
    "per-company role / job-title text, achievement bullet text, project "
    "bullet text, and the summary paragraph text. The custom prompt may "
    "narrow this list further or extend it (e.g. to also rewrite skill "
    "items or project titles) — follow it.\n"
    "4. PRESERVE VERBATIM unless the custom prompt explicitly says "
    "otherwise: candidate name, contact line (text + hrefs), company "
    "names, employment dates, employment locations, education entries, "
    "project titles, skill category labels and items.\n"
    "5. KEEP THE TEMPLATE'S COUNTS. If a job has N bullets in the "
    "template, the output has N bullets. Same for projects per company, "
    "skill rows, education rows, contact items. Never add or drop "
    "entries — the template's shape is the spec.\n"
    "6. Output raw HTML only — the <body> element and nothing else. No code "
    "fences. No prose. No comments before or after it.\n"
)

# Soft-trim marker appended when a JD exceeds RESUME_JD_MAX_CHARS.
_JD_TRUNCATION_MARKER = "\n[... job description truncated ...]"


def trim_jd(job_description: str) -> str:
    """Soft-cap JD chars for LLM prompts. Core requirements sit up front;
    the tail is usually benefits / EEO / legal boilerplate. Tunable via
    RESUME_JD_MAX_CHARS (~12000 ≈ 3K tokens)."""
    jd = (job_description or "").strip()
    cap = getattr(get_settings(), "RESUME_JD_MAX_CHARS", 12000) or 12000
    if len(jd) > cap:
        return jd[:cap].rstrip() + _JD_TRUNCATION_MARKER
    return jd


_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _collapse_blank_lines(html: str) -> str:
    """Light whitespace trim: collapse 3+ consecutive newlines to 2. Does
    not touch intra-tag spaces or <pre> content shape beyond blank runs."""
    return _BLANK_LINES_RE.sub("\n\n", html or "")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_S3_SAFE = re.compile(r"[^a-zA-Z0-9\-_]")


def _safe_segment(s: str) -> str:
    return _S3_SAFE.sub("-", (s or "user").strip())


def _strip_fences(s: str) -> str:
    """Drop ```html … ``` or ```… ``` fences the model might wrap the doc in.
    The system prompt forbids fences, but models occasionally ignore that —
    we tolerate and recover instead of failing the job."""
    s = (s or "").strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


# Typographic “long” dashes LLMs often emit → ASCII hyphen-minus.
_LONG_DASH_TRANSLATE = str.maketrans({
    "\u2012": "-",  # figure dash ‒
    "\u2013": "-",  # en dash –
    "\u2014": "-",  # em dash —
    "\u2015": "-",  # horizontal bar ―
})


def _normalize_dashes(text: str) -> str:
    """Replace common typographic long dashes with ASCII hyphens."""
    return (text or "").translate(_LONG_DASH_TRANSLATE)


_BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)


def _body_inner_html(html: str) -> Optional[str]:
    """Inner HTML of the first <body> element, or None if there's no
    recognizable <body>…</body> region."""
    m = _BODY_OPEN_RE.search(html or "")
    if not m:
        return None
    end = (html or "").lower().rfind("</body>")
    if end == -1 or end < m.end():
        return None
    return html[m.end():end]


def _normalize_dashes_body_only(html: str) -> str:
    """Normalize long dashes inside <body> only — never touch <style>/<head>."""
    m = _BODY_OPEN_RE.search(html or "")
    if not m:
        return _normalize_dashes(html)
    end = (html or "").lower().rfind("</body>")
    if end == -1 or end < m.end():
        return _normalize_dashes(html)
    return (
        html[:m.end()]
        + _normalize_dashes(html[m.end():end])
        + html[end:]
    )


def _reassemble_body(template_html: str, model_output: str) -> str:
    """Splice the model's rewritten body into the template, keeping the
    template's DOCTYPE/<head>/<style>/<link> shell byte-for-byte. The model is
    asked to return only the <body>; we tolerate it returning a full document
    (we take its body) or a bare fragment (we treat the whole thing as the
    body's inner HTML). The template's own <body> open/close tags — and thus
    its body attributes — are preserved; only the inner content is replaced.

    If the template has no clean <body> region to splice into, fall back to
    the raw model output as a full document so generation still produces
    something renderable."""
    inner = _body_inner_html(model_output)
    if inner is None:
        inner = (model_output or "").strip()
    m = _BODY_OPEN_RE.search(template_html or "")
    end = (template_html or "").lower().rfind("</body>")
    if not m or end == -1 or end < m.end():
        return model_output
    return template_html[:m.end()] + inner + template_html[end:]


def _ensure_body_styles(out_html: str, template_html: str) -> str:
    """Re-inject original body-origin <style> tags after splice if missing.

    Head styles already survive via `_reassemble_body`. Templates that store
    CSS inside <body> lose those tags when the body is replaced — restore them
    immediately after the opening <body…> so layout matches the upload.
    """
    body_styles, _head_styles = body_styles_vs_head(template_html or "")
    if not body_styles:
        return out_html
    # Already present (e.g. model copied them, or template had no clean body
    # and we fell back to raw model output that kept styles).
    if all(tag in (out_html or "") for tag in body_styles):
        return out_html
    m = _BODY_OPEN_RE.search(out_html or "")
    if not m:
        return out_html
    injection = "".join(body_styles)
    return out_html[:m.end()] + injection + out_html[m.end():]


def _model_body_fragment(model_raw: str) -> str:
    """Strip fences + any sneaked <style> from model output; wrap as <body>."""
    raw = _strip_fences(model_raw)
    inner = _body_inner_html(raw)
    if inner is None:
        inner = (raw or "").strip()
    inner = strip_style_tags(inner)
    return f"<body>{inner}</body>"


def _finalize_resume_html(template: str, model_raw: str) -> str:
    """Assemble final HTML: template shell + cleaned model body + body styles.

    CSS never comes from the model. Em-dash normalization applies to body text
    only so stylesheet content is preserved byte-for-byte.
    """
    fragment = _model_body_fragment(model_raw)
    out = _reassemble_body(template, fragment)
    out = _ensure_body_styles(out, template)
    out = _normalize_dashes_body_only(out)
    return out


def _validate_llm_html(out_html: str) -> None:
    """Structural guards only: non-empty, has html or body.

    Style equality is no longer checked — CSS is isolated from the LLM and
    reattached from the uploaded template by construction.
    """
    if not (out_html or "").strip():
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Resume LLM returned empty output.",
        )
    soup = BeautifulSoup(out_html, "html.parser")
    if soup.find("body") is None and soup.find("html") is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Resume LLM output is not a full HTML document.",
        )


def _build_resume_prompt(
    account: Account,
    job_description: str,
    template_html: str,
    prompt_body: Optional[str] = None,
) -> str:
    """Compose the LLM prompt: system rules → body-only HTML → custom prompt
    body (if any) → JD.

    Prefer callers to pass style-stripped body markup (see
    `body_html_for_llm` / `wrap_body_for_llm`). Full documents are also
    accepted — CSS is stripped defensively so it never enters the prompt.

    Ordering is deliberate for OpenAI prompt caching: the account-stable
    prefix (system rules + body) comes FIRST so it warm-hits across
    generations, and the per-generation parts (custom prompt, JD) come LAST.
    """
    body_html = _collapse_blank_lines(
        wrap_body_for_llm(body_html_for_llm(template_html))
    )
    body = (prompt_body or "").strip()
    custom_block = (
        "\n\nCUSTOM PROMPT (the candidate's guidance — follow it within "
        "the absolute rules above):\n" + body
        if body else ""
    )
    jd = trim_jd(job_description)
    return (
        _SYSTEM_RULES
        + "\n\n=== RESUME BODY HTML (CSS removed — rewrite text in place; "
          "return ONLY the <body> element; do not emit <style>) ===\n"
        + body_html
        + custom_block
        + "\n\n=== JOB DESCRIPTION ===\n"
        + jd
    )


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def _resume_cache_key(prompt: str, lane: str) -> str:
    """Content hash that fully determines the LLM output: lane (provider+model
    params) + the complete prompt."""
    return hashlib.sha256(
        "\x00".join([lane, prompt]).encode("utf-8")
    ).hexdigest()


async def _resume_llm_output(
    prompt: str,
    user: Optional[UserExtra],
    template: str,
    model_id: Optional[str] = None,
) -> LlmResult:
    """Return LLM output with provider metadata. Uses a content-hash cache per model lane.

    Only the preferred model's lane is checked, so a fallback entry never shadows the model the
    user asked for (it gets retried next time). On a miss the router walks its fallback chain
    and the output is stored under the lane of the model that actually answered."""

    def _validate(text: str) -> None:
        _validate_llm_html(_finalize_resume_html(template, text))

    chain = fallback_chain("resume", model_id, user)
    if chain:
        preferred = chain[0]
        cache_key = _resume_cache_key(prompt, cache_lane(preferred, "resume", user))
        cached = await ResumeLLMCache.find_one(ResumeLLMCache.promptHash == cache_key)
        if cached is not None:
            log.info("resume LLM cache HIT key=%s model=%s", cache_key[:12], cached.model)
            return LlmResult(
                text=cached.output,
                provider=preferred.provider,
                model=cached.model,
                usage={"input": 0, "output": 0, "reasoning": 0},
                fallback_used=False,
                model_id=preferred.id,
                cost_usd=0.0,
            )

    result = await complete("resume", prompt, user, model_id=model_id, validate=_validate)

    answered = next((s for s in chain if s.id == result.model_id), None)
    if answered is not None:
        cache_key = _resume_cache_key(prompt, cache_lane(answered, "resume", user))
        try:
            await ResumeLLMCache(
                promptHash=cache_key,
                output=result.text,
                model=result.model,
                isFallback=result.fallback_used,
                inputTokens=result.usage.get("input"),
                outputTokens=result.usage.get("output"),
                reasoningTokens=result.usage.get("reasoning"),
            ).insert()
            log.info("resume LLM cache STORE key=%s fallback=%s", cache_key[:12], result.fallback_used)
        except Exception as exc:  # noqa: BLE001
            log.warning("resume LLM cache store skipped (key=%s): %s", cache_key[:12], exc)

    return result


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


_DEFAULT_MARGIN_PT = {"top": 35, "right": 30, "bottom": 30, "left": 30}


def _resolve_margin_pt(account: Account) -> dict:
    """Template owns its own @page margins via CSS. We pass defaults; the
    template's CSS @page rules override these visually."""
    return dict(_DEFAULT_MARGIN_PT)


_PT_TO_MM = 25.4 / 72


def _pt_to_mm(v: float) -> str:
    return f"{v * _PT_TO_MM:.3f}mm"


async def _html_to_pdf(html: str, page_format: str = "A4", margin_pt: Optional[dict] = None) -> bytes:
    m = margin_pt or _DEFAULT_MARGIN_PT
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            # Wait for web fonts (the template's own <link>/<style>
            # @import) to resolve before snapshotting; otherwise
            # Chromium can rasterize with a system fallback.
            try:
                await page.evaluate("document.fonts.ready")
            except Exception:
                pass
            return await page.pdf(
                format=page_format,
                margin={
                    "top": _pt_to_mm(m["top"]),
                    "right": _pt_to_mm(m["right"]),
                    "bottom": _pt_to_mm(m["bottom"]),
                    "left": _pt_to_mm(m["left"]),
                },
                print_background=True,
            )
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def _s3_client():
    import boto3
    from botocore.config import Config

    s = get_settings()
    cfg = Config(signature_version="s3v4", s3={"addressing_style": "virtual"})
    kwargs = {"region_name": s.AWS_REGION, "config": cfg}
    if s.AWS_ACCESS_KEY_ID and s.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = s.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = s.AWS_SECRET_ACCESS_KEY
    return boto3.client("s3", **kwargs)


def s3_enabled() -> bool:
    settings = get_settings()
    return bool(settings.AWS_S3_BUCKET and settings.AWS_REGION)


def generate_presigned_get(s3_key: str, expires: int = 900, download_filename: Optional[str] = None) -> str:
    settings = get_settings()
    params: dict = {"Bucket": settings.AWS_S3_BUCKET, "Key": s3_key}
    if download_filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_filename}"'
    return _s3_client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=expires,
    )


def _maybe_upload_to_s3(
    pdf_bytes: bytes, profile_name: str, company: str, job_id: Optional[str] = None
) -> tuple[Optional[str], Optional[str]]:
    settings = get_settings()
    bucket = settings.AWS_S3_BUCKET
    region = settings.AWS_REGION
    if not bucket or not region:
        return None, None

    s3 = _s3_client()
    date_segment = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    # The job id keeps two generations for the same profile/company/day from overwriting each other.
    job_segment = f"{_safe_segment(job_id)}/" if job_id else ""
    key = f"{date_segment}/{_safe_segment(profile_name)}/{_safe_segment(company)}/{job_segment}Resume.pdf"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=pdf_bytes,
        ContentType="application/pdf",
        ServerSideEncryption="AES256",
    )
    log.info("uploaded resume to s3: bucket=%s key=%s", bucket, key)
    return key, region


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


@dataclass
class GeneratedResume:
    pdf_bytes: bytes
    s3_key: Optional[str]
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    plain_text: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_fallback_used: Optional[bool] = None
    llm_fallback_reason: Optional[str] = None
    llm_model_id: Optional[str] = None
    cost_usd: Optional[float] = None


_render_sem: Optional[asyncio.Semaphore] = None


def _render_slots() -> asyncio.Semaphore:
    """At most RESUME_RENDER_CONCURRENCY Chromium renders at once (each launches its own browser)."""
    global _render_sem
    if _render_sem is None:
        _render_sem = asyncio.Semaphore(max(1, get_settings().RESUME_RENDER_CONCURRENCY))
    return _render_sem


async def generate_resume(
    account: Account,
    job_description: str,
    company: str,
    prompt_body: Optional[str] = None,
    owner: Optional[UserExtra] = None,
    job_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> GeneratedResume:
    """Run the full pipeline: LLM rewrites style-stripped body text (or a
    content-hash cache hit reuses a prior output, skipping the call), the body
    is spliced back into the template shell with original CSS restored,
    Playwright renders the HTML to PDF, optional S3 upload."""
    template = (account.styleTemplate or "").strip()
    if not template:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No HTML template on this profile. Upload one on the "
                   "Resume Styles tab first.",
        )

    log.info(
        "Generating resume for account=%s (%s) company=%s",
        account.name, account.id, company,
    )

    llm_body = wrap_body_for_llm(body_html_for_llm(template))
    prompt = _build_resume_prompt(account, job_description, llm_body, prompt_body=prompt_body)
    llm_result = await _resume_llm_output(prompt, owner, template, model_id)
    raw = llm_result.text
    usage = llm_result.usage

    # Model returns rewritten body only; CSS is reattached from the uploaded
    # template (head styles via splice, body-origin styles via reinjection).
    out_html = _finalize_resume_html(template, raw)
    _validate_llm_html(out_html)

    async with _render_slots():
        pdf_bytes = await _html_to_pdf(
            out_html,
            page_format=account.pageFormat or "A4",
            margin_pt=_resolve_margin_pt(account),
        )
    s3_key, _region = await asyncio.to_thread(_maybe_upload_to_s3, pdf_bytes, account.name, company, job_id)
    plain_text = re.sub(
        r"\n{3,}", "\n\n",
        BeautifulSoup(out_html, "html.parser").get_text("\n").strip(),
    )
    return GeneratedResume(
        pdf_bytes=pdf_bytes,
        s3_key=s3_key,
        input_tokens=usage.get("input"),
        output_tokens=usage.get("output"),
        reasoning_tokens=usage.get("reasoning"),
        plain_text=plain_text,
        llm_provider=llm_result.provider,
        llm_model=llm_result.model,
        llm_fallback_used=llm_result.fallback_used,
        llm_fallback_reason=llm_result.fallback_reason,
        llm_model_id=llm_result.model_id,
        cost_usd=llm_result.cost_usd,
    )
