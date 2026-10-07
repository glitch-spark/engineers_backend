from types import SimpleNamespace

import pytest

from app.job_apply import jd_store

URL = "https://jobs.example.com/1"


def _row(**kw):
    base = {"jdText": None, "jdSource": "ats_api", "url": URL}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("source", ["html", "ats_api", "browser"])
def test_fetched_text_is_not_stored_on_the_row(source):
    assert jd_store.persisted("We need X", source) is None


@pytest.mark.parametrize("source", [None, "sheet", "manual"])
def test_text_found_nowhere_else_stays_on_the_row(source):
    assert jd_store.persisted("We need X", source) == "We need X"


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()


class _Coll:
    def __init__(self, docs):
        self.docs, self.queries = docs, []

    def find(self, flt, projection):
        self.queries.append(flt)
        return _Cursor([d for d in self.docs if d["url"] in flt["url"]["$in"]])


@pytest.fixture
def cache(monkeypatch):
    from app.models.job_apply import JobPostingCache

    coll = _Coll([{"url": URL, "text": "Cached description"}])
    monkeypatch.setattr(JobPostingCache, "get_motor_collection", classmethod(lambda cls: coll))
    return coll


async def test_hydrate_fills_fetched_rows_from_the_cache(cache):
    fetched, gone = _row(), _row(url="https://jobs.example.com/expired")
    await jd_store.hydrate([fetched, gone, None])
    assert fetched.jdText == "Cached description"
    assert gone.jdText is None  # cache entry expired: the row reads as having no description
    assert len(cache.queries) == 1  # one lookup for the whole batch


async def test_hydrate_leaves_stored_and_already_filled_rows_alone(cache):
    sheet = _row(jdSource="sheet", jdText="From the sheet")
    manual = _row(jdSource="manual", jdText="Corrected")
    legacy = _row(jdText="Copy stored before the change")
    await jd_store.hydrate([sheet, manual, legacy])
    assert (sheet.jdText, manual.jdText, legacy.jdText) == ("From the sheet", "Corrected", "Copy stored before the change")
    assert cache.queries == []


async def test_save_fields_never_writes_a_fetched_description(monkeypatch):
    from app.job_apply import worker
    from app.models.job_apply import JobApplyRow

    writes = []

    class Coll:
        async def update_one(self, flt, update):
            writes.append(update["$set"])

    monkeypatch.setattr(JobApplyRow, "get_motor_collection", classmethod(lambda cls: Coll()))
    row = SimpleNamespace(id="r1", jdText="Fetched text", jdSource="html", status="fetched")
    await worker._save_fields(row, ("jdText", "jdSource", "status"))
    assert writes[0]["jdText"] is None and writes[0]["jdSource"] == "html"
    row.jdSource = "manual"
    await worker._save_fields(row, ("jdText", "jdSource"))
    assert writes[1]["jdText"] == "Fetched text"
