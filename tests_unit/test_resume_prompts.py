"""Several role-based resume prompts per profile (in-memory MongoDB via mongomock-motor): the lazy "Default" view of
older profiles, saving and validating the list, and keeping `resumePromptBody` equal to the default prompt."""

import os
import re

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.resume_prompts import PromptError, apply_prompts, find_prompt, prompts_view, sync_default_body
from app.models.account import ResumePrompt
from app.routers.accounts import AccountUpdate, get_account, list_accounts, update_account

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

HEX24 = re.compile(r"^[0-9a-f]{24}$")


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["resume-prompts-unit"]
    await init_beanie(database=database, document_models=[UserExtra, Account])
    return database


async def _owner():
    return await UserExtra(name="Athen", email="athen@x.io", role="staff").insert()


def _row(p):
    return (p.id, p.name, p.role, p.body)


async def test_legacy_profile_reads_as_one_default_prompt(db):
    owner = await _owner()
    acc = Account(name="Blake", createdBy=owner.id, resumePromptBody="X")

    prompts, default_id = prompts_view(acc)

    assert [_row(p) for p in prompts] == [("default", "Default", None, "X")]
    assert default_id == "default"


async def test_empty_profile_has_an_empty_default(db):
    owner = await _owner()

    prompts, default_id = prompts_view(Account(name="Blake", createdBy=owner.id))

    assert [_row(p) for p in prompts] == [("default", "Default", None, "")] and default_id == "default"


async def test_apply_prompts_assigns_ids_and_syncs_default_body(db):
    owner = await _owner()
    acc = Account(name="Blake", createdBy=owner.id, resumePromptBody="old")

    apply_prompts(acc, [
        {"id": "default", "name": "Default", "role": None, "body": "old"},
        {"id": "new-1", "name": "DevOps", "role": "devops", "body": "devops text"},
    ], "new-1")

    first, second = acc.resumePrompts
    assert first.id == "default" and HEX24.match(second.id)
    assert acc.defaultPromptId == second.id and acc.resumePromptBody == "devops text"
    assert prompts_view(acc)[1] == second.id
    assert find_prompt(acc, second.id).name == "DevOps" and find_prompt(acc, "nope") is None


def _p(i, name=None, body="b"):
    return {"id": f"new-{i}", "name": f"P{i}" if name is None else name, "role": None, "body": body}


@pytest.mark.parametrize(
    "prompts, default_id",
    [
        ([_p(i) for i in range(11)], "new-0"),        # more than 10
        ([], None),                                   # none
        ([_p(1, name="")], "new-1"),                  # empty name
        ([_p(1, name="x" * 61)], "new-1"),            # name too long
        ([_p(1, "DevOps"), _p(2, "devops")], "new-1"),  # duplicate name, case-insensitive
        ([_p(1, body="x" * 30001)], "new-1"),         # body too long
        ([_p(1)], "missing"),                         # default not in the list
    ],
)
async def test_apply_prompts_rejects_bad_lists(db, prompts, default_id):
    owner = await _owner()
    acc = Account(name="Blake", createdBy=owner.id)

    with pytest.raises(PromptError):
        apply_prompts(acc, prompts, default_id)


async def test_body_only_write_updates_just_the_default(db):
    owner = await _owner()
    acc = Account(name="Blake", createdBy=owner.id)
    apply_prompts(acc, [_p(1, "Default", "d"), {**_p(2, "DevOps", "v"), "role": "devops"}], "new-1")

    sync_default_body(acc, "new")

    by_name = {p.name: p.body for p in acc.resumePrompts}
    assert by_name == {"Default": "new", "DevOps": "v"} and acc.resumePromptBody == "new"


async def test_update_account_route_saves_prompts(db):
    owner = await _owner()
    acc = await Account(name="Blake", createdBy=owner.id, resumePromptBody="legacy").insert()
    body = AccountUpdate(
        resumePrompts=[{"id": "default", "name": "Default", "role": None, "body": "legacy"},
                       {"id": "new-1", "name": "DevOps", "role": "devops", "body": "devops text"}],
        defaultPromptId="new-1",
    )

    out = await update_account(str(acc.id), body, owner)

    assert [p["name"] for p in out["resumePrompts"]] == ["Default", "DevOps"]
    assert HEX24.match(out["defaultPromptId"]) and out["resumePromptBody"] == "devops text"
    again = await get_account(str(acc.id), owner)
    assert again["defaultPromptId"] == out["defaultPromptId"]

    dup = AccountUpdate(resumePrompts=[_p(1, "DevOps"), _p(2, "devops")], defaultPromptId="new-1")
    with pytest.raises(HTTPException) as err:
        await update_account(str(acc.id), dup, owner)
    assert err.value.status_code == 400 and err.value.detail == "Another prompt already has this name"


