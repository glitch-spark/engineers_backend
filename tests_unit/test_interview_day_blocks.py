"""Block Kit rounds for the interview day post (pure builder; loader against mongomock)."""

import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from beanie import PydanticObjectId, init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app.interview_day.blocks import day_messages, fallback_text, round_blocks  # noqa: E402
from app.interview_day.rounds import DayRound, load_rounds  # noqa: E402
from app.models import Account, Caller, UserExtra  # noqa: E402
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent  # noqa: E402
from app.models.interview_question import InterviewQuestion  # noqa: E402
from app.slack_bot import slack_time  # noqa: E402

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

ORIGIN = "https://app.test"
START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
OWNER = SimpleNamespace(name="Pedro", email="p@x.io", slackUserId="U0OWNER001", isActive=True)
CALLER = SimpleNamespace(name="Sam", email="s@x.io", slackUserId="U0CALLER01", isActive=True)
MANAGER = SimpleNamespace(name="Maria", email="m@x.io", slackUserId="U0MANAGER1", isActive=True)


def rnd(**kw) -> DayRound:
    base = dict(interview_id="IV", round_id="R", start=START, company="Acme", stage="intro", profile="Ian Meron",
                position="Senior Engineer", round_no=1, method="video", link="https://meet.example/x",
                caller="self", caller_name="Self", caller_user=None, confirmed=False, owner=OWNER, thread_link=None)
    base.update(kw)
    return DayRound(**base)


def blocks(r, **kw):
    kw.setdefault("managers", [MANAGER])
    return round_blocks(r, origin=ORIGIN, **kw)


def text_of(bs):
    return bs[0]["text"]["text"]


def buttons(bs):
    return {b["text"]["text"]: b for b in bs[1]["elements"]}


# --- builder --------------------------------------------------------------------------------------


def test_self_round_tags_owner_and_has_two_buttons():
    bs = blocks(rnd())
    assert [b["type"] for b in bs] == ["section", "actions"]
    assert bs[0]["text"]["type"] == "mrkdwn"
    text = text_of(bs)
    assert "Self · <@U0OWNER001>" in text
    lines = text.splitlines()
    assert lines[0] == f"*{slack_time(START, with_day=False)} · Acme · Intro · Ian Meron*"
    assert lines[1] == "Senior Engineer · Round 1 · Video meeting (<https://meet.example/x|link>)"
    btn = buttons(bs)
    assert list(btn) == ["Download brief", "Open interview"]
    assert btn["Download brief"]["url"] == "https://app.test/interview/IV?brief=R"
    assert btn["Open interview"]["url"] == "https://app.test/interviews?panel=IV"


def test_user_caller_tagged_with_owner_cc():
    r = rnd(caller="user", caller_name="Sam", caller_user=CALLER)
    bs = blocks(r)
    assert "<@U0CALLER01> (caller)" in text_of(bs) and "owner <@U0OWNER001>" in text_of(bs)
    assert "⏳ not confirmed" in text_of(bs)
    confirm = buttons(bs)["Confirm"]
    assert confirm["action_id"] == "interview_confirm" and confirm["value"] == "IV:R"
    assert list(buttons(bs)) == ["Download brief", "Open interview", "Confirm"]

    done = blocks(rnd(caller="user", caller_name="Sam", caller_user=CALLER, confirmed=True))
    assert "✅ confirmed" in text_of(done) and "Confirm" not in buttons(done)


def test_tbd_round_flags_managers_and_offers_assign():
    bs = blocks(rnd(caller="tbd", caller_name="TBD"))
    assert "No caller yet" in text_of(bs) and "<@U0MANAGER1>" in text_of(bs)
    btn = buttons(bs)
    assert btn["Assign caller"]["url"] == "https://app.test/interviews/callers?round=IV:R"
    assert "Confirm" not in btn


def test_external_unconfirmed_tags_managers():
    bs = blocks(rnd(caller="external", caller_name="Pietro"))
    assert "caller Pietro (external)" in text_of(bs)
    assert "⏳ not confirmed — <@U0MANAGER1>" in text_of(bs)
    assert "Confirm" in buttons(bs) and "Assign caller" not in buttons(bs)
    done = blocks(rnd(caller="external", caller_name="Pietro", confirmed=True))
    assert "✅ confirmed" in text_of(done) and "<@U0MANAGER1>" not in text_of(done) and "Confirm" not in buttons(done)


