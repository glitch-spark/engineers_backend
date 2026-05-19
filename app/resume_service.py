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
    "Generate ATS-optimized, job-tailored resume CONTENT for the candidate, aligned to the job description. "
    "Do NOT modify company names, work-experience locations, or employment dates — copy them verbatim "
    "from the candidate `experience` field into `companyName` / `originalRole` / `location` / `period`. "
    "Per company, `tailoredRole` is a JD-aligned reinterpretation shown on the resume; see EXPERIENCE ROLE rules. "
    # ─── Achievement bullets ──────────────────────────────────────────────
    "ACHIEVEMENT BULLETS: the most recent company's `bullets` array has exactly 8 entries; every other company has exactly 6. "
    "For the most recent company, structure the 8 bullets as: 1 bullet on a realistic core product/platform developed there, "
    "6 bullets on core engineering work, 1 bullet on cross-functional collaboration + leadership. "
    "Distribute the 6 core engineering bullets by job type: "
    "pure AI/GenAI role = 1 frontend / 1 backend / 4 AI; AI-leaning full-stack = 2/2/2; "
    "frontend role = 4 frontend / 1 backend / 1 DevOps; backend role = 1/4/1; "
    "frontend-leaning full-stack = 3/2/1; backend-leaning full-stack = 2/3/1; "
    "DevOps-leaning full-stack = 1 frontend / 2 backend / 3 DevOps (AWS or Azure only if realistic for the company). "
    "Each bullet is plain text, work details only, no category labels, no repetition, metrics only when meaningful, no buzzwords. "
    # ─── Summary ──────────────────────────────────────────────────────────
    "SUMMARY: 3-4 sentences, narrative tone, opens with \"X years of experience in [field]\" where X is a plain number "
    "(current year minus university graduation year). Never copy text from the job description. "
    # ─── Skills ───────────────────────────────────────────────────────────
    "SKILLS: derive from work experience + JD-aligned pairings. No skill duplicated across categories. No qualifiers like 'familiar with'. "
    "Each `skills` entry = one category {category, items}. "
    "For AI/GenAI jobs, category order: Languages; AI (2nd); Frontend; Backend; Cloud & DevOps; Databases; Testing & CI/CD (8+ tools); Other Tools & Technologies. "
    "For non-AI jobs: Languages; Frontend; Backend; Cloud & DevOps; Databases; Testing & CI/CD (8+ tools); Other Tools & Technologies. "
    "Deeper skills in the category the role emphasizes. "
    # ─── Projects ─────────────────────────────────────────────────────────
    "PROJECTS: the candidate `projects` field is structured by company — numbered company headings ('1. Apple', '2. Google') "
    "with project lines under each ('- Project Title: brief description.'). "
    "For each company's `projects` array, emit EXACTLY the projects listed under that company heading — never add, drop, or infer extras. "
    "Attribute each project to the company named in the heading directly above it. If a company has no projects listed, its `projects` array is empty. "
    "Each project = {title, bullets}: `title` is the project title VERBATIM from the input; `bullets` is exactly 2-3 plain-text bullets "
    "describing what was built and the impact (from the input brief + JD + experience context). Project tech goes in the company `techStack`, not in project bullets. "
    # ─── Tech stack ───────────────────────────────────────────────────────
    "TECH STACK: each company's `techStack` is a comma-separated line of 8-14 technologies actually used there, JD-aligned, "
    "consistent with that company's bullets + projects."
)


_S3_SAFE = re.compile(r"[^a-zA-Z0-9\-_]")


def _safe_segment(s: str) -> str:
    return _S3_SAFE.sub("-", (s or "user").strip())


_EXPERIENCE_ROLE_RULES = (
    "EXPERIENCE ROLE (every experience entry):\n"
    "- `originalRole`: verbatim job title from the candidate `experience` field.\n"
    "- `tailoredRole`: JD-aligned reinterpretation for the resume header under each "
    "company — shift seniority wording and role nouns toward the target role "
    "(e.g. 'Senior Fullstack Developer' → 'Senior Software Quality Engineer' when "
    "the JD is QA/SDET). Stay anchored to work the candidate actually did; never "
    "inflate seniority more than one level or invent a domain they did not touch.\n"
    "- When the target role classification differs from the candidate's original "
    "domain, `originalRole` and `tailoredRole` must NOT be byte-identical — at "
    "minimum normalize seniority ('Sr' → 'Senior') and shift the role noun toward "
    "target vocabulary.\n"
    "- If work at a company cannot support the target role, keep `tailoredRole` close "
    "to `originalRole` and reflect the target in bullets/skills instead."
)

