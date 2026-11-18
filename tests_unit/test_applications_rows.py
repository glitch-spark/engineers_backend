from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from bson import ObjectId

from app.applications.rows import board_of, build_rows, interview_outcome, match_interviews

ACC, OWN = ObjectId(), ObjectId()
T0 = datetime(2026, 9, 1, 10)


def bid(url="https://jobs.acme.com/1", kind="bidder", status="approved", at=T0, acc=ACC):
    return {"_id": ObjectId(), "kind": kind, "bidderId": ObjectId() if kind == "bidder" else None, "accountId": acc,
            "ownerId": OWN, "jobUrl": url, "firstAt": at, "status": status}


def resume(url="https://jobs.acme.com/1", at=T0, acc=ACC, **kw):
    return {"_id": ObjectId(), "userId": OWN, "accountId": acc, "jobUrl": url, "createdAt": at,
            "matchScore": {"total": 80, "band": "strong"}, "promptId": "p", "promptName": "P", "templateName": "T", **kw}


def rnd(stage, at=None, status="completed"):
    return NS(stage=stage, scheduledAt=at, status=status, caller=None, id=None)


def iv(rounds, url=None, acc=ACC, resume_id=None):
    *hist, tip = rounds
    return NS(id=ObjectId(), accountId=acc, jobUrl=url, stage=tip.stage, scheduledAt=tip.scheduledAt,
              status=tip.status, caller=None, stageHistory=hist,
              resumeLink=NS(resumeJobId=resume_id) if resume_id else None)


def test_board_of_examples():
    assert board_of("https://jobs.ashbyhq.com/x/1") == "ashbyhq.com"
    assert board_of("https://boards.greenhouse.io/a/jobs/1") == "greenhouse.io"
    assert board_of("https://acme.wd5.myworkdayjobs.com/en/job") == "myworkdayjobs.com"
    assert board_of("https://jobs.lever.co/acme/1") == "lever.co"
    assert board_of("https://www.linkedin.com/jobs/view/1") == "linkedin.com"
    assert board_of("https://careers.foo.co.uk/1") == "foo.co.uk"
    assert board_of(None) is None


def test_bidder_and_resume_merge_one_row_bidder_wins():
    b, r = bid(at=T0 + timedelta(days=1)), resume(url="https://www.jobs.acme.com/1?utm_source=x")
    rows = build_rows([b], [r], {ACC: {"region": "EU"}})
    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert row["by"] == "bidder" and row["bidderId"] == b["bidderId"] and row["appliedAt"] == T0
    assert row["score"] == 80 and row["region"] == "EU" and row["sources"] == ["bid", "resume"]
    assert row["board"] == "acme.com"


def test_rows_without_link_keyed_by_source():
    b, r = bid(url=None), resume(url=None)
    rows = build_rows([b], [r], {})
    assert set(rows) == {f"src:bids:{b['_id']}", f"src:resumeapplications:{r['_id']}"}


def test_rejected_bid_kept_with_status():
    rows = build_rows([bid(status="rejected")], [], {})
    assert next(iter(rows.values()))["bidStatus"] == "rejected"


def test_counts_as_bid_false_skipped():
    assert build_rows([], [resume(countsAsBid=False)], {}) == {}


def test_self_bid_is_self():
    assert next(iter(build_rows([bid(kind="self")], [], {}).values()))["by"] == "self"


def test_match_by_resume_link_then_job_key():
    r1, r2 = resume(url="https://a.com/1", acc=ObjectId()), resume(url="https://b.com/2")
    rows = build_rows([], [r1, r2], {})
    k1, k2 = list(rows)
    by_link = iv([rnd("intro", T0 + timedelta(days=3))], url="https://b.com/2", resume_id=str(r1["_id"]))
    by_url = iv([rnd("intro", T0 + timedelta(days=2))], url="https://b.com/2")
    nothing = iv([rnd("intro", T0)], url="https://zzz.com/9")
    m, u = match_interviews(rows, [by_link, by_url, nothing], {str(r1["_id"]): k1, str(r2["_id"]): k2})
    assert (m, u) == (2, 1)
    assert rows[k1]["interviewId"] == by_link.id and rows[k2]["interviewId"] == by_url.id


def test_job_link_with_tracking_params_matches():
    rows = build_rows([bid()], [], {})
    i = iv([rnd("intro", T0 + timedelta(days=1))], url="https://www.jobs.acme.com/1/?utm_source=li&gclid=2")
    assert match_interviews(rows, [i], {}) == (1, 0)


def test_earliest_interview_wins():
    rows = build_rows([bid()], [], {})
    late, early = (iv([rnd("intro", T0 + timedelta(days=d))], url="https://jobs.acme.com/1") for d in (9, 4))
    match_interviews(rows, [late, early], {})
    row = next(iter(rows.values()))
    assert row["interviewId"] == early.id and row["daysToInterview"] == 4


