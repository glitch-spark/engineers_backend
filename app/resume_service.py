"""Resume generation pipeline: OpenAI rewrites the user's HTML template in
place, Playwright renders to PDF, optional upload to S3.

The LLM receives the raw uploaded HTML template plus candidate data, the job
description, and the user's custom prompt. It returns the SAME HTML with the
template's sample text replaced by tailored content — structure, CSS, classes,
and tag layout preserved byte-for-byte. No marker annotation, no deterministic
fill: the model handles the replacement directly, the way ChatGPT does when a
user uploads a template into a chat.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup
from fastapi import HTTPException, status
from openai import AsyncOpenAI
from playwright.async_api import async_playwright

from app.config import get_settings
from app.models import Account

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
    "1. Output the FULL HTML document — same DOCTYPE, <html>, <head>, "
    "<style>, <link>, <body>, and every element from the template — "
    "preserved BYTE-FOR-BYTE except for the specific text nodes the custom "
    "prompt asks you to rewrite.\n"
    "2. Do NOT modify, reorder, rename, add, or remove: <style> block "
    "contents, class names, ids, inline style attributes, href targets, "
    "font links, or DOM structure.\n"
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
    "6. Output raw HTML only. No code fences. No prose. No comments "
    "before or after the document.\n"
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
    """Compose the LLM prompt: system rules → custom prompt body (if any) →
    template HTML → JD. The template carries all candidate data; no separate
    candidate-fields block. Stable prefix first (account-stable across
    generations) so OpenAI prompt caching can warm-hit; per-generation JD
    last. When no custom prompt is supplied, the model gets just the system
    rules — no built-in content guidance."""
    body = (prompt_body or "").strip()
    custom_block = (
        "\n\nCUSTOM PROMPT (the candidate's guidance — follow it within "
        "the absolute rules above):\n" + body
        if body else ""
    )
    return (
        _SYSTEM_RULES
        + custom_block
        + "\n\n=== TEMPLATE HTML (the candidate's resume — edit in place; "
          "return the full document) ===\n"
        + template_html
        + "\n\n=== JOB DESCRIPTION ===\n"
        + (job_description or "").strip()
    )


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


async def _openai_call(prompt: str) -> str:
    """Single OpenAI Responses call. Returns the raw output_text."""
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY not configured",
        )
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    response = await client.responses.create(
        model=settings.RESUME_OPENAI_MODEL,
        input=prompt,
    )
    return response.output_text or ""


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


async def generate_resume(
    account: Account,
    job_description: str,
    company: str,
    prompt_body: Optional[str] = None,
) -> GeneratedResume:
    """Run the full pipeline: LLM rewrites the template's text in place,
    Playwright renders the resulting HTML to PDF, optional S3 upload."""
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
    raw = await _openai_call(prompt)
    out_html = _strip_fences(raw)
    _validate_llm_html(template, out_html)

    pdf_bytes = await _html_to_pdf(
        out_html,
        page_format=account.pageFormat or "A4",
        margin_pt=_resolve_margin_pt(account),
    )
    s3_key, _region = _maybe_upload_to_s3(pdf_bytes, account.name, company)
    return GeneratedResume(pdf_bytes=pdf_bytes, s3_key=s3_key)
