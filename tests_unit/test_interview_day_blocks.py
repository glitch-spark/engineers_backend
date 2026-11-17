"""Block Kit rounds for the interview day post (pure builder; loader against mongomock)."""

import os
import re
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
    assert "confirmed" not in text_of(bs)  # assigning confirms: no Confirm step, no confirmed wording
    assert list(buttons(bs)) == ["Download brief", "Open interview"]

    done = blocks(rnd(caller="user", caller_name="Sam", caller_user=CALLER, confirmed=True))
    assert "confirmed" not in text_of(done) and list(buttons(done)) == ["Download brief", "Open interview"]


def test_tbd_round_flags_managers_and_offers_assign():
    bs = blocks(rnd(caller="tbd", caller_name="TBD"))
    assert "No caller yet" in text_of(bs) and "<@U0MANAGER1>" in text_of(bs)
    btn = buttons(bs)
    assert btn["Assign caller"]["url"] == "https://app.test/interviews/callers?round=IV:R"
    assert "Confirm" not in btn


@pytest.mark.parametrize("confirmed", [False, True])
def test_external_caller_has_no_confirm_and_no_manager_tag(confirmed):
    bs = blocks(rnd(caller="external", caller_name="Pietro", confirmed=confirmed))
    assert "caller Pietro (external) · owner" in text_of(bs)
    assert "confirmed" not in text_of(bs) and "<@U0MANAGER1>" not in text_of(bs)
    assert list(buttons(bs)) == ["Download brief", "Open interview"]


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


PANDA = SimpleNamespace(name="Panda", email="pa@x.io", slackUserId="U0PANDA001", isActive=True)
JUSTIN = SimpleNamespace(name="Justin", email="j@x.io", slackUserId="U0JUSTIN01", isActive=True)
NOV17 = datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc)


def _t(minutes):
    at = NOV17 + timedelta(minutes=minutes)
    return at, slack_time(at, with_day=False)


def test_day_post_groups_rounds_by_owner_one_line_each():
    """Two owners, Self, an assigned caller, TBD and a canceled round: the exact text."""
    (t1, s1), (t2, s2), (t3, s3), (t4, s4), (t5, s5) = (_t(m) for m in (0, 30, 60, 90, 120))
    rounds = [
        rnd(round_id="R2", interview_id="IV2", start=t2, company="Proofpoint", stage="tech_round_1",
            interviewer="Shane Kelly", caller="user", caller_name="Nicholas", caller_user=CALLER,
            owner=PANDA, owner_id="pa"),
        rnd(round_id="R1", interview_id="IV1", start=t1, company="Workster & Jobs", interviewer="Liviu <Ciulei>",
            owner=PANDA, owner_id="pa"),
        rnd(round_id="R3", interview_id="IV3", start=t3, company="Hexagon", caller="tbd", caller_name="TBD",
            owner=PANDA, owner_id="pa"),
        rnd(round_id="R5", interview_id="IV5", start=t5, company="Globex", caller="external", caller_name="Pietro",
            owner=JUSTIN, owner_id="ju"),
        rnd(round_id="R4", interview_id="IV4", start=t4, company="Initech", caller="user", caller_name="Sam",
            caller_user=CALLER, owner=JUSTIN, owner_id="ju", status="canceled"),
    ]
    msgs = day_messages(date(2026, 11, 17), rounds, origin=ORIGIN + "/")
    assert len(msgs) == 1 and [b["type"] for b in msgs[0]] == ["header", "section", "section"]
    assert msgs[0][0]["text"]["text"] == "⏰ Interviews today — Tue Nov 17 · 5 interviews · 2 with a caller"
    app = "https://app.test"
    assert msgs[0][1]["text"]["text"] == "\n".join([
        "<@U0PANDA001> — 3",
        f"• {s1}  Workster &amp; Jobs · Intro · Liviu &lt;Ciulei&gt; — Self · "
        f"<{app}/interview/IV1?brief=R1|Brief> · <{app}/interviews?panel=IV1|Open>",
        f"• {s2}  Proofpoint · Tech Round 1 · Shane Kelly — 📞 <@U0CALLER01> · "
        f"<{app}/interview/IV2?brief=R2|Brief> · <{app}/interviews?panel=IV2|Open>",
        f"• {s3}  Hexagon · Intro — 📞 No caller yet · "
        f"<{app}/interview/IV3?brief=R3|Brief> · <{app}/interviews?panel=IV3|Open>",
    ])
    assert msgs[0][2]["text"]["text"] == "\n".join([
        "<@U0JUSTIN01> — 2",
        f"• ~{s4}  Initech · Intro~ — ❌ Canceled · "
        f"<{app}/interview/IV4?brief=R4|Brief> · <{app}/interviews?panel=IV4|Open>",
        f"• {s5}  Globex · Intro — 📞 Pietro · "
        f"<{app}/interview/IV5?brief=R5|Brief> · <{app}/interviews?panel=IV5|Open>",
    ])
    assert not any(b["type"] == "actions" for m in msgs for b in m)  # links, no buttons


