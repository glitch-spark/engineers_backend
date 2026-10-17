"""A profile's resume units: each one an HTML template, the prompt written for it, and the job roles it is for.

Older profiles have only the single template and prompt fields (`styleTemplate`, `resumePromptBody`, …); they read
as one unit (id "default", named from the prompt's heading) that is the default. Units saved before they had their
own templates get the profile template, and the "default" unit is the default, until the profile is saved again.
The single fields always mirror the default unit, so everything that reads them (Generate page, Prompt Eval,
screening answers, lookup badges) keeps working on the default unit.
"""
import re
from datetime import datetime
from typing import Any, Optional, get_args

from beanie import PydanticObjectId

from app.html_sanitize import sanitize_template_html
from app.models.account import PageFormat, PromptRole, ResumePrompt

MAX_PROMPTS = 10
MAX_NAME = 60
MAX_BODY = 30000
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024
# All of a profile's templates (the default's counted twice: it is mirrored into styleTemplate), well under
# MongoDB's 16 MB document limit.
MAX_TOTAL_TEMPLATE_BYTES = 8 * 1024 * 1024
DEFAULT_NAME = "Default"
LEGACY_ID = "default"
_ROLES = set(get_args(PromptRole))
_FORMATS = set(get_args(PageFormat))


class PromptError(ValueError):
    """The unit list can't be saved; the message is shown to the user."""


def legacy_name(body: Optional[str]) -> str:
    """A name for an older profile's single prompt: its first line when that's a markdown heading ("# Resume Tailoring —
    Master Prompt (general: …)" → "Resume Tailoring — Master Prompt"), else "Default"."""
    first = next((line.strip() for line in (body or "").splitlines() if line.strip()), "")
    if not first.startswith("#"):
        return DEFAULT_NAME
    name = re.sub(r"\s*\([^)]*\)\s*$", "", first.lstrip("#").strip()).replace("**", "").strip()
    if len(name) > MAX_NAME:
        name = name[:MAX_NAME].rsplit(" ", 1)[0]
    return name.rstrip(" -—:·") or DEFAULT_NAME


def _upgrade(unit: ResumePrompt, account: Any) -> ResumePrompt:
    """A stored unit in today's shape: a single `role` as `roles`; no template yet → the profile template."""
    update: dict = {"roles": list(unit.roles) or ([unit.role] if unit.role else []), "role": None}
    if unit.templateHtml is None and unit.templateName is None:
        update.update(templateHtml=getattr(account, "styleTemplate", None) or "",
                      templateName=getattr(account, "styleTemplateName", None) or "",
                      pageFormat=getattr(account, "pageFormat", None) or "A4")
    return unit.model_copy(update=update)


def prompts_view(account: Any) -> tuple[list[ResumePrompt], str]:
    """(units, default unit id). An older profile reads as one unit, the default; units saved before they had
    templates get the profile template, with the "default" unit as the default; otherwise a missing or stale default
    id falls back to the first unit."""
    stored = list(getattr(account, "resumePrompts", None) or [])
    if not stored:
        body = getattr(account, "resumePromptBody", None) or ""
        unit = ResumePrompt(id=LEGACY_ID, name=legacy_name(body), body=body,
                            templateHtml=getattr(account, "styleTemplate", None) or "",
                            templateName=getattr(account, "styleTemplateName", None) or "",
                            pageFormat=getattr(account, "pageFormat", None) or "A4")
        return [unit], LEGACY_ID
    # Saved before units had templates: neither template field was ever stored (saves now always store both).
    before_templates = all(u.templateHtml is None and u.templateName is None for u in stored)
    units = [_upgrade(u, account) for u in stored]
    ids = {u.id for u in units}
    if before_templates and LEGACY_ID in ids:
        return units, LEGACY_ID  # the previous template + master prompt stay the default
    default_id = getattr(account, "defaultPromptId", None)
    return units, default_id if default_id in ids else units[0].id


def find_prompt(account: Any, prompt_id: Optional[str]) -> Optional[ResumePrompt]:
    return next((p for p in prompts_view(account)[0] if p.id == prompt_id), None) if prompt_id else None


def default_prompt_body(account: Any) -> str:
    """The default unit's prompt. Readers of the single prompt use this rather than `resumePromptBody`, which a
    profile saved before units had templates may still hold from an earlier default."""
    units, default_id = prompts_view(account)
    return next((u.body for u in units if u.id == default_id), "") or ""


def unit_template(unit: ResumePrompt) -> tuple[Optional[str], str]:
    """(template HTML or None, page format) a resume is generated with for this unit."""
    return unit.templateHtml or None, unit.pageFormat or "A4"


