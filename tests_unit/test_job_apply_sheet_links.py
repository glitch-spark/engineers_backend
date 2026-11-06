"""Finding an exported row again before filling its resume link in (no network)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace  # noqa: E402

from app.job_apply.schema import TailoredMatch  # noqa: E402
from app.job_apply.sheet_links import export_places, exported_to, find_row, score_cells  # noqa: E402

HEADER = ["Profile", "Company Name", "Job Title", "Job URL", "Download Resume", "Match Score", "Band"]
URL = "https://boards.greenhouse.io/acme/jobs/9"


def sheet(*rows):
    return [HEADER, *rows]


def r(profile, url):
    return [profile, "Acme", "Engineer", url, '=HYPERLINK("https://api.example.com/job-applies/files/x","Download")', 72, "good"]


def test_find_row_same_row():
    values = sheet(r("Eric", "https://a.com/1"), r("Tharun", URL))
    assert find_row(values, recorded=3, url=URL, profile="Tharun") == 3


def test_find_row_moved():
    # someone sorted the tab: the job is now on row 2
    values = sheet(r("Tharun", URL), r("Eric", "https://a.com/1"))
    assert find_row(values, recorded=3, url=URL, profile="Tharun") == 2


def test_find_row_gone():
    values = sheet(r("Eric", "https://a.com/1"))
    assert find_row(values, recorded=3, url=URL, profile="Tharun") is None
    assert find_row(sheet(r("Eric", URL)), recorded=2, url=URL, profile="Tharun") is None  # same job, other profile

def row(exported=(), sheets=None, legacy=None):
    return SimpleNamespace(exported={a: 1 for a in exported}, exportedSheets=sheets or {}, exportedRows=legacy or {})


def test_exported_to_is_per_sheet():
    r = row(["eric"], sheets={"eric": {"s1": {"tab": "Apply · 2026-10-06", "row": 4}}})
    assert exported_to(r, "eric", "s1") is True
    assert exported_to(r, "eric", "s2") is False  # the same job can go to another sheet
    assert exported_to(r, "eric", None) is True  # in some sheet
    assert exported_to(r, "tharun", "s1") is False


def test_old_exports_keep_their_one_sheet():
    r = row(["eric"], legacy={"eric": {"sheetId": "s1", "tab": "2026-10-03", "row": 2}})
    assert exported_to(r, "eric", "s1") is True and exported_to(r, "eric", "s2") is False
    assert export_places(r, "eric") == {"s1": {"tab": "2026-10-03", "row": 2}}
    # an old export that recorded no sheet stays skipped everywhere, as before
    assert exported_to(row(["eric"]), "eric", "s2") is True


def test_export_places_merges_old_and_new():
    r = row(
        ["eric"],
        sheets={"eric": {"s2": {"tab": "Apply · 2026-10-06", "row": None}}},
        legacy={"eric": {"sheetId": "s1", "tab": "2026-10-03", "row": 2}},
    )
    assert export_places(r, "eric") == {"s2": {"tab": "Apply · 2026-10-06", "row": None}, "s1": {"tab": "2026-10-03", "row": 2}}


def test_score_cells():
    # a stored matchScore, a TailoredMatch, an uploaded resume's bare total, an early record's bare number
    assert score_cells({"total": 82, "band": "strong", "knockouts": [], "missing": []}) == [82, "strong"]
    assert score_cells(TailoredMatch(total=66, band="good")) == [66, "good"]
    assert score_cells(49) == [49, "weak"]
    assert score_cells(64.6) == [65, "good"]
    # no score (never scored, or tried and couldn't be): blank cells, which sort to the bottom
    assert score_cells(None) == ["", ""] and score_cells({}) == ["", ""]


def test_score_is_a_number_so_the_column_sorts():
    total, band = score_cells({"total": 100, "band": "strong"})
    assert isinstance(total, int) and total == 100 and band == "strong"
