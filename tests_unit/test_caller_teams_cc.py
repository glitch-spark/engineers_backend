import os
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.caller_teams.blocks import cc  # noqa: E402


def _c(id_, slack=None, name="Omar"):
    return SimpleNamespace(id=id_, slackUserId=slack, name=name)


def test_cc_tags_the_manager_when_someone_else_is_calling():
    assert cc(SimpleNamespace(caller=_c(1, "U1"), manager=_c(2, "U2"))) == " · cc: <@U2>"


def test_cc_is_empty_for_no_manager_or_manager_on_the_call():
    assert cc(SimpleNamespace(caller=_c(1, "U1"), manager=None)) == ""
    assert cc(SimpleNamespace(caller=_c(2, "U2"), manager=_c(2, "U2"))) == ""


def test_cc_without_a_caller_still_names_the_manager():
    assert cc(SimpleNamespace(caller=None, manager=_c(2, None, "Muhammad"))) == " · cc: Muhammad"


def test_scheduled_post_asks_for_a_ready_check_and_canceled_does_not():
    from app.caller_teams.blocks import READY_ASK, scheduled_blocks
    caller = SimpleNamespace(method="video", methodValue="")
    entry = SimpleNamespace(caller=caller, stage="intro")
    tr = SimpleNamespace(iv=SimpleNamespace(companyName="Acme"), entry=entry, caller=_c(1, "U1"), manager=None,
                         start=None, team=SimpleNamespace(timezone="America/Los_Angeles"))
    assert READY_ASK in str(scheduled_blocks(tr, "scheduled")[1])
    assert READY_ASK not in str(scheduled_blocks(tr, "canceled")[1])