def test_days_to_interview_first_non_canceled_round():
    i = iv([rnd("intro", T0 + timedelta(days=2), "canceled"), rnd("intro_x", None), rnd("tech", T0 + timedelta(days=6))])
    rows = build_rows([bid()], [], {})
    i.jobUrl = "https://jobs.acme.com/1"
    match_interviews(rows, [i], {})
    assert next(iter(rows.values()))["daysToInterview"] == 6


def test_only_canceled_rounds_not_an_interview():
    i = iv([rnd("intro", T0, "canceled"), rnd("tech", T0, "canceled")], url="https://jobs.acme.com/1")
    assert interview_outcome(i) is None
    rows = build_rows([bid()], [], {})
    assert match_interviews(rows, [i], {}) == (0, 0)
    assert "interviewId" not in next(iter(rows.values()))


def test_ai_interview_and_home_assessment_ignored():
    assert interview_outcome(iv([rnd("ai_interview", T0), rnd("home_assessment", T0)])) is None


def test_furthest_and_result():
    i = iv([rnd("intro", T0, "passed"), rnd("final", T0 + timedelta(days=5), "rejected"), rnd("tech", T0, "passed")])
    out = interview_outcome(i)
    assert out == {"firstRoundAt": T0, "furthest": "final", "result": "rejected"}


def test_shared_row_best_status_earliest_bidder():
    early, late = bid(status="rejected", at=T0), bid(status="approved", at=T0 + timedelta(days=1))
    row = next(iter(build_rows([late, bid(kind="self", status="approved", at=T0 - timedelta(days=1)), early],
                               [], {}).values()))
    assert row["bidStatus"] == "approved" and row["bidderId"] == early["bidderId"] and row["by"] == "bidder"


def test_legacy_bare_number_match_score():
    row = next(iter(build_rows([], [resume(matchScore=72)], {}).values()))
    assert row["score"] == 72 and row["scoreBand"]
    assert next(iter(build_rows([], [resume(matchScore=None)], {}).values()))["score"] is None


def test_account_none_not_merged_across_owners():
    a, b = resume(acc=None), resume(acc=None)
    assert len(build_rows([], [a, b], {})) == 2
    rows = build_rows([bid(acc=None)], [], {})
    i = iv([rnd("intro", T0)], url="https://jobs.acme.com/1", acc=None)
    assert match_interviews(rows, [i], {}) == (0, 1)


def test_several_resumes_earliest_wins_and_owner_is_earliest():
    late = resume(at=T0 + timedelta(days=2), promptName="late", matchScore={"total": 10, "band": "weak"})
    early = resume(at=T0, promptName="early")
    b = bid(at=T0 + timedelta(days=1))
    row = next(iter(build_rows([b], [late, early], {}).values()))
    assert row["promptName"] == "early" and row["score"] == 80 and row["appliedAt"] == T0
    assert row["ownerId"] == OWN
    o2 = ObjectId()
    early["userId"] = o2
    assert next(iter(build_rows([b], [early], {}).values()))["ownerId"] == o2


def test_row_area_prefers_resume_title():
    b = bid()
    b["jobKey"] = "f/2026-09-01/p/acme_backend-engineer"
    r = resume(jobDescription="Frontend Developer\nCompany: X")
    row = next(iter(build_rows([b], [r], {}).values()))
    assert (row["title"], row["area"]) == ("Frontend Developer", "Frontend")
    row = next(iter(build_rows([b], [], {}).values()))
    assert (row["title"], row["area"]) == ("backend engineer", "Backend")
    row = next(iter(build_rows([bid()], [], {}).values()))
    assert (row["title"], row["area"]) == (None, None)


def test_profile_age_days():
    accounts = {ACC: {"region": "EU", "createdAt": T0 - timedelta(days=10, hours=5)}}
    assert next(iter(build_rows([bid()], [], accounts).values()))["profileAgeDays"] == 10
    assert next(iter(build_rows([bid()], [], {ACC: {"region": "EU"}}).values()))["profileAgeDays"] is None
    assert next(iter(build_rows([bid()], [], {}).values()))["profileAgeDays"] is None


def test_profile_age_clamped_and_bid_key_fallback():
    accounts = {ACC: {"region": "EU", "createdAt": T0 + timedelta(days=3)}}
    assert next(iter(build_rows([bid()], [], accounts).values()))["profileAgeDays"] == 0
    b1, b2 = bid(), bid(at=T0 + timedelta(days=1))
    b1["jobKey"], b2["jobKey"] = "f/d/p/halcyon_6252121004", "f/d/p/acme_backend-engineer"
    assert next(iter(build_rows([b1, b2], [], {}).values()))["title"] == "backend engineer"
