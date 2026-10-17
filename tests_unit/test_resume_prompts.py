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
from app.routers.accounts import AccountUpdate, get_account, update_account

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