async def test_get_account_shows_the_default_view_for_an_older_profile(db):
    owner = await _owner()
    acc = await Account(name="Blake", createdBy=owner.id, resumePromptBody="legacy").insert()

    out = await get_account(str(acc.id), owner)

    assert out["resumePrompts"][0]["id"] == "default" and out["resumePrompts"][0]["body"] == "legacy"
    assert out["defaultPromptId"] == "default"


async def test_an_older_prompt_is_named_from_its_heading(db):
    owner = await _owner()
    body = "# Resume Tailoring — Master Prompt (general: any team member, any JD)\n\n**Scope:** …"

    prompts, _ = prompts_view(Account(name="Blake", createdBy=owner.id, resumePromptBody=body))

    assert prompts[0].name == "Resume Tailoring — Master Prompt" and prompts[0].id == "default"


async def test_heading_names_are_trimmed_and_fall_back_to_default(db):
    owner = await _owner()
    long = "## " + "Very long heading words " * 5
    no_heading = "Write the resume for the job.\n# Not first"

    named = [prompts_view(Account(name="B", createdBy=owner.id, resumePromptBody=b))[0][0].name for b in (long, no_heading)]

    assert len(named[0]) <= 60 and named[0].startswith("Very long heading") and not named[0].endswith(" ")
    assert named[1] == "Default"


# ---------- resume units: each unit has its own HTML template and several roles ----------

OLD_TEMPLATE = "<html><body><h1>OLD</h1></body></html>"


def _unit(i, name, roles=(), html="<html><body>T</body></html>", fmt=None, body="b"):
    out = {"id": f"new-{i}", "name": name, "roles": list(roles), "body": body, "templateHtml": html,
           "templateName": f"{name}.html"}
    return {**out, **({"pageFormat": fmt} if fmt else {})}


async def test_legacy_profile_is_one_default_unit_with_its_template(db):
    owner = await _owner()
    acc = Account(name="Blake", createdBy=owner.id, resumePromptBody="# Master (general)\nbody",
                  styleTemplate=OLD_TEMPLATE, styleTemplateName="master.html", pageFormat="Letter")

    units, default_id = prompts_view(acc)

    (unit,) = units
    assert (unit.id, unit.name, unit.roles, default_id) == ("default", "Master", [], "default")
    assert (unit.templateHtml, unit.templateName, unit.pageFormat) == (OLD_TEMPLATE, "master.html", "Letter")


async def test_units_saved_before_templates_get_the_old_template_and_master_is_default(db):
    owner = await _owner()
    acc = Account(name="Blake", createdBy=owner.id, styleTemplate=OLD_TEMPLATE, styleTemplateName="master.html",
                  resumePrompts=[ResumePrompt(id="default", name="Master", body="m"),
                                 ResumePrompt(id="ops", name="DevOps", role="devops", body="o")],
                  defaultPromptId="ops")

    units, default_id = prompts_view(acc)

    assert default_id == "default"  # the previous template + master prompt are the default
    assert [(u.id, u.roles, u.templateHtml, u.templateName) for u in units] == [
        ("default", [], OLD_TEMPLATE, "master.html"), ("ops", ["devops"], OLD_TEMPLATE, "master.html")]


async def test_apply_units_sanitizes_and_mirrors_legacy_fields(db):
    owner = await _owner()
    acc = Account(name="Blake", createdBy=owner.id)

    apply_prompts(acc, [
        _unit(1, "Master", html="<html><body><h1>M</h1><script>alert(1)</script></body></html>", fmt="Letter", body="m"),
        _unit(2, "Full stack + DevOps", roles=["fullstack", "devops"]),
    ], "new-1")

    master, fsd = acc.resumePrompts
    assert "<script" not in master.templateHtml and "<h1>M</h1>" in master.templateHtml
    assert fsd.roles == ["fullstack", "devops"] and fsd.pageFormat == "A4"
    assert (acc.styleTemplate, acc.styleTemplateName, acc.pageFormat, acc.resumePromptBody) == (
        master.templateHtml, "Master.html", "Letter", "m")


@pytest.mark.parametrize("bad", [
    _unit(1, "A", roles=["devops", "devops"]),          # duplicate role
    _unit(1, "A", fmt="B5"),                             # unknown page format
    _unit(1, "A", html="x" * (2 * 1024 * 1024 + 1)),     # template over 2 MB
])
async def test_apply_units_rejects_bad_roles_format_and_big_template(db, bad):
    owner = await _owner()

    with pytest.raises(PromptError):
        apply_prompts(Account(name="Blake", createdBy=owner.id), [bad], "new-1")


def test_unknown_role_is_rejected_by_the_route_model():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AccountUpdate(resumePrompts=[{"name": "A", "roles": ["chef"]}], defaultPromptId="x")


