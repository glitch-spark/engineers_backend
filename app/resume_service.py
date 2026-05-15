"""Resume generation pipeline: OpenAI → HTML → Playwright PDF → optional S3.

Ported from the standalone resume-generator Express service. The OpenAI prompt
and the HTML/CSS wrapper are reproduced verbatim — they are the product spec
and changing them changes the output.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from openai import AsyncOpenAI
from playwright.async_api import async_playwright

from app.config import get_settings
from app.models import Account

log = logging.getLogger(__name__)


_PROMPT_BODY = (
    "Generate an ATS-optimized, job-tailored resume for {name} aligned to the job description being applied for. "
    "Contact header fields are governed by the HEADER OVERRIDE block below. "
    "Do NOT derive a city, country, region, or location from education, work experience, university name, or company name. "
    "If any contact field is empty, omit it from the header — never invent or infer a value. "
    "Do not modify company names, job titles, work-experience locations, or employment dates. "
    "For work experience, the most recent company must have exactly 8 bullet points and all other companies must have exactly 6 points each. "
    "For the most recent company, structure bullets as follows: 1 bullet describing a realistic core product or platform developed at the company, "
    "6 bullets describing core engineering work, and 1 bullet describing cross-functional collaboration and leadership. "
    "Distribute the 6 core engineering bullets based on job type: "
    "pure AI/GenAI engineer role uses 1 frontend, 1 backend, and 4 AI/GenAI bullets; "
    "AI-leaning full-stack role uses 2 frontend, 2 backend, and 2 AI/GenAI bullets; "
    "frontend role uses 4 frontend, 1 backend, and 1 DevOps bullet; backend role uses 1 frontend, 4 backend, and 1 DevOps bullet; "
    "frontend-leaning full-stack role uses 3 frontend, 2 backend, and 1 DevOps bullet; backend-leaning full-stack role uses 2 frontend, 3 backend, and 1 DevOps bullet; "
    "DevOps-leaning full-stack role uses 1 frontend, 2 backend, and 3 DevOps bullets, choosing AWS or Azure only if realistically used by the company rather than merely listed in the job description. "
    "Each bullet must contain only work details, must not be labeled by category, must avoid repetition, and may include quantitative metrics only when meaningful. "
    "Use ATS-friendly language with a professional, concise, non-repetitive tone, focusing on realistic tech stacks, architectures, workflows, and concrete product features without buzzwords. "
    "Generate a summary of 3–4 sentences with no bullets or headings, starting with “X years of experience in [relevant field or domain]” where X is a plain number with no brackets or symbols, "
    "calculated by subtracting the university graduation year from the current year, written in a narrative human tone, including relevant skills only when they strengthen alignment, and never copying text directly from the job description. "
    "Generate a skills section derived from work experience and JD-aligned pairings only, with no duplicated skills across categories, one line per category, and no qualifiers such as “familiar with.” "
    "For AI or GenAI-related jobs, organize skills in this strict order: Languages; AI (must be second, right after Languages); Frontend (ordered as frameworks, languages, state management, UI frameworks, HTML/CSS); "
    "Backend (ordered as frameworks, languages, API development, authentication, caching, microservices, event-driven systems, monitoring); Cloud & DevOps; Databases; "
    "Testing & CI/CD (include at least 8 unit or integration testing tools across frontend and backend); Other Tools & Technologies. "
    "For non-AI jobs, organize skills in this strict order: Languages; Frontend (ordered as frameworks, languages, state management, UI frameworks, HTML/CSS); "
    "Backend (ordered as frameworks, languages, API development, authentication, caching, microservices, event-driven systems, monitoring); Cloud & DevOps; Databases; "
    "Testing & CI/CD (include at least 8 unit or integration testing tools across frontend and backend); Other Tools & Technologies. "
    "Adjust skill depth per category based on role emphasis, with deeper frontend skills for frontend-heavy roles, deeper backend skills for backend-heavy roles, "
    "deeper AI or GenAI skills for AI roles, and deeper cloud and CI/CD skills for DevOps roles. "
    # ─── Per-company structure ────────────────────────────────────────────
    "PER-COMPANY STRUCTURE (strict): under each company's header line, render the following blocks in this order: "
    "(1) achievement bullet list (the 8/6 bullets above; counts apply to ACHIEVEMENT bullets only); "
    "(2) a 'Projects' subsection — a <strong>Projects</strong> label on its own line, then ONE project block per project listed in the candidate `projects` field for that company. "
    "PROJECT INPUT FORMAT: the candidate `projects` field is STRUCTURED BY COMPANY. Numbered company headings ('1. Apple', '2. Google', etc.) "
    "with project lines under each ('- Project Title: brief description.'). "
    "PROJECT COUNT (strict): render EXACTLY the same number of projects per company as listed in `projects`. If a company has 1 project listed, render 1. If 2, render 2. Never add, never drop, never infer extras. "
    "PROJECT ATTRIBUTION (strict): attribute each project block to the company named in the heading directly above it in `projects`. Do not re-attribute across companies. "
    "PROJECT BLOCK FORMAT: for each project, emit a sub-block with two parts: "
    "(a) the project title as a <strong>Project Title</strong> on its own line — use the title from the input VERBATIM (e.g. 'Apple Developer Program App'). The title line has NO dash prefix and is NOT inside a <ul>; render it as a standalone heading-style element (e.g. <div><strong>...</strong></div>). "
    "(b) a <ul> with exactly 2-3 <li> bullets describing what the candidate built and the impact on this specific project. Derive bullet content from the brief description in the input + the JD + work-experience context. Match the template's bullet convention exactly. Do NOT also include a separate one-sentence summary — the 2-3 bullets ARE the project description. "
    "If a company has no projects listed in `projects`, omit the Projects subsection entirely for that company (do not infer). "
    "Do not put project tech in the project line itself — tech belongs in the Tech stack subsection. "
    "(3) a 'Tech stack' subsection — a <strong>Tech stack</strong> label on its own line, then a single line of comma-separated technologies actually used at that company (no <ul>, no bullets, no categories). "
    "Limit Tech stack to 8-14 items per company; pick technologies that align with the JD and the projects + bullets you just wrote. "
    "PER-COMPANY ORDER: Header → Bullets → Projects → Tech stack. Match the template's spacing/style conventions for subsections. "
    "The top-level Skills section still applies and aggregates technologies across all companies — per-company Tech stack and top-level Skills coexist."
)


_TEMPLATE_HTML_WRAPPER = """<html>
  <head>
    <meta charset="utf-8" />
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Roboto:wght@400;500;700&family=Open+Sans:wght@400;600;700&family=Lato:wght@400;700&family=Montserrat:wght@400;500;700&family=Source+Sans+3:wght@400;600;700&family=Source+Serif+4:wght@400;600;700&family=Merriweather:wght@400;700&family=PT+Sans:wght@400;700&family=PT+Serif:wght@400;700&family=Nunito:wght@400;600;700&family=Raleway:wght@400;500;700&family=Poppins:wght@400;500;600;700&family=Work+Sans:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
      html, body {{ margin: 0; padding: 0; background: white; }}
      @media print {{ @page {{ size: A4; }} }}
    </style>
  </head>
  <body>{html_content}</body>
