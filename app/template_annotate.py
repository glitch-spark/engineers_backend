"""LLM-assisted one-time conversion: plain HTML resume → marker-annotated
template.

Drift-free design: the LLM NEVER re-emits the template's HTML/CSS. Instead:
  1. Server tags every element of the original template with a temp
     `data-anno-id`.
  2. LLM reads that and returns a small JSON MARKER MAP — a list of
     {id, attr, value} assignments.
  3. Server applies the markers to the ORIGINAL template via BeautifulSoup
     and strips the temp ids.

The template's CSS, fonts, margins, and structure are preserved byte-for-byte
— the LLM only decides WHERE markers go.

Runtime behavior is unchanged; this module only maps markers onto existing HTML.
Deploy-only touch: no functional change.
"""

from __future__ import annotations

import json

from bs4 import BeautifulSoup, Tag
from fastapi import HTTPException, status

from app.config import get_settings

_ANNO_ID = "data-anno-id"

_ANNOTATE_PROMPT = """\
You convert a plain HTML resume into a marker-annotated template. You do NOT
write or edit HTML. You return a JSON MARKER MAP only.

Every element in the HTML below has a unique `data-anno-id` attribute. Your job:
decide which elements should carry resume markers, and return the assignments.

MARKER CONTRACT — assign these (attr, value) pairs to the right elements:

Single regions (one element each):
- (data-slot, name)     -> element holding the candidate name
- (data-slot, title)    -> element holding the job title under the name
- (data-slot, summary)  -> element holding the professional summary paragraph

Contact line — the repeating contact entry:
- (data-repeat, contact) -> ONE inline element (<span>/<a>) that represents a
  single contact item. If the template lists several sample contacts, pick the
  FIRST as the repeat element.
- (data-field, contactDisplay) -> the element whose text is the contact value
  (often the same element as data-repeat=contact).
- (data-sep, " | ") -> ALSO on the data-repeat=contact element: set value to
  the literal separator the template uses between contact items.

Experience — the company block that repeats per company. Pick the FIRST sample
company block:
- (data-repeat, company) -> the per-company container
  Inside it:
  - (data-field, companyName), (data-field, role), (data-field, period),
    (data-field, techStack) -> the elements holding each value
  - (data-repeat, bullet) -> the FIRST <li> of the achievement list; ALSO
    assign (data-field, bulletText) to that same <li>
  - (data-repeat, project) -> the FIRST per-project container
    Inside it:
    - (data-field, projectTitle) -> element holding the project name
    - (data-repeat, projectBullet) -> the FIRST project-bullet <li>; ALSO
      assign (data-field, projectBulletText) to that same <li>

Skills — pick the FIRST sample skills row:
- (data-repeat, skill) -> the per-category row container
  - (data-field, skillCategory) -> element holding the category label
  - (data-field, skillItems) -> element holding the comma-separated skills

Education — pick the FIRST sample education row:
- (data-repeat, education) -> the per-school row container
  - (data-field, school), (data-field, degree), (data-field, eduPeriod)

REDUNDANT SAMPLE REMOVAL (critical):
The template usually shows SEVERAL sample entries per repeating section (e.g.
2-3 sample companies, 4-5 sample contact items, multiple skill rows, multiple
education rows, alternate one-line "compact" job blocks). The filler CLONES the
single marked data-repeat element per real candidate item — so every OTHER
sample entry MUST be deleted, or it will appear in the output as junk.

Return a `remove` list: the data-anno-id of EVERY redundant sample element —
- the 2nd and all later sample entries of each repeating section,
- any alternate-layout sample blocks (e.g. compact one-line job entries) that
  are not the marked data-repeat element,
- extra sample bullets / projects beyond the first.
Do NOT list any element that received a marker. Do NOT list section
headings, dividers, or structural containers — only sample CONTENT entries.

RULES:
- Assign each marker to the FIRST sample element of its kind only.
- One element may receive multiple markers (e.g. an <li> getting both
  data-repeat=bullet and data-field=bulletText) — emit one entry per (attr).
- data-field elements must sit inside their data-repeat ancestor.
- If a section is absent from the template, omit its markers.
- Return STRICT JSON only, no prose, no code fences:
  {"markers": [{"id": <id int>, "attr": "<attr>", "value": "<value>"}],
   "remove": [<id int>, ...]}

HTML (each element carries data-anno-id):
"""