async def test_template_only_write_updates_the_default_unit(db):
    owner = await _owner()
    acc = Account(name="Blake", createdBy=owner.id)
    apply_prompts(acc, [_unit(1, "Master"), _unit(2, "DevOps", roles=["devops"], html="<html><body>D</body></html>")], "new-1")
    await acc.insert()

    await update_account(str(acc.id), AccountUpdate(styleTemplate="<html><body>NEW</body></html>", styleTemplateName="n.html"), owner)

    saved = await Account.get(acc.id)
    by_name = {u.name: (u.templateHtml, u.templateName) for u in saved.resumePrompts}
    assert "NEW" in by_name["Master"][0] and by_name["Master"][1] == "n.html"
    assert by_name["DevOps"] == ("<html><body>D</body></html>", "DevOps.html")


async def test_account_list_strips_unit_bodies_and_templates(db):
    owner = await _owner()
    acc = Account(name="Blake", createdBy=owner.id)
    apply_prompts(acc, [_unit(1, "Master")], "new-1")
    await acc.insert()

    out = await list_accounts(owner, page=1, limit=10, search="", userId="", status_="active")

    (unit,) = out["accounts"][0]["resumePrompts"] if "accounts" in out else out["data"][0]["resumePrompts"]
    assert "templateHtml" not in unit and "body" not in unit and unit["templateName"] == "Master.html"


# ---------- final-review fixes ----------


async def test_profile_saved_before_templates_reads_the_master_prompt_everywhere(db):
    from app.routers.prompt_eval import resolve_prompt
    from app.resume_prompts import default_prompt_body
    owner = await _owner()
    acc = await Account(name="Blake", createdBy=owner.id, styleTemplate=OLD_TEMPLATE, resumePromptBody="ops body",
                        resumePrompts=[ResumePrompt(id="default", name="Master", body="master body"),
                                       ResumePrompt(id="ops", name="DevOps", role="devops", body="ops body")],
                        defaultPromptId="ops").insert()

    assert default_prompt_body(acc) == "master body"
    assert resolve_prompt(None, acc, owner) == ("profile", "master body")
    assert (await get_account(str(acc.id), owner))["resumePromptBody"] == "master body"


async def test_total_template_size_is_capped(db):
    owner = await _owner()
    big = [_unit(i, f"U{i}", html="<html><body>" + "x" * (1900 * 1024) + "</body></html>") for i in range(5)]

    with pytest.raises(PromptError) as err:
        apply_prompts(Account(name="Blake", createdBy=owner.id), big, "new-0")
    assert "8 MB" in str(err.value)


async def test_a_template_without_a_file_name_still_counts_as_a_template(db):
    owner = await _owner()
    acc = Account(name="Blake", createdBy=owner.id)

    apply_prompts(acc, [{**_unit(1, "A"), "templateName": ""}, {**_unit(2, "B"), "templateHtml": "", "templateName": "x.html"}], "new-1")

    a, b = acc.resumePrompts
    assert (a.templateName, b.templateName) == ("HTML template", "")  # name set iff there is a template


async def test_the_prompt_limit_comes_from_the_setting(db, monkeypatch):
    from app import resume_queue
    from app.config import get_settings
    from app.routers.resume import limits

    owner = await _owner()
    acc = Account(name="Blake", createdBy=owner.id)
    monkeypatch.setattr(get_settings(), "RESUME_PROMPT_MAX_CHARS", 50000)

    apply_prompts(acc, [_p(1, body="x" * 40000)], "new-1")  # over the old 30,000: fine now
    with pytest.raises(PromptError, match="50,000"):
        apply_prompts(acc, [_p(1, body="x" * 50001)], "new-1")
    assert await limits(owner) == {"promptMaxChars": 50000}

    monkeypatch.setattr(get_settings(), "RESUME_PROMPT_MAX_CHARS", 100)
    with pytest.raises(PromptError):
        apply_prompts(acc, [_p(1, body="x" * 101)], "new-1")
    assert resume_queue.max_body() == 100  # one-off prompts are cut to the same length


async def test_the_user_prompt_and_eval_prompt_follow_the_setting(db, monkeypatch):
    from app.config import get_settings
    from app.routers.profile import ProfileUpdate, update_profile
    from app.routers.prompt_eval import RunIn, create_run

    owner = await _owner()
    monkeypatch.setattr(get_settings(), "RESUME_PROMPT_MAX_CHARS", 50000)
    await update_profile(ProfileUpdate(username="Athen", email="athen@x.io", resumePromptBody="x" * 60000), owner)
    assert len(owner.resumePromptBody) == 50000  # was cut at 30,000 whatever the setting

    RunIn(accountId="a", promptBody="x" * 40000)  # no fixed 30,000 cap on the model
    monkeypatch.setattr(get_settings(), "RESUME_PROMPT_MAX_CHARS", 100)
    monkeypatch.setattr("app.routers.prompt_eval.require_model", lambda *a, **k: "m")
    with pytest.raises(HTTPException, match="100 characters"):
        await create_run(RunIn(accountId="a", promptBody="x" * 101), owner)
