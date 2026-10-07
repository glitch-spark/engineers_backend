import gzip
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId, json_util

from app import resume_retention as rr

NOW = datetime(2026, 10, 7, 9, 2, tzinfo=timezone.utc)


def _match(doc: dict, flt: dict) -> bool:
    """Just enough of Mongo's matcher for _stale_filter and the update's $in."""
    for key, cond in flt.items():
        if key == "$or":
            if not any(_match(doc, sub) for sub in cond):
                return False
        elif key.endswith(".0"):
            if bool(doc.get(key[:-2])) != cond["$exists"]:
                return False
        elif isinstance(cond, dict):
            val = doc.get(key)
            if "$lt" in cond and not (val is not None and val < cond["$lt"]):
                return False
            if "$nin" in cond and val in cond["$nin"]:
                return False
            if "$in" in cond and val not in cond["$in"]:
                return False
        elif doc.get(key) != cond:
            return False
    return True


class _Cursor:
    def __init__(self, docs, keep):
        self.docs, self.keep = docs, keep

    def sort(self, key, direction):
        self.docs.sort(key=lambda d: d[key], reverse=direction < 0)
        return self

    async def to_list(self, n):
        return [{k: v for k, v in d.items() if k in self.keep} for d in self.docs[:n]]


class _Res:
    def __init__(self, n):
        self.modified_count = n


class FakeColl:
    def __init__(self, docs):
        self.docs = docs

    def find(self, flt, projection):
        return _Cursor([d for d in self.docs if _match(d, flt)], set(projection) | {"_id"})

    async def update_many(self, flt, update):
        n = 0
        for d in self.docs:
            if _match(d, flt):
                d.update(update["$set"])
                for k in update["$unset"]:
                    d.pop(k, None)
                n += 1
        return _Res(n)


def _job(days_old: int, status: str = "completed", **extra) -> dict:
    doc = {
        "_id": ObjectId(), "status": status, "createdAt": NOW - timedelta(days=days_old),
        "companyName": "Acme", "s3Key": "resumes/x.pdf",
        "jobDescription": "JD text", "plainText": "resume text", "coverLetterText": "letter",
        "screeningPairs": [{"q": "why", "a": "because"}], "resumeLlmFallbackReason": None,
    }
    doc.update(extra)
    return doc


@pytest.fixture
def env(monkeypatch):
    archived = {}

    async def fake_archive(key, docs):
        archived[key] = gzip.decompress(gzip.compress("\n".join(json_util.dumps(d) for d in docs).encode()))

    monkeypatch.setattr(rr, "_archive", fake_archive)
    monkeypatch.setattr(rr, "s3_enabled", lambda: True)
    return archived


def _use(monkeypatch, docs):
    coll = FakeColl(docs)
    monkeypatch.setattr(rr.ResumeApplication, "get_motor_collection", classmethod(lambda cls: coll))
    return coll


async def test_strips_only_old_completed_jobs_and_archives_first(monkeypatch, env):
    old, recent, failed = _job(40), _job(5), _job(40, status="failed")
    _use(monkeypatch, [old, recent, failed])

    out = await rr.strip_old_resume_text(days=30, now=NOW)

    assert out == {"stripped": 1, "batches": 1, "cutoffDays": 30}
    assert old["jobDescription"] == "" and "plainText" not in old and "coverLetterText" not in old
    assert "screeningPairs" not in old and old["textArchivedAt"] == NOW
    assert old["companyName"] == "Acme" and old["s3Key"] == "resumes/x.pdf"  # metadata + PDF kept
    assert recent["plainText"] == "resume text"  # inside the window
    assert failed["plainText"] == "resume text"  # retry needs its inputs
    (key, body), = env.items()
    assert old["textArchiveKey"] == key and key.startswith("archive/resume-text/2026/10/07/")
    restored = json_util.loads(body.decode())
    assert restored["_id"] == old["_id"] and restored["plainText"] == "resume text"


async def test_second_run_finds_nothing(monkeypatch, env):
    _use(monkeypatch, [_job(40)])
    await rr.strip_old_resume_text(days=30, now=NOW)
    assert await rr.strip_old_resume_text(days=30, now=NOW) == {"stripped": 0, "batches": 0, "cutoffDays": 30}


async def test_batches_until_done(monkeypatch, env):
    monkeypatch.setattr(rr, "BATCH_SIZE", 2)
    _use(monkeypatch, [_job(40 + i) for i in range(5)])
    out = await rr.strip_old_resume_text(days=30, now=NOW)
    assert out["stripped"] == 5 and out["batches"] == 3


async def test_never_strips_without_s3(monkeypatch):
    monkeypatch.setattr(rr, "s3_enabled", lambda: False)
    doc = _job(40)
    _use(monkeypatch, [doc])
    assert await rr.strip_old_resume_text(days=30, now=NOW) == {"skipped": "no-s3"}
    assert doc["plainText"] == "resume text"


async def test_zero_days_is_off(monkeypatch, env):
    assert await rr.strip_old_resume_text(days=0, now=NOW) == {"skipped": "off"}


def test_daily_window():
    assert rr.daily_due(NOW)
    assert not rr.daily_due(NOW.replace(minute=5))
    assert not rr.daily_due(NOW.replace(hour=10))


async def test_tick_outside_window_does_nothing(monkeypatch, env):
    doc = _job(40)
    _use(monkeypatch, [doc])
    assert await rr.run_retention_tick(NOW.replace(hour=3)) == {"skipped": "not-due"}
    assert doc["plainText"] == "resume text"
