"""The coordination channel's short post: pure Block Kit, no Slack calls."""

import os
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402

from app.coordination.blocks import post_blocks  # noqa: E402
from app.interview_day.rounds import DayRound  # noqa: E402
from app.slack_bot import slack_time  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")

IV, R = "a" * 24, "b" * 32
START = datetime(2026, 11, 16, 18, 30, tzinfo=timezone.utc)
ORIGIN = "https://app.x"
JUSTIN = SimpleNamespace(id="1" * 24, name="Justin", email="j@x.io", slackUserId="U0JUSTIN01", isActive=True)
NICK = SimpleNamespace(id="2" * 24, name="Nicholas", email="n@x.io", slackUserId="U0NICK0001", isActive=True)
PUMA = SimpleNamespace(id="3" * 24, name="Puma", email="p@x.io", slackUserId="U0PUMA0001", isActive=True)


def rnd(**kw) -> DayRound:
    base = dict(interview_id=IV, round_id=R, start=START, company="Lumin Digital", stage="intro",
                profile="Brandon Mayer", position="Dev", round_no=1, method="video", link="https://meet.x/abc",
                caller="user", caller_name="Nicholas", caller_user=NICK, caller_id=NICK.id, confirmed=True,
                owner=JUSTIN, status="scheduled")
    base.update(kw)
    return DayRound(**base)


def entry(i, kind="user", name=None):
    return {"_id": f"{i:024d}", "kind": kind, "name": name or f"{kind}{i}", "color": "blue", "archived": False}


NICK_ENTRY = {"_id": NICK.id, "kind": "user", "name": "Nicholas", "color": "blue", "archived": False}
JUSTIN_ENTRY = {"_id": JUSTIN.id, "kind": "user", "name": "Justin", "color": "blue", "archived": False}


def build(r, **kw):
    kw.setdefault("directory", [NICK_ENTRY])
    kw.setdefault("managers", [PUMA])
    return post_blocks(r, origin=ORIGIN, **kw)


def lines(bs):
    return bs[0]["text"]["text"].split("\n")


def ids(bs):
    return [e["action_id"] for e in bs[1]["elements"]]


def test_post_reads_like_the_sketch():
    text, bs = build(rnd())
    assert text == "📞 Lumin Digital · Intro"
    assert [b["type"] for b in bs] == ["section", "actions"]
    assert lines(bs) == [
        f"📞 *Lumin Digital · Intro · {slack_time(START)} · Brandon Mayer*",
        "Owner <@U0JUSTIN01> · Caller: <@U0NICK0001> · Video meeting (<https://meet.x/abc|link>)",
    ]
    assert ids(bs) == ["coord_choose_caller", "coord_open_brief", "coord_open_interview"]
    by = {e["action_id"]: e for e in bs[1]["elements"]}
    assert by["coord_open_brief"]["url"] == f"https://app.x/interview/{IV}?brief={R}"
    assert by["coord_open_interview"]["url"] == f"https://app.x/interviews?panel={IV}"


@pytest.mark.parametrize("confirmed", [False, True])
def test_no_confirm_button_and_no_confirmed_wording(confirmed):
    """Assigning a caller confirms the round: the post has no Confirm step, whatever the stored flag says."""
    _, bs = build(rnd(confirmed=confirmed))
    assert "confirmed" not in lines(bs)[1]
    assert ids(bs) == ["coord_choose_caller", "coord_open_brief", "coord_open_interview"]
    ext = rnd(caller="external", caller_name="Pietro", caller_user=None, caller_id="e1", confirmed=confirmed)
    assert "coord_confirm" not in ids(build(ext)[1])


def test_tbd_tags_managers():
    _, bs = build(rnd(caller="tbd", caller_name="TBD", caller_user=None, caller_id="", method="", link=""))
    assert lines(bs)[1] == "Owner <@U0JUSTIN01> · ⚠️ No caller yet — <@U0PUMA0001>"
    _, bs = build(rnd(caller="tbd", caller_user=None, caller_id="", method="", link=""), managers=[])
    assert lines(bs)[1] == "Owner <@U0JUSTIN01> · ⚠️ No caller yet"
    assert "coord_confirm" not in ids(bs)


def test_self_round():
    _, bs = build(rnd(caller="self", caller_name="Self", caller_user=None, caller_id="", method="", link=""))
    assert lines(bs)[1] == "Owner <@U0JUSTIN01> · No caller (Self)"
    assert bs[1]["elements"][0]["initial_option"]["value"] == f"{IV}:{R}:self"
    assert "coord_confirm" not in ids(bs)