@pytest.mark.parametrize("origin", ["", "  ", "app.test", "ftp://app.test"])
def test_without_a_usable_app_url_the_links_are_left_out(origin):
    msgs = day_messages(date(2026, 11, 17), [rnd(owner_id="o", interviewer="")], origin=origin)
    line = msgs[0][1]["text"]["text"].split("\n")[1]
    assert line.endswith("Acme · Intro — Self") and "Brief" not in line and "Open" not in line


def test_owners_ordered_by_first_round_then_name_and_unknown_owner():
    early, _ = _t(0)
    late, _ = _t(60)
    rounds = [rnd(round_id="a", start=late, owner=JUSTIN, owner_id="ju"),
              rnd(round_id="b", start=early, owner=None, owner_id="gone"),
              rnd(round_id="c", start=late, owner=PANDA, owner_id="pa")]
    heads = [b["text"]["text"].split("\n")[0] for b in day_messages(date(2026, 11, 17), rounds, origin=ORIGIN)[0][1:]]
    assert heads == ["Unknown owner — 1", "<@U0JUSTIN01> — 1", "<@U0PANDA001> — 1"]


def test_thirty_rounds_stay_within_slack_limits():
    """One owner with 30 long rounds splits into "(cont.)" sections; 30 owners stay under 50 blocks per message."""
    long_name = "Z" * 200
    one_owner = [rnd(round_id=f"R{i}", start=START + timedelta(minutes=i), company=long_name, interviewer=long_name,
                     owner_id="o") for i in range(30)]
    msgs = day_messages(date(2026, 10, 7), one_owner, origin=ORIGIN)
    sections = [b["text"]["text"] for m in msgs for b in m if b["type"] == "section"]
    assert len(sections) >= 2 and all(len(t) <= 3000 for t in sections)
    assert sections[0].startswith("<@U0OWNER001> — 30") and all(t.startswith("<@U0OWNER001> (cont.)") for t in sections[1:])
    briefs = re.findall(r"brief=(R\d+)\|", "\n".join(sections))
    assert briefs == [f"R{i}" for i in range(30)]  # every round once, in time order

    many = [rnd(round_id=f"R{i}", start=START + timedelta(minutes=i), owner_id=f"o{i}") for i in range(30)]
    msgs = day_messages(date(2026, 10, 7), many, origin=ORIGIN)
    assert all(len(m) <= 50 for m in msgs) and msgs[0][0]["type"] == "header"
    assert sum(len(m) for m in msgs) == 31

    sixty = [rnd(round_id=f"R{i}", start=START + timedelta(minutes=i), owner_id=f"o{i}") for i in range(60)]
    msgs = day_messages(date(2026, 10, 7), sixty, origin=ORIGIN)
    assert [len(m) for m in msgs] == [50, 11] and all(m[0]["type"] != "header" for m in msgs[1:])


