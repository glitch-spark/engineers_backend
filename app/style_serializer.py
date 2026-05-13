"""Serialize a structured `styleConfig` JSON into the prose-format style spec
fed to the LLM. Mirrors the TypeScript `StyleConfig` shape in
engineersapp/src/lib/resumeStyles.ts.

Both the structured editor and this serializer must stay in lockstep — the LLM
contract depends on the resulting prose looking like the legacy hand-authored
spec. Golden tests in tests/test_style_serializer.py guard that.
"""

from __future__ import annotations

from typing import Any, Dict


_PREAMBLE = (
    "Generate HTML only for a professional resume. Do not include explanations, comments, "
    "Markdown, or code fences. GLOBAL RULES (STRICT): Output HTML only. Use <strong> for bold "
    "text only. Never output Markdown symbols (**, _, __). Never use double spaces anywhere. "
    "Always use exactly one space on both sides of the | separator: \"A | B | C\". "
    "Never omit spaces around |. Do not insert extra whitespace at line breaks. Do not insert "
    "blank lines between elements. All spacing must be controlled explicitly via CSS styles. "
    "Do not use line-height anywhere. All <ul> and <li> elements must have margin: 0; padding: 0;. "
    "Do not use tables, grids, or borders."
)


def _typo(t: Dict[str, Any], align: str | None = None) -> str:
    """Render a Typography block to a CSS-ish prose fragment."""
    parts = [
        f"font {t.get('fontFamily', 'Tahoma')}",
        f"{t.get('fontSize', 11)}pt",
    ]
    if t.get("fontWeight") == "bold":
        parts.append("bold")
    if align:
        parts.append(f"{align} aligned")
    color = t.get("color")
    if color and color.lower() not in ("#000", "#000000", "black"):
        parts.append(f"color {color}")
    return ", ".join(parts)


def _section_label(key: str) -> str:
    return {
        "summary": "SUMMARY",
        "experience": "PROFESSIONAL EXPERIENCE",
        "skills": "SKILLS",
        "education": "EDUCATION",
    }.get(key, key.upper())


