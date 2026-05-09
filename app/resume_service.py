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
    "Contact header must contain ONLY these candidate fields when non-empty: name, email, phone, address. "
    "Do NOT derive a city, country, region, or location from education, work experience, university name, or company name. "
    "If address is empty, omit address entirely from the header — never infer one. "
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
    "For resume styling, ensure work experience bullet points are rendered with a consistent visual indent equivalent to a 10px left offset relative to the company header, "
    "using list indentation rather than margin-left declarations, and apply this consistently across all companies."
)


_HTML_WRAPPER = """<html>
  <head>
    <!-- Web fonts users may pick in the structured editor. Chromium fetches
         these at render time (page.set_content waits for networkidle).
         System fonts (Arial / Tahoma / Verdana / Times New Roman / Courier)
         come from the Docker image's msttcorefonts + Liberation install. -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Roboto:wght@400;500;700&family=Open+Sans:wght@400;600;700&family=Lato:wght@400;700&family=Montserrat:wght@400;500;700&family=Source+Sans+3:wght@400;600;700&family=Source+Serif+4:wght@400;600;700&family=Merriweather:wght@400;700&family=PT+Sans:wght@400;700&family=PT+Serif:wght@400;700&family=Nunito:wght@400;600;700&family=Raleway:wght@400;500;700&family=Poppins:wght@400;500;600;700&family=Work+Sans:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
      * {{
        margin: 0;
        padding: 0;
        box-sizing: border-box;
      }}
      ul, li {{
        margin: 0;
        padding: 0;
        box-sizing: border-box;
      }}
      body {{
        background: white;
        line-height: 1.3 !important;
        margin: 0;
        padding: 0;
      }}
      ul {{
        list-style-type: disc !important;
        list-style-position: outside !important;
        padding-left: 1em !important;
        margin: 0 !important;
      }}
      li {{
        display: list-item !important;
        list-style-type: disc !important;
        list-style-position: outside !important;
        margin: 0 !important;
        padding: 0 !important;
      }}
      * ul {{ list-style-type: disc !important; }}
      * li {{ list-style-type: disc !important; display: list-item !important; }}
      .page-break {{ page-break-before: always; break-before: page; }}
      @media print {{
        @page {{ margin: 35pt 30pt 30pt 30pt; size: A4; }}
        html, body {{ margin: 0; padding: 0; }}
      }}
    </style>
  </head>
  <body>{html_content}</body>
</html>"""


_S3_SAFE = re.compile(r"[^a-zA-Z0-9\-_]")


def _safe_segment(s: str) -> str:
    return _S3_SAFE.sub("-", (s or "user").strip())


def _resolve_style_spec(account: Account) -> str:
    """Build the prose style spec for the LLM prompt.

    Markdown is canonical. Flow:
      1. Read styleMarkdown (fallback: legacy `styles`, then styleConfig).
      2. Parse @style tokens → structured config dict; deep-merge over the
         account's stored styleConfig so token coverage gaps don't blow away
         the editor's last-saved values.
      3. Run to_prose() on merged config → strict LLM-contract prose.
      4. Append any free-form user prose (notes / inline guidance) verbatim.
    """
    from app.style_serializer import to_prose
    from app.style_tokens import deep_merge, free_prose, parse_tagged_markdown

    md = account.styleMarkdown or account.styles or ""
    parsed_cfg, _notes, token_count = parse_tagged_markdown(md)

    base_cfg = account.styleConfig if isinstance(account.styleConfig, dict) else {}
    merged_cfg = deep_merge(base_cfg, parsed_cfg) if token_count else base_cfg

    # If user has pure-prose markdown (no tokens, no styleConfig), ship the
    # prose raw — same as the legacy markdown-only path.
    if not merged_cfg and md.strip():
        return free_prose(md)

    prose = to_prose(merged_cfg or {})
    user_prose = free_prose(md)
    if user_prose:
        prose = (
            f"{prose}\n\n"
            f"ADDITIONAL USER STYLE NOTES (apply alongside the rules above):\n"
            f"{user_prose}"
        )
    return prose


def _build_prompt(account: Account, job_description: str) -> str:
    # Ship candidate fields as discrete labeled slots, not free-form sentences.
    # Prevents the model from bleeding education location (e.g. "Singapore
    # University") into the contact-header address slot.
    candidate_fields = {
        "name": account.name or "",
        "email": account.email or "",
        "phone": account.phone or "",
        "address": account.address or "",
        "education": account.education or "",
        "experience": account.experience or "",
    }
    candidate_block = (
        "Candidate fields (treat each key as an isolated slot — do NOT copy "
        "values across keys, especially do not pull location text from "
        "education/experience into address):\n"
        + json.dumps(candidate_fields, ensure_ascii=False, indent=2)
    )

    # Length cap — soft hint to LLM. Bullet-count rules above are still
    # authoritative; this nudges layout density and skills depth.
    target_pages = getattr(account, "targetPages", None)
    length_block = ""
    if isinstance(target_pages, int) and target_pages > 0:
        if target_pages == 1:
            length_block = (
                "TARGET LENGTH: Output must comfortably fit a SINGLE A4 page at 11pt body. "
                "If candidate has 4+ companies, drop the oldest companies' bullets to "
                "exactly 4 and tighten skills depth. Do not violate the bullet-count rules "
                "for the most recent company."
            )
        else:
            length_block = (
                f"TARGET LENGTH: Output should target {target_pages} A4 pages at 11pt body. "
                "Use the full bullet count and skills depth as specified."
            )

    # Per-account override block — appended last, treated as highest priority.
    overrides = (account.promptOverrides or "").strip()
    overrides_block = (
        f"OVERRIDES (highest priority — supersede any earlier rule that conflicts):\n{overrides}"
        if overrides
        else ""
    )

    parts = [
        "Forget anything you learned before.",
        "You render the resume in html format(only body element).\n",
        candidate_block,
        f"Here's the job description. {job_description}\n",
        _PROMPT_BODY.format(name=account.name),
        length_block,
        "2. Resume Formatting Styles: Exactly follow the styles below.",
        _resolve_style_spec(account),
        overrides_block,
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
    """Pull `styleConfig.page.margin` (set by the structured editor) so the PDF
    matches the on-screen preview. Falls back to the legacy defaults when the
    user is in markdown mode or hasn't authored a styleConfig yet."""
    cfg = account.styleConfig or {}
    page = cfg.get("page") if isinstance(cfg, dict) else None
    margin = page.get("margin") if isinstance(page, dict) else None
    if not isinstance(margin, dict):
        return _DEFAULT_MARGIN_PT
    return {
        "top": margin.get("top", _DEFAULT_MARGIN_PT["top"]),
        "right": margin.get("right", _DEFAULT_MARGIN_PT["right"]),
        "bottom": margin.get("bottom", _DEFAULT_MARGIN_PT["bottom"]),
        "left": margin.get("left", _DEFAULT_MARGIN_PT["left"]),
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
    log.info("Generating resume for account=%s (%s) company=%s", account.label or account.name, account.id, company)
    html_content = await _openai_html(prompt)
    html = _HTML_WRAPPER.format(html_content=html_content)
    pdf_bytes = await _html_to_pdf(
        html,
        page_format=account.pageFormat or "A4",
        margin_pt=_resolve_margin_pt(account),
    )
    s3_key, _region = _maybe_upload_to_s3(pdf_bytes, account.name, company)
    return GeneratedResume(pdf_bytes=pdf_bytes, s3_key=s3_key)