</html>"""


_S3_SAFE = re.compile(r"[^a-zA-Z0-9\-_]")


def _safe_segment(s: str) -> str:
    return _S3_SAFE.sub("-", (s or "user").strip())


_TEMPLATE_INSTRUCTION = (
    "STYLE MODE: HTML TEMPLATE.\n\n"
    "OUTPUT FORMAT (strict):\n"
    "1. First, emit the template's <style>...</style> block VERBATIM (no edits, no reformatting). "
    "If the template has no <style> block, skip step 1.\n"
    "2. Then emit the contents of the template's <body> element (NOT the <body> tag itself). "
    "Preserve every element, every class name, every inline style attribute, every nesting level, "
    "and every attribute value EXACTLY as in the template. Change ONLY text nodes (visible text) "
    "and href/src attribute VALUES (to point at the candidate's email/phone/links). "
    "Do not include <html>, <head>, or <body> tags — they are added by the wrapper.\n\n"
    "CONTENT RULES:\n"
    "- Replace placeholder text inside elements with the candidate's data from the `Candidate fields` JSON. "
    "Do not invent new sections or drop sections the template defines. If the candidate lacks data for a "
    "section the template has, render the section heading + empty placeholder text.\n"
    "- Match the template's bullet convention exactly (if it uses '- ' text prefix, keep that; if native "
    "<li> markers, keep that). This OVERRIDES the BULLET FORMAT rule earlier.\n"
    "- Preserve the exact number of company entries the template demonstrates as the structural pattern; "
    "extend by repeating the same DOM shape per actual company in candidate.experience. Use the most "
    "recent company entry as the structural prototype for newly-added companies.\n"
    "- Do NOT add a global <h1> if the template uses a different element for the candidate name; mimic "
    "what the template uses.\n"
    "- Do NOT introduce new class names. Do NOT change attribute names.\n\n"
    "HTML TEMPLATE:\n"
)


def _build_prompt(account: Account, job_description: str) -> str:
    # Contact items now live as a flat, generic list on the Account itself:
    # each entry is {display, url, show}. No semantic types — the LLM treats
    # `display` as the visible text and `url` as the href.
    contact_items = [
        {"label": ci.label, "display": ci.display, "url": ci.url}
        for ci in (account.contactItems or [])
        if ci.show and (ci.display or "").strip()
    ]

    candidate_fields = {
        "name": account.name or "",
        "contactItems": contact_items,
        "education": account.education or "",
        "experience": account.experience or "",
        "projects": account.projects or "",
    }
    candidate_block = (
        "Candidate fields. `contactItems` is a flat list of {label, display, url}. "
        "`label` is a short type hint (e.g. 'Email', 'GitHub', 'Phone') — useful "
        "for picking the right icon or fallback semantics when the template "
        "expects them. RENDER each item's `display` value as visible text in the "
        "contact header; use `url` as the href when wrapping in <a> tags. Skip "
        "items with empty `display`. Do NOT pull location text from "
        "education/experience into anything else:\n"
        + json.dumps(candidate_fields, ensure_ascii=False, indent=2)
    )

    # Header allowlist is the contactItems order itself (only show=True entries
    # are passed in). Tell the LLM the order is authoritative.
    header_override = (
        "HEADER OVERRIDE (supersedes any earlier 'header allowlist' rule): "
        "The contact header must include exactly the items from `contactItems` "
        "in the order given. Use each item's `display` as text and `url` as href. "
        "Do not add fields that aren't in the list."
    )

    # Auto-title — when account.titleAutoFromJD is true, LLM derives the
    # header title from the JD. Static account.title becomes a seniority hint.
    title_block = ""
    if getattr(account, "titleAutoFromJD", False):
        preferred_title = (account.title or "").strip()
        title_block = (
            "TITLE OVERRIDE (highest priority for the header title): "
            "Derive the title shown under the candidate's name FROM THE JOB DESCRIPTION. "
            "Pick the most-aligned seniority + role label (e.g. 'Senior Software Engineer', "
            "'Staff Backend Engineer', 'Engineering Manager', 'Principal AI Engineer'). "
            "3-6 words max. No company names. No buzzwords ('rockstar', 'ninja', '10x'). "
            f"Candidate's preferred title is '{preferred_title or 'unspecified'}' — use it ONLY "
            "as a seniority hint; the JD-aligned label wins. If the resume uses an HTML "
            "template, replace the title element's text with the JD-derived label while "
            "preserving its structure/classes/styles."
        )
    elif (account.title or "").strip():
        # Static title — tell LLM to use it verbatim in the header.
        title_block = (
            "TITLE OVERRIDE (highest priority for the header title): "
            f"Use the title text \"{(account.title or '').strip()}\" exactly as-is under "
            "the candidate's name. Do not infer or modify it from the JD."
        )

    # Template mode is now the only style path. Inject the template inline as
    # the structural source-of-truth; everything visual (bullet glyph, indent,
    # spacing, section headings) is owned by the template.
    from app.html_sanitize import sanitize_template_html
    template_block = _TEMPLATE_INSTRUCTION + sanitize_template_html(account.styleTemplate or "")

    preamble = (
        "You render the resume's body inner-HTML. You MAY also emit a leading "
        "<style>...</style> tag (copied from the template). Do NOT emit "
        "<html>, <head>, or <body> tags — the wrapper adds them.\n"
    )

    parts = [
        "Forget anything you learned before.",
        preamble,
        candidate_block,
        f"Here's the job description. {job_description}\n",
        _PROMPT_BODY.format(name=account.name),
        header_override,
        title_block,
        template_block,
    ]
    return "\n".join(p for p in parts if p)


@dataclass
class GeneratedResume:
    pdf_bytes: bytes
    s3_key: Optional[str]


async def _openai_html(prompt: str) -> str:
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY not configured",
        )
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    # The Express service used the Responses API with `output_text`. The Python
    # SDK exposes the same surface via `client.responses.create(...).output_text`.
    response = await client.responses.create(
        model=settings.RESUME_OPENAI_MODEL,
        input=prompt,
    )
    return response.output_text or ""


_DEFAULT_MARGIN_PT = {"top": 35, "right": 30, "bottom": 30, "left": 30}


def _resolve_margin_pt(account: Account) -> dict:
    """Template now owns visual layout including its own @page margins (set
    via CSS inside the template). Playwright still needs a margin dict for
    the PDF call, so use defaults — the template's CSS @page rules will
    override these visually."""
    return {
        "top": _DEFAULT_MARGIN_PT["top"],
        "right": _DEFAULT_MARGIN_PT["right"],
        "bottom": _DEFAULT_MARGIN_PT["bottom"],
        "left": _DEFAULT_MARGIN_PT["left"],
    }


_PT_TO_MM = 25.4 / 72  # 1pt = 0.3528mm


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
            # Playwright accepts px, in, cm, mm — not pt. Convert pt → mm.
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


def _s3_client():
    import boto3
    from botocore.config import Config

    s = get_settings()
    # Force SigV4 + virtual-host addressing so presigned URLs match the host
    # boto3 actually signs against. Mixing global vs regional endpoints
    # produces SignatureDoesNotMatch on non-us-east-1 buckets.
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
    """Short-lived URL for client download. Bucket stays private.
    `download_filename` injects ResponseContentDisposition so the browser
    saves with the right name when the URL is opened directly (anchor click)."""
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
    """Upload to S3 when configured. Returns (key, region) or (None, None).
    Raises on failure when S3 IS configured — caller treats as job failure
    rather than silently dropping the PDF."""
    settings = get_settings()
    bucket = settings.AWS_S3_BUCKET
    region = settings.AWS_REGION
    if not bucket or not region:
        return None, None

    s3 = _s3_client()
    # {YYYY_MM_DD}/{Profile}/{Company}/Resume.pdf — date first so daily
    # listings group naturally; profile/company subfolders for browsing.
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


async def generate_resume(
    account: Account,
    job_description: str,
    company: str,
) -> GeneratedResume:
    prompt = _build_prompt(account, job_description)
    log.info("Generating resume for account=%s (%s) company=%s", account.title or account.name, account.id, company)
    html_content = await _openai_html(prompt)
    # Template mode is the only path: minimal wrapper, template owns visuals.
    html = _TEMPLATE_HTML_WRAPPER.format(html_content=html_content)
    pdf_bytes = await _html_to_pdf(
        html,
        page_format=account.pageFormat or "A4",
        margin_pt=_resolve_margin_pt(account),
    )
    s3_key, _region = _maybe_upload_to_s3(pdf_bytes, account.name, company)
    return GeneratedResume(pdf_bytes=pdf_bytes, s3_key=s3_key)
