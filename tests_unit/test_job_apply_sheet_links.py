"""Finding an exported row again before filling its resume link in (no network)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.job_apply.sheet_links import find_row  # noqa: E402

HEADER = ["Profile", "Company Name", "Job Title", "Job URL", "Download Resume"]
URL = "https://boards.greenhouse.io/acme/jobs/9"


def sheet(*rows):
    return [HEADER, *rows]


def r(profile, url):
    return [profile, "Acme", "Engineer", url, '=HYPERLINK("https://api.example.com/job-applies/files/x","Download")']


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