def _mirror_default(account: Any, units: list[ResumePrompt], default_id: str) -> None:
    default = next(u for u in units if u.id == default_id)
    account.resumePrompts = units
    account.defaultPromptId = default_id
    account.resumePromptBody = default.body or None
    account.styleTemplate = default.templateHtml or None
    account.styleTemplateName = default.templateName or None
    account.pageFormat = default.pageFormat or "A4"


def apply_prompts(account: Any, prompts: list[dict], default_id: Optional[str]) -> None:
    """Replace the profile's units (validated; templates sanitized) and its default, and mirror the default unit into
    the single fields. New units (no id, or a "new-…" id from the app) get an id. Raises PromptError."""
    if not prompts:
        raise PromptError("Keep at least one resume unit")
    if len(prompts) > MAX_PROMPTS:
        raise PromptError(f"A profile can have up to {MAX_PROMPTS} resume units")
    before = {p.id: p for p in prompts_view(account)[0]}
    out: list[ResumePrompt] = []
    renamed: dict[str, str] = {}
    names: set[str] = set()
    for raw in prompts:
        name = str(raw.get("name") or "").strip()
        body = str(raw.get("body") or "")
        if not name:
            raise PromptError("Give every unit a name")
        if len(name) > MAX_NAME:
            raise PromptError(f"Keep unit names under {MAX_NAME} characters")
        if name.lower() in names:
            raise PromptError("Another prompt already has this name")
        names.add(name.lower())
        if len(body) > MAX_BODY:
            raise PromptError(f"A prompt can be up to {MAX_BODY} characters")
        roles = list(raw.get("roles") or ([raw["role"]] if raw.get("role") else []))
        if any(r not in _ROLES for r in roles) or len(set(roles)) != len(roles):
            raise PromptError("Each role can be marked once, from the list")
        given = str(raw.get("id") or "")
        pid = given if given and not given.startswith("new-") else str(PydanticObjectId())
        if pid in {p.id for p in out}:
            raise PromptError("Two units have the same id")
        if given:
            renamed[given] = pid
        old = before.get(pid)
        html = raw.get("templateHtml")
        if html is None:  # not sent (older screens): keep what the unit had
            html, tname = (old.templateHtml or "", old.templateName or "") if old else ("", "")
        else:
            if len(html.encode("utf-8")) > MAX_TEMPLATE_BYTES:
                raise PromptError("An HTML template can be up to 2 MB")
            html, tname = sanitize_template_html(html), str(raw.get("templateName") or "")
        tname = (tname or "HTML template") if html else ""  # a name iff there is a template
        fmt = raw.get("pageFormat") or (old.pageFormat if old else None) or account.pageFormat or "A4"
        if fmt not in _FORMATS:
            raise PromptError(f"Page format must be one of {', '.join(sorted(_FORMATS))}")
        same = old is not None and (old.name, old.roles, old.body, old.templateHtml, old.templateName, old.pageFormat) == (
            name, roles, body, html, tname, fmt)
        out.append(ResumePrompt(id=pid, name=name, roles=roles, body=body, templateHtml=html, templateName=tname,
                                pageFormat=fmt, updatedAt=old.updatedAt if same else datetime.utcnow()))
    chosen = renamed.get(default_id or "", default_id)
    if chosen not in {p.id for p in out}:
        raise PromptError("Pick one of the units as the default")
    total = sum(len((u.templateHtml or "").encode("utf-8")) * (2 if u.id == chosen else 1) for u in out)
    if total > MAX_TOTAL_TEMPLATE_BYTES:
        raise PromptError("A profile's HTML templates can be up to 8 MB in total: remove or shrink some templates")
    _mirror_default(account, out, chosen)


def sync_default_body(account: Any, body: Optional[str]) -> None:
    """A write of resumePromptBody alone (older screens): it becomes the default unit's prompt; other units stay."""
    if not account.resumePrompts:
        account.resumePromptBody = body or None
        return
    units, default_id = prompts_view(account)
    units = [u.model_copy(update={"body": body or "", "updatedAt": datetime.utcnow()}) if u.id == default_id else u
             for u in units]
    _mirror_default(account, units, default_id)


def sync_default_template(account: Any, html: Optional[str], name: Optional[str]) -> None:
    """A write of styleTemplate (already sanitized) / styleTemplateName alone (older screens): it becomes the default
    unit's template; other units keep theirs."""
    if not account.resumePrompts:
        if html is not None:
            account.styleTemplate = html or None
        if name is not None:
            account.styleTemplateName = name or None
        return
    units, default_id = prompts_view(account)  # before changing the profile template: units without one inherit it
    patch = {"updatedAt": datetime.utcnow(), **({"templateHtml": html or ""} if html is not None else {}),
             **({"templateName": name or ""} if name is not None else {})}
    _mirror_default(account, [u.model_copy(update=patch) if u.id == default_id else u for u in units], default_id)