def test_external_caller_is_the_initial_option():
    ext = entry(7, "external", "Pietro & Co")
    r = rnd(caller="external", caller_name="Pietro & Co", caller_user=None, caller_id=ext["_id"], confirmed=False)
    _, bs = build(r, directory=[NICK_ENTRY, ext])
    select = bs[1]["elements"][0]
    assert select["initial_option"]["value"] == f"{IV}:{R}:{ext['_id']}"
    assert select["initial_option"]["text"] == {"type": "plain_text", "text": "Pietro & Co"}
    assert "Caller: Pietro &amp; Co · Video meeting" in lines(bs)[1]


def test_unlisted_caller_has_no_initial_option():
    _, bs = build(rnd(caller_id="9" * 24))
    assert "initial_option" not in bs[1]["elements"][0]
    _, bs = build(rnd(caller="tbd", caller_id="", caller_user=None))
    assert "initial_option" not in bs[1]["elements"][0]


def test_completed_offers_the_result():
    _, bs = build(rnd(status="completed"))
    assert lines(bs)[2] == "Completed · waiting for the result"
    assert ids(bs) == ["coord_result_passed", "coord_result_rejected", "coord_open_brief", "coord_open_interview"]
    passed, rejected = bs[1]["elements"][:2]
    assert (passed["style"], rejected["style"]) == ("primary", "danger")
    assert passed["value"] == rejected["value"] == f"{IV}:{R}"


@pytest.mark.parametrize("status,line", [("passed", "🎉 Passed"), ("rejected", "❌ Rejected"),
                                          ("canceled", "❌ Canceled")])
def test_ended_round_has_only_links(status, line):
    _, bs = build(rnd(status=status))
    assert lines(bs)[2] == line
    assert ids(bs) == ["coord_open_brief", "coord_open_interview"]
    if status in ("canceled", "rejected"):
        assert lines(bs)[0].startswith("❌ ~") and lines(bs)[0].endswith("~")
    else:
        assert lines(bs)[0].startswith("📞 *")


def test_select_groups_and_limits():
    directory = [NICK_ENTRY, JUSTIN_ENTRY] + [entry(i) for i in range(10, 138)]
    directory += [entry(999, "external", "x" * 90)] + [entry(i, "external") for i in range(200, 319)]
    assert sum(e["kind"] == "user" for e in directory) == 130 and len(directory) == 250
    _, bs = build(rnd(), directory=directory)
    select = bs[1]["elements"][0]
    assert select["type"] == "static_select" and select["action_id"] == "coord_choose_caller"
    assert select["placeholder"] == {"type": "plain_text", "text": "Choose caller"}
    groups = select["option_groups"]
    assert [g["label"]["text"] for g in groups] == ["People", "External callers", "No caller"]
    assert all(g["label"]["type"] == "plain_text" for g in groups)
    assert all(len(g["options"]) <= 100 for g in groups)
    assert [len(g["options"]) for g in groups] == [100, 100, 1]
    assert groups[2]["options"][0]["value"] == f"{IV}:{R}:self"
    values = [o["value"] for g in groups for o in g["options"]]
    assert not any(JUSTIN.id in v for v in values)
    assert all(len(v) <= 150 for v in values)
    long = groups[1]["options"][0]["text"]["text"]
    assert len(long) == 75 and long.endswith("…")
    assert select["initial_option"]["value"] == f"{IV}:{R}:{NICK.id}"
    assert select["initial_option"]["text"]["text"] == "Nicholas"


def test_option_text_is_cut_without_escaping():
    directory = [NICK_ENTRY, entry(6, "external", "&" * 80), entry(7, "external", "")]
    _, bs = build(rnd(), directory=directory)
    options = bs[1]["elements"][0]["option_groups"][1]["options"]
    amp = options[0]["text"]["text"]
    assert len(amp) == 75 and amp.endswith("…") and "&amp;" not in amp
    assert options[1]["text"]["text"]  # a blank name still gives Slack a non-empty label


def test_empty_groups_are_left_out():
    _, bs = build(rnd(caller="self", caller_user=None, caller_id=""), directory=[])
    groups = bs[1]["elements"][0]["option_groups"]
    assert [g["label"]["text"] for g in groups] == ["No caller"]


def test_post_escapes_user_text():
    fallback, bs = build(rnd(company="<!channel> & Co"))
    text = bs[0]["text"]["text"]
    assert "&lt;!channel&gt; &amp; Co" in text and "<!channel>" not in text
    assert "<!channel>" not in fallback


def test_section_capped():
    _, bs = build(rnd(profile="P" * 5000))
    assert len(bs[0]["text"]["text"]) <= 3000


def test_round_without_a_time_does_not_crash():
    _, bs = build(rnd(start=None))
    assert "TBD" in lines(bs)[0]