def to_prose(cfg: Dict[str, Any]) -> str:
    """Convert a styleConfig dict into the prose spec the LLM follows."""
    lines: list[str] = [_PREAMBLE]

    # PAGE LAYOUT
    margin = cfg.get("page", {}).get("margin", {})
    lines.append(
        "PAGE LAYOUT: Page margins are handled by the PDF generator "
        f"({margin.get('top', 35)}pt top, {margin.get('right', 30)}pt right, "
        f"{margin.get('bottom', 30)}pt bottom, {margin.get('left', 30)}pt left). "
        "Do not add any body-level margins or padding in the generated HTML."
    )

    # SECTION SPACING
    spacing = cfg.get("sectionSpacing", {})
    lines.append(
        "SECTION SPACING SYSTEM (AUTHORITATIVE): "
        f"Each top-level section container must have margin-bottom: {spacing.get('containerMb', 8)}px. "
        f"Section heading margin-bottom: {spacing.get('headingMb', 4)}px. "
        f"Section divider margin-bottom: {spacing.get('dividerMb', 6)}px. "
        "All other elements margin 0."
    )

    # HEADER (Basic Info)
    bi = cfg.get("basicInfo", {})
    if bi:
        name = bi.get("name", {})
        title = bi.get("title", {})
        contact = bi.get("contact", {})
        # Render the enabled contact items live so the LLM emits the exact
        # row the user configured (github/linkedin/website/etc.) — not the
        # hardcoded "Email | Phone | Address" template.
        items = contact.get("items") or []
        sep = contact.get("separator", "|")
        enabled = [
            (it.get("customLabel") or it.get("type", ""))
            if it.get("type") == "custom"
            else it.get("type", "")
            for it in items
            if it.get("enabled")
        ]
        if not enabled:
            enabled = ["email", "phone", "address"]
        contact_template = f" {sep} ".join(enabled)
        lines.append(
            f"HEADER: Name must use {_typo(name, name.get('align', 'center'))}, margin 0. "
            f"Title ({title.get('text', 'Senior Software Engineer')}) must use "
            f"{_typo(title, title.get('align', 'center'))}, margin 0. "
            f"Contact line ({contact_template}) must use "
            f"{_typo(contact, contact.get('align', 'center'))}, "
            f"margin-bottom {spacing.get('containerMb', 8)}px. "
            f"Render each item in the order shown above, separated by ' {sep} '. "
            f"Use ONLY the items listed; do not add or reorder fields."
        )

    # SECTION HEADINGS
    sh = cfg.get("sectionHeading")
    if sh:
        lines.append(
            "SECTION HEADINGS (SUMMARY, PROFESSIONAL EXPERIENCE, SKILLS, EDUCATION): "
            f"{_typo(sh, sh.get('align', 'center'))}, margin-top 0, "
            f"margin-bottom {spacing.get('headingMb', 4)}px."
        )

    # SECTION DIVIDER
    lines.append(
        f"SECTION DIVIDER: Solid black horizontal line, height 0.5pt, width 100%, "
        f"margin-top 0, margin-bottom {spacing.get('dividerMb', 6)}px."
    )

    # SUMMARY
    summary = cfg.get("summary")
    if summary:
        bold_clause = (
            "highlight category labels using <strong>"
            if summary.get("boldLabels", True)
            else "do not bold any category labels"
        )
        lines.append(
            f"SUMMARY CONTENT: {_typo(summary, summary.get('align', 'justify'))}, "
            f"margin 0, padding 0, {bold_clause}."
        )

    # PROFESSIONAL EXPERIENCE
    exp = cfg.get("experience")
    if exp:
        sep = exp.get("separator", "|")
        cn = exp.get("companyName", {})
        role = exp.get("role", {})
        period = exp.get("period", {})
        period_fmt = period.get("format", "YYYY/MM - YYYY/MM")
        lines.append(
            f"PROFESSIONAL EXPERIENCE: Company line (Company Name {sep} Role {sep} Period) must be "
            f"on a single line, left aligned, margin-top 4pt, margin-bottom 4pt, using {sep} as divider. "
            f"Company name must use {_typo(cn)}. Role must use {_typo(role)}. "
            f"Period must use {_typo(period)}, format {period_fmt}."
        )
        bullet = exp.get("bullet", {})
        bold_kw = (
            "Highlight keywords and skills using <strong>"
            if bullet.get("boldKeywords", True)
            else "Do not bold keywords"
        )
        lines.append(
            f"WORK EXPERIENCE BULLETS: {_typo(bullet, bullet.get('align', 'justify'))}, "
            f"margin 0, padding 0, standard bullets. Each <li> must have "
            f"margin-left: {bullet.get('indentPx', 10)}px. {bold_kw}."
        )

    # SKILLS
    skills = cfg.get("skills")
    if skills:
        layout = skills.get("layout", "one-per-line")
        layout_clause = (
            "one line per category"
            if layout == "one-per-line"
            else "categories joined on one line, separated by commas"
        )
        bold_cats = (
            "highlight skill categories using <strong>"
            if skills.get("boldCategories", True)
            else "do not bold skill categories"
        )
        lines.append(
            f"SKILLS CONTENT: {_typo(skills, skills.get('align', 'justify'))}, "
            f"margin 0, padding 0, {layout_clause}, {bold_cats}."
        )

    # EDUCATION
    edu = cfg.get("education")
    if edu:
        sep = edu.get("separator", "|")
        uni = edu.get("university", {})
        deg = edu.get("degree", {})
        ep = edu.get("period", {})
        ep_fmt = ep.get("format", "YYYY - YYYY")
        lines.append(
            f"EDUCATION: Education line (University {sep} Degree {sep} Period) must be on a "
            f"single line, left aligned, using {sep} as divider. "
            f"University must use {_typo(uni)}. Degree must use {_typo(deg)}. "
            f"Period must use {_typo(ep)}, format {ep_fmt}."
        )

    # SECTION ORDER
    order = cfg.get("sectionOrder") or ["summary", "experience", "skills", "education"]
    arrow_chain = " → ".join(_section_label(k) for k in order)
    lines.append(f"SECTION ORDER (STRICT): {arrow_chain}.")

    lines.append(
        "FINAL ENFORCEMENT: Do not reintroduce line-height. Do not invent styles. "
        "Do not add extra margins or padding. Do not output Markdown. Generate clean, "
        "deterministic HTML suitable for PDF rendering."
    )

    return "\n".join(lines)
