"""A profile's resume prompts: several, one per job role, with a default.

Older profiles have only `resumePromptBody`; they read as one prompt named "Default" (id "default") until their
prompts are saved. `resumePromptBody` is kept equal to the default prompt's body, so everything that reads it (the
generation fallback chain, Prompt Eval, the lookup badge) keeps working.
"""
import re
from datetime import datetime
from typing import Any, Optional

from beanie import PydanticObjectId

from app.models.account import ResumePrompt

MAX_PROMPTS = 10
MAX_NAME = 60
MAX_BODY = 30000
DEFAULT_NAME = "Default"
LEGACY_ID = "default"


class PromptError(ValueError):
    """The prompt list can't be saved; the message is shown to the user."""


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


def prompts_view(account: Any) -> tuple[list[ResumePrompt], str]:
    """(prompts, default prompt id). An older profile reads as one prompt (named from its heading, else "Default");
    a missing or stale default id falls back to the first prompt."""
    prompts = list(account.resumePrompts or [])
    if not prompts:
        body = account.resumePromptBody or ""
        prompts = [ResumePrompt(id=LEGACY_ID, name=legacy_name(body), body=body)]
    ids = {p.id for p in prompts}
    default_id = account.defaultPromptId if account.defaultPromptId in ids else prompts[0].id
    return prompts, default_id


def find_prompt(account: Any, prompt_id: Optional[str]) -> Optional[ResumePrompt]:
    return next((p for p in prompts_view(account)[0] if p.id == prompt_id), None) if prompt_id else None


def apply_prompts(account: Any, prompts: list[dict], default_id: Optional[str]) -> None:
    """Replace the profile's prompts (validated) and its default; new prompts (no id, or a "new-…" id from the app)
    get an id. Raises PromptError with a message for the user."""
    if not prompts:
        raise PromptError("Keep at least one resume prompt")
    if len(prompts) > MAX_PROMPTS:
        raise PromptError(f"A profile can have up to {MAX_PROMPTS} resume prompts")
    before = {p.id: p for p in prompts_view(account)[0]}
    out: list[ResumePrompt] = []
    renamed: dict[str, str] = {}
    names: set[str] = set()
    for raw in prompts:
        name = str(raw.get("name") or "").strip()
        body = str(raw.get("body") or "")
        if not name:
            raise PromptError("Give every prompt a name")
        if len(name) > MAX_NAME:
            raise PromptError(f"Keep prompt names under {MAX_NAME} characters")
        if name.lower() in names:
            raise PromptError("Another prompt already has this name")
        names.add(name.lower())
        if len(body) > MAX_BODY:
            raise PromptError(f"A prompt can be up to {MAX_BODY} characters")
        given = str(raw.get("id") or "")
        pid = given if given and not given.startswith("new-") else str(PydanticObjectId())
        if pid in {p.id for p in out}:
            raise PromptError("Two prompts have the same id")
        if given:
            renamed[given] = pid
        old = before.get(pid)
        role = raw.get("role") or None
        same = old is not None and (old.name, old.role, old.body) == (name, role, body)
        out.append(ResumePrompt(id=pid, name=name, role=role, body=body,
                                updatedAt=old.updatedAt if same else datetime.utcnow()))
    chosen = renamed.get(default_id or "", default_id)
    if chosen not in {p.id for p in out}:
        raise PromptError("Pick one of the prompts as the default")
    account.resumePrompts = out
    account.defaultPromptId = chosen
    account.resumePromptBody = next(p.body for p in out if p.id == chosen) or None


def sync_default_body(account: Any, body: Optional[str]) -> None:
    """A write of resumePromptBody alone (older screens): it becomes the default prompt's body; other prompts stay."""
    account.resumePromptBody = body or None
    if not account.resumePrompts:
        return
    _, default_id = prompts_view(account)
    account.resumePrompts = [
        p.model_copy(update={"body": body or "", "updatedAt": datetime.utcnow()}) if p.id == default_id else p
        for p in account.resumePrompts
    ]