@pytest.mark.parametrize("bad", [{"isActive": False}, {"slackUserId": None}])
def test_inactive_or_unlinked_people_are_named_not_tagged(bad):
    person = SimpleNamespace(**{**vars(CALLER), **bad})
    owner = SimpleNamespace(**{**vars(OWNER), **bad})
    text = text_of(blocks(rnd(caller="user", caller_name="Sam", caller_user=person, owner=owner), managers=[]))
    assert "<@" not in text
    assert "Sam (caller)" in text and "owner Pedro" in text


def test_starting_in_header():
    assert text_of(blocks(rnd(), starting_in=30)).startswith("⏰ *Starting in 30 min*")
    assert "Starting in" not in text_of(blocks(rnd()))


def test_thread_link_only_on_caller_rounds_and_text_is_escaped():
    r = rnd(caller="user", caller_name="Sam", caller_user=CALLER, thread_link="https://slack.example/p1",
            company="A&B <x>", method="phone_hushed", link="+1 555")
    text = text_of(blocks(r))
    assert text.endswith(" · <https://slack.example/p1|thread>")
    assert "A&amp;B &lt;x&gt;" in text and "Phone (Hushed) (+1 555)" in text
    assert "thread" not in text_of(blocks(rnd(thread_link="https://slack.example/p1")))


def test_thirty_rounds_split_under_fifty_blocks():
    rounds = [rnd(round_id=f"R{i}", start=START + timedelta(minutes=i)) for i in range(30)]
    msgs = day_messages(date(2026, 10, 7), rounds, managers=[MANAGER], origin=ORIGIN)
    assert len(msgs) >= 2 and all(len(m) <= 50 for m in msgs)
    assert msgs[0][0]["type"] == "header" and all(m[0]["type"] != "header" for m in msgs[1:])
    for m in msgs:  # a round's section is always followed by its actions block
        kinds = [b["type"] for b in m if b["type"] != "divider"]
        assert kinds.count("section") == kinds.count("actions")
        assert [k for k in kinds if k != "header"] == ["section", "actions"] * kinds.count("section")
    seen = [b["elements"][0]["url"] for m in msgs for b in m if b["type"] == "actions"]
    assert seen == [f"https://app.test/interview/IV?brief=R{i}" for i in range(30)]


def test_empty_day_and_fallback():
    msgs = day_messages(date(2026, 11, 13), [], managers=[], origin=ORIGIN)
    assert len(msgs) == 1 and len(msgs[0]) == 1 and msgs[0][0]["text"]["text"] == "No interviews today."
    one = day_messages(date(2026, 11, 13), [rnd()], managers=[], origin=ORIGIN)
    assert one[0][0]["text"]["text"] == "⏰ Interviews today — Fri Nov 13"
    assert fallback_text(date(2026, 11, 13), 4) == "Interviews today — Fri Nov 13 · 4 rounds"
    assert fallback_text(date(2026, 11, 13), 1) == "Interviews today — Fri Nov 13 · 1 round"


# --- loader ---------------------------------------------------------------------------------------


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["interview-day-unit"]
    await init_beanie(database=database, document_models=[Interview, UserExtra, Account, InterviewQuestion, Caller])
    return database


def ev(stage="intro", at=START, status="scheduled", caller=None, **kw):
    return InterviewStageEvent(stage=stage, at=at, scheduledAt=at, status=status, caller=caller, **kw).model_dump()