_CONTENT_OUTPUT_SCHEMA = (
    "OUTPUT FORMAT (strict): Return ONLY a JSON object — no prose, no markdown, "
    "no code fences. The server fills an HTML template deterministically from "
    "this JSON, so the shape must be exact. Schema:\n"
    "{\n"
    '  "title": "<header subtitle — see TITLE rule below>",\n'
    '  "summary": "<the summary paragraph, plain text, no bullets>",\n'
    '  "experience": [\n'
    "    {\n"
    '      "companyName": "<verbatim from candidate experience>",\n'
    '      "originalRole": "<verbatim role title from candidate input>",\n'
    '      "tailoredRole": "<JD-aligned reinterpretation; MUST NOT equal originalRole when target classification differs from original domain>",\n'
    '      "location": "<verbatim from candidate experience; empty string if not provided>",\n'
    '      "period": "<verbatim>",\n'
    '      "bullets": ["<achievement bullet>", "..."],\n'
    '      "projects": [{"title": "<verbatim project title>", "bullets": ["<2-3 bullets>"]}],\n'
    '      "techStack": "<comma-separated technologies for this company, 8-14 items>"\n'
    "    }\n"
    "  ],\n"
    '  "skills": [{"category": "<label>", "items": ["<skill>", "<skill>", "..."]}],\n'
    '  "education": [{"school": "<name>", "degree": "<degree>", "eduPeriod": "<dates>"}]\n'
    "}\n"
    "FIELD NOTES:\n"
    "- `originalRole` and `tailoredRole` are BOTH required on every experience entry. "
    "The renderer displays tailoredRole; originalRole is kept for debug/audit. "
    "They must NOT be byte-identical when the target role's classification differs "
    "from the candidate's original domain — at minimum normalize seniority ('Sr' → 'Senior') "
    "and shift the role noun toward the target vocabulary.\n"
    "- `skills[].items` is a JSON ARRAY of strings, NOT a comma-separated string.\n"
    "- `projects` is NESTED inside each experience entry — there is no top-level projects array. "
    "Project count per company must match exactly what the candidate `projects` field lists "
    "for that company; emit `\"projects\": []` when none listed.\n"
    "- Bullet text is PLAIN TEXT — no leading dash, no bullet glyph, no HTML. The "
    "template supplies the bullet marker.\n"
    "- Company order: most recent first."
)


def _normalize_experience_entry(exp: dict) -> dict:
    """Map LLM experience objects to the template-fill shape.

    Display role prefers tailoredRole; keeps originalRole for audit. Accepts
    legacy single-field `role` from older prompts/models.
    """
    original = (exp.get("originalRole") or exp.get("role") or "").strip()
    tailored = (exp.get("tailoredRole") or "").strip()
    if not tailored:
        tailored = original
    out = dict(exp)
    out["originalRole"] = original
    out["tailoredRole"] = tailored
    out["role"] = tailored
    if "location" not in out:
        out["location"] = ""
    return out


def _normalize_skills(skills: list) -> list:
    out = []
    for sk in skills or []:
        if not isinstance(sk, dict):
            continue
        items = sk.get("items", [])
        if isinstance(items, list):
            items_str = ", ".join(str(i).strip() for i in items if str(i).strip())
        else:
            items_str = str(items or "").strip()
        out.append({**sk, "items": items_str})
    return out


