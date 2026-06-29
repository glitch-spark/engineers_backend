"""Resume generation pipeline: OpenAI rewrites the user's HTML template in
place, Playwright renders to PDF, optional upload to S3.

The LLM receives the raw uploaded HTML template plus candidate data, the job
description, and the user's custom prompt. It returns ONLY the rewritten
<body> — structure, classes, and tag layout preserved byte-for-byte. The
template's DOCTYPE/<head>/<style>/<link> shell is kept server-side and the
rewritten body is spliced back in (see `_reassemble_body`), so the CSS can
never drift and we don't pay output tokens to re-emit it. Playwright then
renders the reassembled HTML to PDF, with optional upload to S3.

Identical regenerations skip the OpenAI call via a content-hash cache
(`ResumeLLMCache`); see `generate_resume`.
"""

from __future__ import annotations

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
from app.llm.client import complete_text
from app.llm.resolver import resolve_llm_config
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
    "You are editing a resume HTML template IN PLACE. The uploaded template "
    "already contains the candidate's real data (name, contact, education, "
    "company names, dates, locations, project titles). The candidate's "
    "custom prompt below tells you which fields to rewrite for this "
    "particular job — typically the headline / title, the per-company role "
    "label, achievement bullets, and the summary.\n"
    "\n"
    "ABSOLUTE RULES — these override anything in the custom prompt:\n"
    "1. Output ONLY the page's <body> element — everything from the opening "
    "<body ...> tag through the closing </body>, inclusive. Do NOT output the "
    "DOCTYPE, <html>, <head>, <style>, or <link> tags. They are fixed and are "
    "re-attached automatically; emitting them wastes output and they are "
    "discarded. Preserve every element inside <body> BYTE-FOR-BYTE except for "
    "the specific text nodes the custom prompt asks you to rewrite.\n"
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


def _extract_style_block(html: str) -> str:
    """Return the FIRST <style>…</style> block's body, lowercased + stripped
    of whitespace, for byte-equal comparison between template and LLM
    output. If no <style> tag, return empty string (templates without inline
    style are accepted as-is)."""
    soup = BeautifulSoup(html or "", "html.parser")
    el = soup.find("style")
    if el is None:
        return ""
    return re.sub(r"\s+", "", el.get_text() or "").lower()


def _validate_llm_html(template: str, out_html: str) -> None:
    """Guards: output parses, has html/body, and the <style> block (if any)
    survived byte-for-byte. The system prompt forbids touching <style>; this
    enforces it. Raises HTTPException on failure."""
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
    tpl_style = _extract_style_block(template)
    out_style = _extract_style_block(out_html)
    if tpl_style and tpl_style != out_style:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Resume LLM modified the template's <style> block. "
                   "Re-run the generation; if it keeps happening, raise the "
                   "issue — the model is drifting from the template.",
        )


def _build_resume_prompt(
    account: Account,
    job_description: str,
    template_html: str,
    prompt_body: Optional[str] = None,
) -> str:
    """Compose the LLM prompt: system rules → template HTML → custom prompt
    body (if any) → JD. The template carries all candidate data; no separate
    candidate-fields block.

    Ordering is deliberate for OpenAI prompt caching: the account-stable
    prefix (system rules + template) comes FIRST so it warm-hits across
    generations, and the per-generation parts (custom prompt, JD) come LAST.
    The custom prompt sits after the template — putting it before would
    bust the cache for the much larger template that follows it. When no
    custom prompt is supplied, the model gets just the system rules — no
    built-in content guidance."""
    body = (prompt_body or "").strip()
    custom_block = (
        "\n\nCUSTOM PROMPT (the candidate's guidance — follow it within "
        "the absolute rules above):\n" + body
        if body else ""
    )
    # Soft-trim the JD before sending. Core role requirements live in the
    # first few KB; the tail is usually benefits / EEO / legal boilerplate
    # with little tailoring signal. Capping cuts input tokens with no
    # quality loss. Tunable via RESUME_JD_MAX_CHARS.
    jd = (job_description or "").strip()
    cap = getattr(get_settings(), "RESUME_JD_MAX_CHARS", 12000) or 12000
    if len(jd) > cap:
        jd = jd[:cap].rstrip() + "\n[... job description truncated ...]"
    return (
        _SYSTEM_RULES
        + "\n\n=== TEMPLATE HTML (the candidate's resume — rewrite its body "
          "text in place; return ONLY the <body> element) ===\n"
        + template_html
        + custom_block
        + "\n\n=== JOB DESCRIPTION ===\n"
        + jd
    )


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


async def _llm_call(
    prompt: str,
    *,
    user: UserExtra,
    account: Account,
) -> tuple[str, dict, str, str, bool]:
    """Returns (text, usage, provider, model, fallback_used)."""
    config = await resolve_llm_config(user, account)
    result = await complete_text(
        config,
        prompt,
        user_id=str(user.id),
        account_id=str(account.id),
    )
    usage = result.usage
    return result.text, usage, result.provider_used, result.model_used, result.fallback_used


def _resume_cache_key(prompt: str, provider: str, model: str) -> str:
    return hashlib.sha256(
        "\x00".join([provider, model, prompt]).encode("utf-8")
    ).hexdigest()


