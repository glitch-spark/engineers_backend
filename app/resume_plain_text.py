"""Structured plain-text resume export in logical reading order.

Use this instead of copy-pasting from PDFs — Chromium PDF text extraction
follows glyph positions and breaks on designer templates (letter-spacing,
icons, multi-column layout).
"""

from __future__ import annotations

import re
from typing import Any

_BULLET_LINE = "• "

# Strip leading decorative glyphs if they ever appear in content strings.
_LEADING_GLYPH_RE = re.compile(r"^\s*[-–—•●◦▸►▪✉✆☎📧*]+\s*")


def strip_decorative_prefix(s: str) -> str:
    """Remove leading bullet/icon glyphs from a single line of text."""
    return _LEADING_GLYPH_RE.sub("", (s or "").strip())


def _clean_line(s: str) -> str:
    return strip_decorative_prefix(s)


def _skill_items_text(skill: dict) -> str:
    items = skill.get("items", "")
    if isinstance(items, list):
        return ", ".join(str(i).strip() for i in items if str(i).strip())
    return str(items or "").strip()


def content_to_plain_text(content: dict[str, Any]) -> str:
    """Render filled resume `content` (template_fill shape) as plain text."""
    lines: list[str] = []

    name = _clean_line(content.get("name", ""))
    title = _clean_line(content.get("title", ""))
    if name:
        lines.append(name)
    if title:
        lines.append(title)

    contacts = content.get("contactItems") or []
    contact_parts = [
        _clean_line(c.get("display", ""))
        for c in contacts
        if isinstance(c, dict) and _clean_line(c.get("display", ""))
    ]
    if contact_parts:
        lines.append(" | ".join(contact_parts))

    if lines:
        lines.append("")

    summary = _clean_line(content.get("summary", ""))
    if summary:
        lines.append("Summary")
        lines.append(summary)
        lines.append("")

    skills = content.get("skills") or []
    if skills:
        lines.append("Technical Skills")
        for sk in skills:
            if not isinstance(sk, dict):
                continue
            cat = _clean_line(sk.get("category", ""))
            items = _skill_items_text(sk)
            if cat and items:
                lines.append(f"{cat}: {items}")
            elif items:
                lines.append(items)
        lines.append("")

    experience = content.get("experience") or []
    if experience:
        lines.append("Experience")
        for exp in experience:
            if not isinstance(exp, dict):
                continue
            role = _clean_line(
                exp.get("tailoredRole") or exp.get("role") or exp.get("originalRole") or ""
            )
            period = _clean_line(exp.get("period", ""))
            company = _clean_line(exp.get("companyName", ""))
            location = _clean_line(exp.get("location", ""))

            if role and period:
                lines.append(f"{role}  {period}")
            elif role:
                lines.append(role)
            elif period:
                lines.append(period)
            if company:
                lines.append(company)
            if location:
                lines.append(location)

            for bullet in exp.get("bullets") or []:
                text = _clean_line(str(bullet))
                if text:
                    lines.append(f"{_BULLET_LINE}{text}")

            for proj in exp.get("projects") or []:
                if not isinstance(proj, dict):
                    continue
                ptitle = _clean_line(proj.get("title", ""))
                if ptitle:
                    lines.append(ptitle)
                for pb in proj.get("bullets") or []:
                    text = _clean_line(str(pb))
                    if text:
                        lines.append(f"{_BULLET_LINE}{text}")

            tech = _clean_line(exp.get("techStack", ""))
            if tech:
                lines.append(f"Tech stack: {tech}")
            lines.append("")

    education = content.get("education") or []
    if education:
        lines.append("Education")
        for edu in education:
            if not isinstance(edu, dict):
                continue
            school = _clean_line(edu.get("school", ""))
            degree = _clean_line(edu.get("degree", ""))
            period = _clean_line(edu.get("eduPeriod", ""))
            if degree and school:
                lines.append(f"{degree}")
                lines.append(school)
            elif school:
                lines.append(school)
            elif degree:
                lines.append(degree)
            if period:
                lines.append(period)
            lines.append("")

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + ("\n" if lines else "")
