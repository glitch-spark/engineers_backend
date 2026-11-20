import pytest

from app.applications.area import classify_area, job_title

P = "kevin-bidder-maria-2/2026-11-10/kevin-smyth/"


def test_job_title_from_description_first_line():
    assert job_title("\n Senior Backend Developer \nCompany: Acme", None) == "Senior Backend Developer"
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


@pytest.mark.parametrize("title,area", [
    ("JavaScript Developer", "Frontend"), ("Node.js Developer", "Backend"),
    ("Full Stack JavaScript Engineer", "Full stack"), ("Java Developer", "Backend"),
    ("Engineer II, Data Platform", "Data"), ("Data Analyst", "Data"), ("Business Intelligence Developer", "Data"),
    ("UI/UX Designer", "Other"), ("Senior Software Engineer, Contract Testing", "General software"),
    ("Test Automation Engineer", "QA"), ("QA Engineer", "QA"), ("SDET", "QA"),
    ("Senior Software Engineer", "General software"), ("Python Developer", "Backend"), ("Cloud Engineer", "DevOps"),
    ("Staff Software Engineer @ Bjak", "General software"), ("Software Developer", "General software"),
    ("Senior Software Development Engineer (Game)", "General software"), ("Senior Developer | Careers | One North", "General software"),
    ("Senior Dev Ops Engineer", "DevOps"), ("Senior Kubernetes & Reliability Engineer", "DevOps"),
    ("Senior Salesforce Engineer", "Other"), ("Database Administrator", "Other"), ("Sign In", "Other"),
    ("DevSecOps Engineer", "DevOps"),
    ("AI Engineer", "AI / ML"), ("React Native Developer", "Mobile"), ("Platform Engineer", "DevOps"),
    ("Maintenance Technician", "Other"), ("Javascript Instructor", "Frontend"), ("TypeScript Engineer", "Frontend"),
])
def test_classify_area_regression(title, area):
    assert classify_area(title) == area


BACKEND_JD = "We build services in Python and Django with Postgres. You will design REST APIs and microservices in Go."


@pytest.mark.parametrize("text,area", [
    (BACKEND_JD, "Backend"),
    ("Our UI is React and TypeScript with Redux, HTML and CSS; you own the frontend.", "Frontend"),
    ("You run Kubernetes, Terraform, Docker and Helm, and own CI/CD and observability. Some Python.", "DevOps"),
    ("Python, Django, Postgres and REST on the server; React, TypeScript, CSS and HTML in the browser.", "Full stack"),
    ("Join us as a full-stack engineer.", "Full stack"),
    ("PyTorch, TensorFlow, LLMs and RAG pipelines; some Python.", "AI / ML"),
    # no area clearly leads, or too little to go on: None (the title's area stands)
    ("Python, Django and SQL; Spark, Airflow, Snowflake and Kafka.", None),
    ("You will write Python.", None), ("", None), (None, None),
    ("We value curiosity, ownership and kindness.", None),
])
def test_area_from_description(text, area):
    from app.applications.area import area_from_description
    assert area_from_description(text) == area
