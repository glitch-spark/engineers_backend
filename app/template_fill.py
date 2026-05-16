"""Deterministic resume template filling.

A marker-annotated HTML template carries `data-slot` / `data-repeat` /
`data-field` attributes. `fill_template` walks the DOM, clones repeat blocks
per content item, and writes text into field/slot nodes. No LLM is involved —
the template's structure and CSS are preserved byte-for-byte.

Marker contract
---------------
data-slot="<key>"        single text region (name, title, summary)
data-repeat="<kind>"     block cloned once per list item
data-field="<key>"       text node filled from the current item / content

Repeat kinds + their item shape (see CONTENT schema in resume_service):
  contact   -> {display, url}
  company   -> {companyName, role, period, bullets[], projects[], techStack}
  bullet    -> str            (nested in company)
  project   -> {title, bullets[]}   (nested in company)
  projectBullet -> str        (nested in project)
  skill     -> {category, items}
  education -> {school, degree, eduPeriod}
"""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup, Tag

# Markers that MUST exist for a template to be considered valid.
REQUIRED_MARKERS = {
    'data-slot="name"': 'header name',
    'data-slot="summary"': 'summary text',
    'data-repeat="company"': 'experience company block',
    'data-repeat="bullet"': 'achievement bullet',
}

# Optional-but-recommended markers — surfaced as warnings, not errors.
RECOMMENDED_MARKERS = {
    'data-slot="title"': 'header title',
    'data-repeat="contact"': 'contact line item',
    'data-repeat="project"': 'project block',
    'data-repeat="projectBullet"': 'project bullet',
    'data-repeat="skill"': 'skills row',
    'data-repeat="education"': 'education row',
}


def validate_annotated(html: str) -> tuple[list[str], list[str]]:
    """Return (missing_required, missing_recommended) marker descriptions."""
    if not html:
        return list(REQUIRED_MARKERS.values()), list(RECOMMENDED_MARKERS.values())
    missing_req = [desc for marker, desc in REQUIRED_MARKERS.items() if marker not in html]
    missing_rec = [desc for marker, desc in RECOMMENDED_MARKERS.items() if marker not in html]
    return missing_req, missing_rec


def _set_text(node: Tag, text: str) -> None:
    """Replace a node's text content, keeping its tag + attributes + children
    structure minimal. We clear children and insert a single string."""
    node.clear()
    node.append(text or "")


def _fill_fields(scope: Tag, mapping: dict[str, str]) -> None:
    """Fill every data-field within `scope` (not descending into nested
    data-repeat blocks — those are handled by their own pass)."""
    for el in scope.find_all(attrs={"data-field": True}):
        # Skip fields that belong to a nested repeat block.
        if _inside_nested_repeat(el, scope):
            continue
        key = el.get("data-field")
        if key in mapping:
            _set_text(el, mapping[key])


def _inside_nested_repeat(el: Tag, scope: Tag) -> bool:
    """True if `el` sits inside a data-repeat block that is itself a
    descendant of `scope` (i.e. belongs to an inner loop)."""
    parent = el.parent
    while parent is not None and parent is not scope:
        if parent.has_attr("data-repeat"):
            return True
        parent = parent.parent
    return False


def _direct_repeat_children(scope: Tag, kind: str) -> list[Tag]:
    """Find data-repeat='kind' elements under `scope` that are not nested
    inside another (different) data-repeat block."""
    out: list[Tag] = []
    for el in scope.find_all(attrs={"data-repeat": kind}):
        if _inside_nested_repeat(el, scope):
            continue
        out.append(el)
    return out


def _expand_repeat(template_el: Tag, items: list[Any], filler, separator: str = "") -> None:
    """Clone `template_el` once per item, fill each clone via `filler(clone,
    item)`, insert them before the template, then remove the template.
    If items is empty, the template element is removed entirely.

    `separator` — when set, a literal text node is inserted between consecutive
    clones. Used for inline lists (contact line) that would otherwise render
    glued together once the template's own separators are dropped."""
    from bs4 import NavigableString

    soup_parent = template_el.parent
    if soup_parent is None:
        return
    for idx, item in enumerate(items):
        clone = _clone(template_el)
        clone.attrs.pop("data-repeat", None)
        filler(clone, item)
        if separator and idx > 0:
            template_el.insert_before(NavigableString(separator))
        template_el.insert_before(clone)
    template_el.extract()


def _clone(el: Tag) -> Tag:
    return BeautifulSoup(str(el), "html.parser").find()


import re as _re

# Leading bullet glyph + space the template's sample <li> uses (e.g. "- ").
# Content bullets are plain text, so we re-apply the template's own prefix
# rather than overwrite it away (templates with list-style:none rely on it).
_BULLET_PREFIX_RE = _re.compile(r"^\s*([-–—•●◦*]+\s*)")