def _build_content_prompt(
    account: Account,
    job_description: str,
    prompt_body: Optional[str] = None,
) -> str:
    """Build the prompt that asks the LLM for resume CONTENT as JSON.
    The LLM never sees or emits HTML — `template_fill.fill_template` renders
    the JSON into the annotated template deterministically.

    `prompt_body` = resolved content-contract text. None → built-in default.
    """
    candidate_fields = {
        "name": account.name or "",
        "education": account.education or "",
        "experience": account.experience or "",
        "projects": account.projects or "",
    }
    candidate_block = (
        "Candidate fields (source material — structure + tailor these into the "
        "output JSON; do NOT pull location text across fields):\n"
        + json.dumps(candidate_fields, ensure_ascii=False, indent=2)
    )

    # Title rule — into the JSON `title` field.
    if getattr(account, "titleAutoFromJD", False):
        preferred = (account.title or "").strip()
        title_rule = (
            "TITLE: derive the `title` field FROM THE JOB DESCRIPTION — the most "
            "aligned seniority + role label (3-6 words, e.g. 'Senior Software "
            f"Engineer'). Candidate's preferred title '{preferred or 'unspecified'}' "
            "is a seniority hint only; the JD-aligned label wins. No buzzwords."
        )
    else:
        title_rule = (
            f"TITLE: set the `title` field to exactly \"{(account.title or '').strip()}\" "
            "(verbatim — do not infer or modify)."
        )

    body = (prompt_body or "").strip() or _PROMPT_BODY

    parts = [
        "Forget anything you learned before.",
        "You produce resume CONTENT as a JSON object. You do NOT write HTML.",
        candidate_block,
        f"Here's the job description. {job_description}\n",
        body,
        title_rule,
        _EXPERIENCE_ROLE_RULES,
        _CONTENT_OUTPUT_SCHEMA,
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
            # Wait for web fonts (Google Fonts <link>) to finish loading before
            # we snapshot to PDF; otherwise Chromium can rasterize with the
            # system fallback (Arial), altering letter-spacing + metrics.
            try:
                await page.evaluate("document.fonts.ready")
            except Exception:
                pass
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


def default_prompt_body() -> str:
    """The built-in resume content-contract prompt. Exported to users so they
    can start from it and customize."""
    return _PROMPT_BODY


_SCREENING_PROMPT_BODY = (
    "Answer each numbered screening question in the candidate's voice. "
    "Be concise, specific, and grounded in the provided context (resume / "
    "experience / job description). Lead with concrete examples. Keep answers "
    "tight — no filler, no restating the question. If a question cannot be "
    "answered from the context, say so briefly rather than fabricating details."
)


def default_screening_prompt_body() -> str:
    """The built-in screening-answer prompt. Exported so users can start from
    it and customize. Account/user-level overrides replace it verbatim."""
    return _SCREENING_PROMPT_BODY


def _strip_fences(s: str) -> str:
    """Drop ```json … ``` fences if the model wrapped its JSON."""
    s = (s or "").strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


_GOOGLE_FONTS_LINK = (
    '<link href="https://fonts.googleapis.com/css2?'
    'family=Inter:wght@400;500;600;700&family=Roboto:wght@400;500;700'
    '&family=Open+Sans:wght@400;600;700&family=Lato:wght@400;700'
    '&family=Montserrat:wght@400;500;700&family=Source+Sans+3:wght@400;600;700'
    '&family=Source+Serif+4:wght@400;600;700&family=Merriweather:wght@400;700'
    '&family=PT+Sans:wght@400;700&family=PT+Serif:wght@400;700'
    '&family=Nunito:wght@400;600;700&family=Raleway:wght@400;500;700'
    '&family=Poppins:wght@400;500;600;700&family=Work+Sans:wght@400;500;700'
    '&display=swap" rel="stylesheet">'
)


def _ensure_fonts_link(html: str) -> str:
    """Inject the Google Fonts <link> into <head> if absent, so the template's
    web fonts resolve during the Playwright render."""
    if "fonts.googleapis.com" in html:
        return html
    if "<head>" in html:
        return html.replace("<head>", "<head>" + _GOOGLE_FONTS_LINK, 1)
    if "<html>" in html:
        return html.replace("<html>", "<html><head>" + _GOOGLE_FONTS_LINK + "</head>", 1)
    return _GOOGLE_FONTS_LINK + html


def _build_content(account: Account, llm_json: dict) -> dict:
    """Merge LLM-generated content with account-owned fields (name + contact
    items) into the shape `template_fill.fill_template` expects."""
    contact_items = [
        {"display": ci.display, "url": ci.url}
        for ci in (account.contactItems or [])
        if ci.show and (ci.display or "").strip()
    ]
    raw_experience = llm_json.get("experience") or []
    experience = [
        _normalize_experience_entry(e)
        for e in raw_experience
        if isinstance(e, dict)
    ]
    return {
        "name": account.name or "",
        "title": (llm_json.get("title") or account.title or "").strip(),
        "summary": llm_json.get("summary") or "",
        "contactItems": contact_items,
        "experience": experience,
        "skills": _normalize_skills(llm_json.get("skills") or []),
        "education": llm_json.get("education") or [],
    }


async def generate_resume(
    account: Account,
    job_description: str,
    company: str,
    prompt_body: Optional[str] = None,
) -> GeneratedResume:
    from app.template_fill import fill_template, validate_annotated

    annotated = (account.styleTemplateAnnotated or "").strip()
    if not annotated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No annotated template. Upload a template and run 'Convert to template' first.",
        )
    missing_req, _ = validate_annotated(annotated)
    if missing_req:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Annotated template is missing required markers: " + ", ".join(missing_req),
        )

    log.info("Generating resume for account=%s (%s) company=%s",
             account.title or account.name, account.id, company)

    # ---- step 1: LLM → JSON content (no HTML) ----
    prompt = _build_content_prompt(account, job_description, prompt_body=prompt_body)
    raw = await _openai_html(prompt)
    try:
        llm_json = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        log.error("resume content JSON parse failed: %s\nraw=%s", exc, raw[:500])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Resume content generation returned malformed JSON.",
        )

    # ---- step 2: deterministic fill into the annotated template ----
    content = _build_content(account, llm_json)
    filled = fill_template(annotated, content)
    html = _ensure_fonts_link(filled)

    # ---- step 3: HTML → PDF ----
    pdf_bytes = await _html_to_pdf(
        html,
        page_format=account.pageFormat or "A4",
        margin_pt=_resolve_margin_pt(account),
    )
    s3_key, _region = _maybe_upload_to_s3(pdf_bytes, account.name, company)
    return GeneratedResume(pdf_bytes=pdf_bytes, s3_key=s3_key)
