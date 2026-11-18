import pytest

from app.applications.area import classify_area, job_title

P = "kevin-bidder-maria-2/2026-11-10/kevin-smyth/"


def test_job_title_from_description_first_line():
    assert job_title("\n Senior Backend Developer | Company: Acme\nmore", None) == "Senior Backend Developer"
    assert job_title("x" * 200, None) == "x" * 120


def test_job_title_from_readable_job_key():
    assert job_title(None, P + "netweb_backend-ai-engineer") == "backend ai engineer"
    assert job_title("  ", P + "netweb_backend-ai-engineer") == "backend ai engineer"


@pytest.mark.parametrize("tail", ["jobs_eaa48261-4ebf-4c1a-9d2e-0123456789ab", "halcyon_6252121004",
                                  "caresource_r-14343", "nounderscore"])
def test_job_title_none_for_id_keys(tail):
    assert job_title(None, P + tail) is None


def test_job_title_none_without_inputs():
    assert job_title(None, None) is None


@pytest.mark.parametrize("title,area", [
    ("Senior Backend Developer", "Backend"), ("Full Stack Engineer", "Full stack"), ("ML Engineer", "AI / ML"),
    ("Site Reliability Engineer", "DevOps"), ("QA Automation", "QA"), ("React Developer", "Frontend"),
    ("iOS Engineer", "Mobile"), ("Product Manager", "Other"), (None, None),
])
def test_classify_area_examples(title, area):
    assert classify_area(title) == area