def _bullet_prefix(el: Tag) -> str:
    m = _BULLET_PREFIX_RE.match(el.get_text() or "")
    return m.group(1) if m else ""


def _fill_company(clone: Tag, company: dict) -> None:
    _fill_fields(clone, {
        "companyName": company.get("companyName", ""),
        "role": company.get("role", ""),
        "period": company.get("period", ""),
        "techStack": company.get("techStack", ""),
    })
    # Nested: bullets — preserve the template <li>'s leading prefix ("- " etc).
    for li in _direct_repeat_children(clone, "bullet"):
        pfx = _bullet_prefix(li)
        _expand_repeat(li, company.get("bullets", []) or [],
                       lambda c, txt, p=pfx: _fill_bullet(c, p + txt, "bulletText"))
    # Nested: projects
    for proj_el in _direct_repeat_children(clone, "project"):
        _expand_repeat(proj_el, company.get("projects", []) or [], _fill_project)


def _fill_bullet(clone: Tag, text: str, field_key: str) -> None:
    matched = False
    for el in clone.find_all(attrs={"data-field": field_key}):
        _set_text(el, text)
        matched = True
    if not matched:
        # <li data-repeat="bullet" data-field="bulletText"> — field on self.
        if clone.get("data-field") == field_key:
            _set_text(clone, text)
        else:
            _set_text(clone, text)


def _fill_project(clone: Tag, project: dict) -> None:
    for el in clone.find_all(attrs={"data-field": "projectTitle"}):
        if not _inside_nested_repeat(el, clone):
            _set_text(el, project.get("title", ""))
    for pb in _direct_repeat_children(clone, "projectBullet"):
        pfx = _bullet_prefix(pb)
        _expand_repeat(pb, project.get("bullets", []) or [],
                       lambda c, txt, p=pfx: _fill_bullet(c, p + txt, "projectBulletText"))


def _fill_contact(clone: Tag, item: dict) -> None:
    display = item.get("display", "")
    url = item.get("url", "")
    # Fill display text on the data-field node (or the clone itself).
    target = clone if clone.get("data-field") == "contactDisplay" else clone.find(
        attrs={"data-field": "contactDisplay"}
    )
    if target is not None:
        _set_text(target, display)
    # Set href on the first <a> (clone itself or descendant).
    anchor = clone if clone.name == "a" else clone.find("a")
    if anchor is not None:
        if url:
            anchor["href"] = url
            anchor["style"] = "color: inherit; text-decoration: none;"
        else:
            anchor.attrs.pop("href", None)


def _fill_skill(clone: Tag, skill: dict) -> None:
    _fill_fields(clone, {
        "skillCategory": skill.get("category", ""),
        "skillItems": skill.get("items", ""),
    })


def _fill_education(clone: Tag, edu: dict) -> None:
    _fill_fields(clone, {
        "school": edu.get("school", ""),
        "degree": edu.get("degree", ""),
        "eduPeriod": edu.get("eduPeriod", ""),
    })


def fill_template(annotated_html: str, content: dict) -> str:
    """Render the annotated template with `content`. Returns a full HTML doc.

    content shape:
      name, title, summary        -> str
      contactItems  -> [{display, url}]
      experience    -> [{companyName, role, period, bullets[],
                         projects[{title, bullets[]}], techStack}]
      skills        -> [{category, items}]
      education     -> [{school, degree, eduPeriod}]
    """
    soup = BeautifulSoup(annotated_html, "html.parser")
    root = soup

    # ---- single slots ----
    for key in ("name", "title", "summary"):
        for el in root.find_all(attrs={"data-slot": key}):
            _set_text(el, str(content.get(key, "") or ""))

    # ---- contact items ----
    # Contact line is an inline list — the template's own separators are lost
    # when annotation collapses the sample items to one repeat element, so we
    # re-insert one. `data-sep` on the repeat element overrides the default.
    for el in _direct_repeat_children(root, "contact"):
        sep = el.get("data-sep") or " | "
        _expand_repeat(el, content.get("contactItems", []) or [], _fill_contact, separator=sep)

    # ---- experience (outer loop; nested bullets/projects handled inside) ----
    for el in _direct_repeat_children(root, "company"):
        _expand_repeat(el, content.get("experience", []) or [], _fill_company)

    # ---- skills ----
    for el in _direct_repeat_children(root, "skill"):
        _expand_repeat(el, content.get("skills", []) or [], _fill_skill)

    # ---- education ----
    for el in _direct_repeat_children(root, "education"):
        _expand_repeat(el, content.get("education", []) or [], _fill_education)

    return str(soup)
