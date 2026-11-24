"""Team channel "Interview scheduled" posts: sync_round / sync_team_posts / sweep_team_posts with Slack and the files
faked (mongomock DB with real unique indexes, so the claim is tested for real)."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.caller_teams import files as team_files  # noqa: E402
from app.caller_teams import slack as team_slack  # noqa: E402
from app.caller_teams import sync  # noqa: E402
from app.caller_teams.files import NO_RESUME_NOTE  # noqa: E402
from app.models.caller import Caller  # noqa: E402
from app.models.caller_team import CallerTeam, TeamRoundPost  # noqa: E402
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent  # noqa: E402

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
START = NOW + timedelta(days=1)
ACC = "64b000000000000000000002"


class FakeSlack:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.uploads: list[tuple[str, str, list, str]] = []
        self.fail: set[str] = set()
        self.n = 0

    async def api(self, method, body):
        await asyncio.sleep(0)  # let concurrent syncs interleave
        self.calls.append((method, body))
        if method in self.fail:
            raise RuntimeError("slack down")
        self.n += 1
        return {"ok": True, "ts": f"{self.n}.000"}

    async def upload(self, channel, thread_ts, files, comment):
        if "upload" in self.fail:
            raise RuntimeError("upload failed")
        self.uploads.append((channel, thread_ts, files, comment))

    def methods(self):
        return [m for m, _ in self.calls]


@pytest.fixture
async def env(monkeypatch):
    await init_beanie(
        database=AsyncMongoMockClient()["t"], document_models=[Interview, Caller, CallerTeam, TeamRoundPost],
    )
    fake = FakeSlack()
    monkeypatch.setattr(team_slack, "api", fake.api)
    monkeypatch.setattr(team_slack, "upload_files", fake.upload)
    monkeypatch.setattr(team_slack, "callers_configured", lambda: True)
    resume = {"note": None, "name": "Acme.pdf"}

    async def round_files(iv, round_id, tz):
        files = [("Brief - Acme.pdf", b"%PDF brief")]
        if resume["note"]:
            return files, resume["note"]
        return files + [(resume["name"], b"%PDF resume")], None

    monkeypatch.setattr(team_files, "round_files", round_files)
    jamil = await Caller(name="Jamil", nameKey="jamil", slackUserId="U_JAMIL").insert()
    team = await CallerTeam(
        name="Alpha", nameKey="alpha", managerCallerId=str(jamil.id), channelId="CTEAM", reminderChannelId="CREM",
    ).insert()
    other = await CallerTeam(
        name="Beta", nameKey="beta", managerCallerId=str(jamil.id), channelId="CBETA", reminderChannelId="CREM2",
    ).insert()
    mk = lambda name, sid, t: Caller(name=name, nameKey=name.lower(), slackUserId=sid, teamId=t)  # noqa: E731
    people = {
        "omar": await mk("Omar", "U_OMAR", str(team.id)).insert(),
        "sara": await mk("Sara", "U_SARA", str(team.id)).insert(),
        "bilal": await mk("Bilal", None, str(team.id)).insert(),
        "zed": await mk("Zed", "U_ZED", None).insert(),
        "bea": await mk("Bea", "U_BEA", str(other.id)).insert(),
    }
    return {"slack": fake, "team": team, "other": other, "people": people, "resume": resume}


async def make_iv(env, who="omar", *, at=START, status="scheduled", kind="external"):
    caller = InterviewCaller(
        enabled=True, callerName=who.capitalize(), callerId=str(env["people"][who].id), callerKind=kind,
        method="video", methodValue="https://meet.example/x",
    )
    rnd = InterviewStageEvent(id="r1", stage="tech_round_1", at=NOW, scheduledAt=at, status=status, caller=caller)
    return await Interview(
        accountId=ACC, createdBy=ACC, scheduledAt=at or NOW, companyName="Acme", stageHistory=[rnd.model_dump()],
    ).insert()


async def assign(env, iv, who):
    iv.stageHistory[0].caller.callerId = str(env["people"][who].id)
    iv.stageHistory[0].caller.callerName = who.capitalize()
    await iv.save()


async def run_round(iv):
    callers = {str(c.id): c for c in await Caller.find_all().to_list()}
    teams = {str(t.id): t for t in await CallerTeam.find_all().to_list()}
    iv = await Interview.get(iv.id)
    return await sync.sync_round(iv, iv.stageHistory[0], "r1", NOW, callers=callers, teams=teams)


def replies(fake):
    return [b for m, b in fake.calls if m == "chat.postMessage" and b.get("thread_ts")]


def all_text(body):
    return body.get("text", "") + str(body.get("blocks", ""))


async def test_assigning_member_posts_and_uploads(env):
    fake = env["slack"]
    iv = await make_iv(env)
    await sync.sync_team_posts(str(iv.id), now=NOW)
    assert fake.methods() == ["chat.postMessage"]
    body = fake.calls[0][1]
    assert body["channel"] == "CTEAM" and "thread_ts" not in body
    assert "<@U_OMAR>" in all_text(body) and "<@U_JAMIL>" in all_text(body)
    assert "Acme" in all_text(body) and "Interview scheduled" in all_text(body)
    assert "elements" not in str(body.get("blocks"))  # no buttons
    [(channel, ts, files, _)] = fake.uploads
    assert (channel, ts) == ("CTEAM", "1.000") and len(files) == 2
    post = await TeamRoundPost.find_one(TeamRoundPost.roundId == "r1")
    assert post.ts == "1.000" and post.state == "scheduled" and post.filesFor == int(START.timestamp())
    assert post.callerId == str(env["people"]["omar"].id) and post.teamId == str(env["team"].id)


async def test_second_sync_is_noop(env):
    fake = env["slack"]
    iv = await make_iv(env)
    assert await run_round(iv) == "posted"
    fake.calls.clear()
    fake.uploads.clear()
    assert await run_round(iv) == "none"
    assert fake.calls == [] and fake.uploads == []


async def test_concurrent_syncs_post_once(env):
    fake = env["slack"]
    iv = await make_iv(env)
    await asyncio.gather(sync.sync_team_posts(str(iv.id), now=NOW), sync.sync_team_posts(str(iv.id), now=NOW))
    assert fake.methods().count("chat.postMessage") == 1 and len(fake.uploads) == 1
    assert await TeamRoundPost.find_all().count() == 1


async def test_other_member_assigned_edits_and_replies(env):
    fake = env["slack"]
    iv = await make_iv(env)
    await run_round(iv)
    fake.calls.clear()
    fake.uploads.clear()
    await assign(env, iv, "sara")
    assert await run_round(iv) == "replied"
    assert fake.methods() == ["chat.update", "chat.postMessage"]
    assert fake.calls[0][1]["ts"] == "1.000" and "<@U_SARA>" in all_text(fake.calls[0][1])
    [reply] = replies(fake)
    assert reply["thread_ts"] == "1.000" and "<@U_SARA>" in reply["text"] and "<@U_JAMIL>" in reply["text"]
    assert fake.uploads == []
    assert (await TeamRoundPost.find_one()).callerId == str(env["people"]["sara"].id)


async def test_time_moved_replies_and_new_brief(env):
    fake = env["slack"]
    iv = await make_iv(env)
    await run_round(iv)
    fake.calls.clear()
    fake.uploads.clear()
    later = START + timedelta(hours=2)
    iv.stageHistory[0].scheduledAt = later
    await iv.save()
    assert await run_round(iv) == "replied"
    [reply] = replies(fake)
    assert reply["text"].startswith("Moved to") and "<@U_OMAR>" in reply["text"] and "<@U_JAMIL>" in reply["text"]
    assert len(fake.uploads) == 1
    post = await TeamRoundPost.find_one()
    assert post.startEpoch == post.filesFor == int(later.timestamp())


async def test_time_first_set_from_tbd(env):
    fake = env["slack"]
    iv = await make_iv(env, at=None)
    assert await run_round(iv) == "posted"
    assert "Time TBD" in all_text(fake.calls[0][1])
    assert (await TeamRoundPost.find_one()).filesFor == -1 and len(fake.uploads) == 1
    fake.calls.clear()
    fake.uploads.clear()
    iv.stageHistory[0].scheduledAt = START
    await iv.save()
    assert await run_round(iv) == "replied"
    [reply] = replies(fake)
    assert reply["text"].startswith("Time set:") and "<@U_OMAR>" in reply["text"]
    assert len(fake.uploads) == 1 and (await TeamRoundPost.find_one()).filesFor == int(START.timestamp())


async def test_canceled_round(env):
    fake = env["slack"]
    iv = await make_iv(env)
    await run_round(iv)
    fake.calls.clear()
    iv.stageHistory[0].status = "canceled"
    await iv.save()
    assert await run_round(iv) == "canceled"
    assert fake.methods() == ["chat.update", "chat.postMessage"]
    assert "Canceled" in all_text(fake.calls[0][1])
    [reply] = replies(fake)
    assert "<@U_OMAR>" in reply["text"] and "<@U_JAMIL>" in reply["text"]
    assert (await TeamRoundPost.find_one()).state == "canceled"
    fake.calls.clear()
    assert await run_round(iv) == "none" and fake.calls == []


async def test_moved_off_team(env):
    fake = env["slack"]
    iv = await make_iv(env)
    await run_round(iv)
    fake.calls.clear()
    await assign(env, iv, "zed")
    assert await run_round(iv) == "off"
    assert fake.methods() == ["chat.update", "chat.postMessage"]
    assert "Moved off this team" in all_text(fake.calls[0][1])
    [reply] = replies(fake)
    assert "<@U_OMAR>" in reply["text"] and "<@U_JAMIL>" in reply["text"] and "<@U_ZED>" not in reply["text"]
    assert (await TeamRoundPost.find_one()).state == "off"
    fake.calls.clear()
    assert await run_round(iv) == "none" and fake.calls == []


async def test_comes_back_to_team_reuses_row(env):
    fake = env["slack"]
    iv = await make_iv(env)
    await run_round(iv)
    await assign(env, iv, "zed")
    await run_round(iv)
    fake.calls.clear()
    fake.uploads.clear()
    await assign(env, iv, "omar")
    assert await run_round(iv) == "replied"
    assert fake.methods() == ["chat.update", "chat.postMessage"] and len(fake.uploads) == 1
    post = await TeamRoundPost.find_one()
    assert post.state == "scheduled" and post.ts == "1.000" and await TeamRoundPost.find_all().count() == 1


async def test_moved_to_other_team_posts_fresh(env):
    fake = env["slack"]
    iv = await make_iv(env)
    await run_round(iv)
    fake.calls.clear()
    await assign(env, iv, "bea")
    await run_round(iv)
    old = await TeamRoundPost.find_one(TeamRoundPost.teamId == str(env["team"].id))
    new = await TeamRoundPost.find_one(TeamRoundPost.teamId == str(env["other"].id))
    assert old.state == "off" and new.state == "scheduled" and new.channel == "CBETA"
    assert [b["channel"] for m, b in fake.calls if m == "chat.postMessage" and "thread_ts" not in b] == ["CBETA"]


async def test_round_deleted_marks_off(env):
    fake = env["slack"]
    iv = await make_iv(env)
    await run_round(iv)
    fake.calls.clear()
    await iv.delete()
    await sync.sync_team_posts(str(iv.id), now=NOW, deleted={"r1"})
    assert "Moved off this team" in all_text(fake.calls[0][1])
    [reply] = replies(fake)
    assert "<@U_OMAR>" in reply["text"] and "<@U_JAMIL>" in reply["text"]
    assert (await TeamRoundPost.find_one()).state == "off"


async def test_member_left_team_via_sweep(env):
    iv = await make_iv(env)
    await run_round(iv)
    omar = env["people"]["omar"]
    omar.teamId = None
    await omar.save()
    assert await sync.sweep_team_posts(NOW) == {"off": 1}
    assert (await TeamRoundPost.find_one()).state == "off"
    assert await sync.sweep_team_posts(NOW) == {}  # an off post of a non-team round is left alone


async def test_sweep_posts_missed_rounds_and_skips_past(env):
    fake = env["slack"]
    await make_iv(env)
    await make_iv(env, at=NOW - timedelta(hours=1))
    assert await sync.sweep_team_posts(NOW) == {"posted": 1}
    assert fake.methods() == ["chat.postMessage"]
    assert await sync.sweep_team_posts(NOW) == {"none": 1}


async def test_archived_team_marks_off(env):
    iv = await make_iv(env)
    await run_round(iv)
    team = env["team"]
    team.archivedAt = NOW
    await team.save()
    assert await run_round(iv) == "off"


async def test_past_round_never_posted(env):
    fake = env["slack"]
    iv = await make_iv(env, at=NOW - timedelta(hours=1))
    assert await run_round(iv) == "skipped"
    assert fake.calls == [] and fake.uploads == [] and await TeamRoundPost.find_all().count() == 0


async def test_no_upload_for_past_round_with_post(env):
    fake = env["slack"]
    iv = await make_iv(env)
    env["slack"].fail.add("upload")
    await run_round(iv)
    env["slack"].fail.clear()
    callers = {str(c.id): c for c in await Caller.find_all().to_list()}
    teams = {str(t.id): t for t in await CallerTeam.find_all().to_list()}
    iv = await Interview.get(iv.id)
    later = START + timedelta(hours=2)
    assert await sync.sync_round(iv, iv.stageHistory[0], "r1", later, callers=callers, teams=teams) == "none"
    assert fake.uploads == []


async def test_failed_upload_retried(env):
    fake = env["slack"]
    iv = await make_iv(env)
    fake.fail.add("upload")
    assert await run_round(iv) == "posted"
    assert (await TeamRoundPost.find_one()).filesFor is None
    fake.fail.clear()
    await run_round(iv)
    assert len(fake.uploads) == 1


async def test_no_slack_id_uses_name(env):
    fake = env["slack"]
    iv = await make_iv(env, "bilal")
    await run_round(iv)
    text = all_text(fake.calls[0][1])
    assert "Bilal" in text and "<@>" not in text and "<@None>" not in text


async def test_token_unset_does_nothing(env, monkeypatch):
    fake = env["slack"]
    monkeypatch.setattr(team_slack, "callers_configured", lambda: False)
    iv = await make_iv(env)
    await sync.sync_team_posts(str(iv.id), now=NOW)
    assert await sync.sweep_team_posts(NOW) == {}
    assert fake.calls == [] and await TeamRoundPost.find_all().count() == 0


async def test_slack_failure_releases_claim(env):
    fake = env["slack"]
    fake.fail.add("chat.postMessage")
    iv = await make_iv(env)
    await sync.sync_team_posts(str(iv.id), now=NOW)
    assert await TeamRoundPost.find_all().count() == 0
    fake.fail.clear()
    assert await run_round(iv) == "posted"


async def test_stale_claim_taken_over(env):
    iv = await make_iv(env)
    await TeamRoundPost(
        teamId=str(env["team"].id), interviewId=str(iv.id), roundId="r1", channel="CTEAM", state="scheduled",
        createdAt=datetime.utcnow() - timedelta(minutes=5),
    ).insert()
    assert await run_round(iv) == "posted"
    assert (await TeamRoundPost.find_one()).ts == "1.000"


async def test_fresh_claim_skipped(env):
    iv = await make_iv(env)
    await TeamRoundPost(
        teamId=str(env["team"].id), interviewId=str(iv.id), roundId="r1", channel="CTEAM", state="scheduled",
    ).insert()
    assert await run_round(iv) == "skipped" and env["slack"].calls == []


async def test_resume_missing_note_in_thread(env):
    fake = env["slack"]
    env["resume"]["note"] = NO_RESUME_NOTE
    iv = await make_iv(env)
    await run_round(iv)
    [(_, _, files, comment)] = fake.uploads
    assert len(files) == 1 and NO_RESUME_NOTE in comment


async def test_uploaded_file_names_are_sanitised(env):
    fake = env["slack"]
    env["resume"]["name"] = "Acme/Inc: CV.pdf"
    iv = await make_iv(env)
    await run_round(iv)
    names = [n for n, _ in fake.uploads[0][2]]
    assert all("/" not in n and ":" not in n for n in names) and names[1].endswith(".pdf")


async def test_interview_save_starts_team_sync(monkeypatch):
    from app.routers import interviews

    thread = AsyncMock(return_value=False)
    team = AsyncMock()
    monkeypatch.setattr(interviews, "sync_caller_thread", thread)
    monkeypatch.setattr(interviews, "sync_team_posts", team)
    monkeypatch.setattr(interviews, "_refresh_day_post", lambda *a, **k: None)
    iv = Interview.model_construct(id="iv1", stageHistory=[])
    await interviews._sync_caller_posts(iv, {"r1": None}, added={"r1"})
    await interviews._sync_caller_posts(iv, {}, deleted={"r2"})
    await interviews.drain_day_post_refreshes()
    assert thread.await_args_list[0].args == (iv, {"r1": None})
    assert thread.await_args_list[0].kwargs == {"added": {"r1"}, "deleted": set()}
    assert [c.args for c in team.await_args_list] == [("iv1",), ("iv1",)]
    assert [c.kwargs for c in team.await_args_list] == [{"deleted": set()}, {"deleted": {"r2"}}]