async def test_load_rounds_skips_home_assessment_and_ended(db):
    owner = await UserExtra(name="Pedro", email="p@x.io", slackUserId="U0OWNER001").insert()
    sam = await UserExtra(name="Sam", email="s@x.io", slackUserId="U0CALLER01", isActive=False).insert()
    acct = await Account(name="Ian Meron", createdBy=owner.id).insert()
    later = START + timedelta(hours=2)
    iv = await Interview(
        accountId=acct.id, createdBy=owner.id, scheduledAt=START, companyName="Acme", appliedPosition="Dev",
        stageHistory=[
            ev("home_assessment"),
            ev("intro", START + timedelta(days=3)),  # round 2, outside the window
            ev("tech_round_1", later, caller=InterviewCaller(
                enabled=True, callerName="Sam", callerId=str(sam.id), callerKind="user", method="video",
                methodValue="https://meet.example/x")),
            ev("final", START, status="canceled"),
            ev("panel", START, status="rejected"),
            ev("cultural", START),
        ],
    ).insert()
    other = await Interview(
        accountId=acct.id, createdBy=owner.id, scheduledAt=START, companyName="Beta",
        stageHistory=[ev("intro", START + timedelta(minutes=5), caller=InterviewCaller(enabled=True))],
    ).insert()

    got = await load_rounds(START - timedelta(hours=1), START + timedelta(hours=5))
    assert [(r.company, r.stage) for r in got] == [("Acme", "cultural"), ("Beta", "intro"), ("Acme", "tech_round_1")]
    cultural, beta, tech = got
    assert cultural.round_no == 6 and cultural.caller == "self" and cultural.profile == "Ian Meron"
    assert cultural.owner.name == "Pedro" and cultural.position == "Dev" and cultural.thread_link is None
    assert cultural.start.tzinfo is not None and cultural.interview_id == str(iv.id)
    assert beta.caller == "tbd" and beta.caller_name == "TBD" and beta.round_no == 1
    assert beta.interview_id == str(other.id)
    assert tech.round_no == 3 and tech.caller == "user" and tech.caller_user.name == "Sam"
    assert tech.caller_user.isActive is False and tech.caller_name == "Sam"
    assert tech.method == "video" and tech.link == "https://meet.example/x"
    assert tech.round_id == iv.stageHistory[2].id
    assert await load_rounds(START + timedelta(days=10), START + timedelta(days=11)) == []


async def test_load_rounds_external_caller_and_confirmed(db):
    owner = await UserExtra(name="Pedro", email="p@x.io").insert()
    pietro = await Caller(name="Pietro", nameKey="pietro").insert()
    await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[ev(caller=InterviewCaller(enabled=True, callerName="Pietro", callerId=str(pietro.id),
                                                callerKind="external"), confirmed=True)],
    ).insert()
    (r,) = await load_rounds(START - timedelta(hours=1), START + timedelta(hours=1))
    assert r.caller == "external" and r.caller_name == "Pietro" and r.caller_user is None and r.confirmed is True
    assert r.profile == ""


# --- limits and link text -------------------------------------------------------------------------


def test_section_text_stays_under_slack_limit_with_huge_typed_fields():
    r = rnd(link="Join here: " + "x" * 5000, company="C" * 4000, position="P&" * 3000, profile="Q" * 4000,
            caller="external", caller_name="N" * 4000, thread_link="https://slack.example/p1")
    text = text_of(blocks(r))
    assert len(text) <= 3000
    assert "…" in text and "Round 1" in text
    assert text.count("<") == text.count(">")  # no token cut in half
    url = rnd(link="https://meet.example/" + "a" * 5000)
    assert len(text_of(blocks(url))) <= 3000 and "<https://meet.example" not in text_of(blocks(url))


def test_cap_never_ends_inside_a_token():
    from app.interview_day.blocks import _cap

    cut = _cap("a" * 2990 + " <https://x.example|link> tail")
    assert len(cut) <= 3000 and "<https" not in cut
    assert len(_cap("&amp;" * 700)) <= 3000 and not _cap("&amp;" * 700).replace("…", "").endswith(("&", "&a", "&am", "&amp"))


def test_unlinkable_http_link_is_shown_as_escaped_text():
    text = text_of(blocks(rnd(link="https://meet.example/a b<c>|d")))
    assert "(https://meet.example/a b&lt;c&gt;|d)" in text and "(link)" not in text


async def test_self_round_shows_no_stale_method_or_link(db):
    owner = await UserExtra(name="Pedro", email="p@x.io").insert()
    await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[ev(caller=InterviewCaller(enabled=False, method="video", methodValue="https://old.example/x"))],
    ).insert()
    (r,) = await load_rounds(START - timedelta(hours=1), START + timedelta(hours=1))
    assert r.caller == "self" and r.method == "" and r.link == ""
    text = text_of(blocks(r))
    assert "Video" not in text and "old.example" not in text