def test_empty_day_and_fallback():
    msgs = day_messages(date(2026, 11, 13), [], managers=[], origin=ORIGIN)
    assert len(msgs) == 1 and len(msgs[0]) == 1 and msgs[0][0]["text"]["text"] == "No interviews today."
    one = day_messages(date(2026, 11, 13), [rnd()], managers=[], origin=ORIGIN)
    assert one[0][0]["text"]["text"] == "⏰ Interviews today — Fri Nov 13 · 1 interview · 0 with a caller"
    assert fallback_text(date(2026, 11, 13), 4) == "Interviews today — Fri Nov 13 · 4 interviews"
    assert fallback_text(date(2026, 11, 13), 1) == "Interviews today — Fri Nov 13 · 1 interview"


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


async def test_typed_name_without_a_caller_id_is_a_named_external_caller_not_tbd(db):
    owner = await UserExtra(name="Pedro", email="p@x.io").insert()
    names = ["Sam", "TBD", "tbd", " ", ""]
    await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[ev(at=START + timedelta(minutes=i), caller=InterviewCaller(enabled=True, callerName=n))
                      for i, n in enumerate(names)],
    ).insert()
    got = await load_rounds(START - timedelta(hours=1), START + timedelta(hours=1))
    assert [(r.caller, r.caller_name) for r in got] == [
        ("external", "Sam"), ("tbd", "TBD"), ("tbd", "tbd"), ("tbd", " "), ("tbd", ""),
    ]
    assert "Assign caller" not in buttons(blocks(got[0])) and "Assign caller" in buttons(blocks(got[1]))


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


@pytest.mark.parametrize("kind", ["self", "tbd", "external", "user"])
def test_round_blocks_cap_escape_heavy_text_in_every_typed_part(kind):
    amp = "&" * 5000
    person = SimpleNamespace(name=amp, email=None, slackUserId=None, isActive=True)
    managers = [SimpleNamespace(name=f"{amp}{i}", email=None, slackUserId=None, isActive=True) for i in range(5)]
    r = rnd(company=amp, position=amp, profile=amp, caller=kind, caller_name=amp, caller_user=person, owner=person,
            link="Call " + amp, thread_link="https://slack.example/p1")
    text = text_of(blocks(r, managers=managers, starting_in=30))
    assert len(text) <= 3000
    last = text.rstrip("…")
    assert last.rfind("<") <= last.rfind(">") and last.rfind("&") <= last.rfind(";")
    assert text.startswith("⏰ *Starting in 30 min*")


# --- day_round / status / caller_id (coordination post) --------------------------------------------


async def test_load_rounds_unchanged(db):
    owner = await UserExtra(name="Pedro", email="p@x.io").insert()
    pietro = await Caller(name="Pietro", nameKey="pietro").insert()
    await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[ev(), ev("final", START + timedelta(minutes=5), status="completed"),
                      ev("panel", START + timedelta(minutes=9), caller=InterviewCaller(
                          enabled=True, callerName="Pietro", callerId=str(pietro.id), callerKind="external"))],
    ).insert()
    got = await load_rounds(START - timedelta(hours=1), START + timedelta(hours=1))
    assert [r.status for r in got] == ["scheduled", "completed", "scheduled"]
    assert [r.caller_id for r in got] == ["", "", str(pietro.id)]


async def test_day_round_tolerates_a_round_without_a_time(db):
    from app.interview_day.rounds import day_round

    owner = await UserExtra(name="Pedro", email="p@x.io").insert()
    legacy = InterviewStageEvent(stage="intro", at=START, scheduledAt=None, status="scheduled").model_dump()
    iv = Interview(accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
                   stageHistory=[legacy])
    r = day_round(iv, 1, iv.stageHistory[0], owner=owner, account=None, caller_user=None)
    assert r.start is None and r.status == "scheduled" and r.caller == "self" and r.caller_id == ""