async def _resume_llm_output(
    cache_key: str,
    prompt: str,
    provider: str,
    model: str,
    *,
    user: UserExtra,
    account: Account,
) -> tuple[str, dict, bool]:
    cached = await ResumeLLMCache.find_one(ResumeLLMCache.promptHash == cache_key)
    if cached is not None:
        log.info("resume LLM cache HIT key=%s provider=%s", cache_key[:12], cached.provider)
        return cached.output, {"input": 0, "output": 0, "reasoning": 0}, False

    raw, usage, prov, mod, fallback = await _llm_call(prompt, user=user, account=account)
    try:
        await ResumeLLMCache(
            promptHash=cache_key,
            output=raw,
            model=model,
            provider=prov,
            inputTokens=usage.get("input"),
            outputTokens=usage.get("output"),
            reasoningTokens=usage.get("reasoning"),
        ).insert()
        log.info("resume LLM cache STORE key=%s provider=%s", cache_key[:12], prov)
    except Exception as exc:  # noqa: BLE001
        log.warning("resume LLM cache store skipped (key=%s): %s", cache_key[:12], exc)
    return raw, usage, fallback


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


def _maybe_upload_to_s3(pdf_bytes: bytes, profile_name: str, company: str) -> tuple[Optional[str], Optional[str]]:
    settings = get_settings()
    bucket = settings.AWS_S3_BUCKET
    region = settings.AWS_REGION
    if not bucket or not region:
        return None, None

    s3 = _s3_client()
    date_segment = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    key = f"{date_segment}/{_safe_segment(profile_name)}/{_safe_segment(company)}/Resume.pdf"
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
    # Plain reading-order text extracted from the rewritten HTML. Used as
    # candidate context for follow-up screening Q&A so the answers reflect
    # the tailored resume, not the raw template.
    plain_text: Optional[str] = None
    llm_provider_used: Optional[str] = None
    llm_fallback_used: bool = False


async def generate_resume(
    account: Account,
    job_description: str,
    company: str,
    prompt_body: Optional[str] = None,
    *,
    user: UserExtra,
) -> GeneratedResume:
    """Run the full pipeline: LLM rewrites the template's <body> text (or a
    content-hash cache hit reuses a prior output, skipping the call), the body
    is spliced back into the template shell, Playwright renders the HTML to
    PDF, optional S3 upload."""
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

    prompt = _build_resume_prompt(account, job_description, template, prompt_body=prompt_body)
    llm_config = await resolve_llm_config(user, account)
    provider = llm_config.effective_provider
    model = llm_config.primary.model
    cache_key = _resume_cache_key(prompt, provider, model)
    raw, usage, fallback = await _resume_llm_output(
        cache_key, prompt, provider, model, user=user, account=account
    )

    # The model returns only the rewritten <body>; splice it back into the
    # template's shell so the DOCTYPE/<head>/<style> stay byte-for-byte
    # identical (CSS cannot drift) and we don't pay output tokens to re-emit
    # them. Tolerates a full-document or bare-fragment response too.
    body_fragment = _strip_fences(raw)
    out_html = _reassemble_body(template, body_fragment)
    _validate_llm_html(template, out_html)

    pdf_bytes = await _html_to_pdf(
        out_html,
        page_format=account.pageFormat or "A4",
        margin_pt=_resolve_margin_pt(account),
    )
    s3_key, _region = _maybe_upload_to_s3(pdf_bytes, account.name, company)
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
        llm_provider_used=provider,
        llm_fallback_used=fallback,
    )


_COVER_LETTER_SYSTEM = (
    "You write a personalized cover letter for a job application. The "
    "letter MUST be grounded in two inputs supplied below:\n"
    "  1. CANDIDATE RESUME — the tailored resume's plain text. Every fact "
    "the letter cites (experience, companies, years, technologies, "
    "achievements, metrics) MUST come from this resume. Do NOT invent "
    "roles, companies, dates, projects, or numbers that are not present.\n"
    "  2. JOB DESCRIPTION — the role's requirements and priorities. The "
    "letter must tie the candidate's resume facts to what this specific "
    "JD asks for (skills, domain, seniority, mission). Do not paste JD "
    "phrases verbatim.\n"
    "\n"
    "Output rules:\n"
    "- PLAIN TEXT only. No Markdown, no HTML, no code fences, no "
    "surrounding commentary.\n"
    "- 3-5 short paragraphs separated by a blank line, unless the custom "
    "prompt below overrides.\n"
    "- Address the hiring team naturally; do not invent a recruiter's "
    "name.\n"
    "- If the resume lacks evidence for a JD requirement, do not claim "
    "it — pivot to adjacent strengths.\n"
)


async def generate_cover_letter(
    *,
    candidate_resume_text: str,
    job_description: str,
    company: str,
    prompt_body: str,
    user: UserExtra,
    account: Account,
) -> tuple[str, dict, bool]:
    """Generate a plain-text cover letter. Returns (text, usage, fallback_used)."""
    parts: list[str] = [_COVER_LETTER_SYSTEM]
    body = (prompt_body or "").strip()
    if body:
        parts.append(
            "CUSTOM PROMPT (candidate's style guidance — follow within the "
            "absolute rules above):\n" + body
        )
    if (company or "").strip():
        parts.append(f"Target company: {company.strip()}")
    # Both sources are required for grounded output — log a warning when
    # either is missing so the caller can fix the upstream pipeline.
    if not (candidate_resume_text or "").strip():
        log.warning("cover letter prompt missing resume text — output will be generic")
    if not (job_description or "").strip():
        log.warning("cover letter prompt missing job description — output will be generic")
    parts.append(
        "=== CANDIDATE RESUME (source of all factual claims) ===\n"
        + (candidate_resume_text or "(no resume text supplied)").strip()
    )
    parts.append(
        "=== JOB DESCRIPTION (what the letter must address) ===\n"
        + (job_description or "(no JD supplied)").strip()
    )
    text, usage, _, _, fallback = await _llm_call(
        "\n\n".join(parts), user=user, account=account
    )
    return _strip_fences(text), usage, fallback