def _inject_ids(html: str) -> tuple[str, BeautifulSoup]:
    """Tag every element with a sequential data-anno-id. Returns
    (html_string_for_llm, soup) — same soup is reused to apply markers."""
    soup = BeautifulSoup(html, "html.parser")
    i = 0
    for el in soup.find_all(True):
        el[_ANNO_ID] = str(i)
        i += 1
    return str(soup), soup


def _apply_markers(soup: BeautifulSoup, markers: list[dict]) -> int:
    """Apply the LLM's marker map to the soup. Returns count applied."""
    by_id: dict[str, object] = {}
    for el in soup.find_all(attrs={_ANNO_ID: True}):
        by_id[el[_ANNO_ID]] = el

    applied = 0
    for m in markers:
        if not isinstance(m, dict):
            continue
        el = by_id.get(str(m.get("id")))
        attr = m.get("attr")
        value = m.get("value")
        if el is None or not attr or value is None:
            continue
        if attr not in ("data-slot", "data-repeat", "data-field", "data-sep"):
            continue
        el[attr] = str(value)
        applied += 1
    return applied


_MARKER_ATTRS = ("data-slot", "data-repeat", "data-field")


def _is_live_tag(el: object) -> bool:
    """True when `el` is still attached and safe for attrs / decompose."""
    return isinstance(el, Tag) and el.parent is not None


def _element_has_marker(el: Tag) -> bool:
    attrs = getattr(el, "attrs", None)
    if not isinstance(attrs, dict):
        return False
    return any(a in attrs for a in _MARKER_ATTRS)


def _contains_marked_descendant(el: Tag) -> bool:
    for child in el.find_all(True):
        if child is el:
            continue
        if _element_has_marker(child):
            return True
    return False


def _removal_depth(el: Tag) -> int:
    depth = 0
    parent = el.parent
    while parent is not None and getattr(parent, "name", None):
        depth += 1
        parent = parent.parent
    return depth


def _apply_removals(soup: BeautifulSoup, remove_ids: list) -> int:
    """Delete redundant sample elements the LLM flagged. Never deletes an
    element that carries a marker (safety against the LLM contradicting
    itself)."""
    by_id: dict[str, Tag] = {}
    for el in soup.find_all(attrs={_ANNO_ID: True}):
        if isinstance(el, Tag):
            by_id[el[_ANNO_ID]] = el

    # Remove deepest nodes first so parent decompose does not leave stale
    # child refs that break has_attr on decomposed tags.
    ordered_ids = sorted(
        (str(rid) for rid in remove_ids),
        key=lambda rid: _removal_depth(by_id[rid]) if rid in by_id and _is_live_tag(by_id[rid]) else -1,
        reverse=True,
    )

    removed = 0
    for rid in ordered_ids:
        el = by_id.get(rid)
        if not _is_live_tag(el):
            continue
        if _element_has_marker(el):
            continue  # never remove a marked element
        if _contains_marked_descendant(el):
            continue
        el.decompose()
        removed += 1
    return removed


def _strip_ids(soup: BeautifulSoup) -> None:
    for el in soup.find_all(attrs={_ANNO_ID: True}):
        del el[_ANNO_ID]


async def annotate_template(plain_html: str) -> str:
    """Run the marker-map annotation. Returns the annotated HTML — the
    original template with data-* markers added, CSS/structure untouched."""
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY not configured",
        )
    if not (plain_html or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No template HTML to annotate",
        )

    id_html, soup = _inject_ids(plain_html)

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    resp = await client.responses.create(
        model=settings.RESUME_OPENAI_MODEL,
        input=_ANNOTATE_PROMPT + id_html,
    )
    raw = (resp.output_text or "").strip()
    # Strip code fences if present.
    if raw.startswith("```"):
        nl = raw.find("\n")
        if nl != -1:
            raw = raw[nl + 1:]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]

    try:
        parsed = json.loads(raw.strip())
        markers = parsed.get("markers") if isinstance(parsed, dict) else None
        if not isinstance(markers, list):
            raise ValueError("missing markers[]")
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Template conversion returned malformed marker map: {exc}",
        )
    remove_ids = parsed.get("remove") if isinstance(parsed.get("remove"), list) else []

    _apply_markers(soup, markers)
    # Removals AFTER markers so the marked-element guard in _apply_removals works.
    _apply_removals(soup, remove_ids)
    _strip_ids(soup)
    return str(soup)
