"""Unit tests for the job role / keyword marks behind Job Applies' suggested resume prompt (pure, no DB)."""
import os
from datetime import datetime

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.job_apply.prompt_suggest import MAX_MARKS, RoleMatch, job_role, suggest_prompt  # noqa: E402
from app.job_apply.schema import Term  # noqa: E402
from app.models.account import ResumePrompt  # noqa: E402


def t(term, tier="mentioned", soft=False):
    return Term(term=term, tier=tier, soft=soft)


def test_title_decides_the_role():
    m = job_role("Senior DevOps Engineer", [])
    assert m.role == "devops" and m.reason == "DevOps job — title says DevOps"


def test_skills_decide_when_the_title_is_generic():
    m = job_role("Software Engineer", [t("Kubernetes", "required"), t("Terraform", "core"), t("Python")])
    assert m.role == "devops"
    assert m.marks[:2] == ["Kubernetes", "Terraform"]
    assert m.reason == "DevOps job — skills Kubernetes, Terraform"


def test_marks_are_capped_at_seven_and_skip_title_words():
    terms = [t("DevOps", "required"), t("Kubernetes", "required"), t("Terraform", "required"), t("Helm", "core"),
             t("Jenkins", "core"), t("Ansible", "mentioned"), t("AWS", "required"), t("Go", "core"), t("Python", "required"),
             t("Linux", "core"), t("Communication", "required", soft=True)]
    m = job_role("DevOps Engineer", terms)
    assert len(m.marks) == MAX_MARKS == 7
    assert "DevOps" not in m.marks and "Communication" not in m.marks
    assert m.marks[:4] == ["Kubernetes", "Terraform", "Helm", "Jenkins"]  # role keywords, required/core first
    assert "AWS" in m.marks  # then the job's other required / core skills


def test_no_role_without_enough_signal():
    m = job_role("Engineer", [t("Jira")])
    assert m == RoleMatch(None, [], "")


def test_no_role_still_shows_top_required_skills():
    m = job_role("Engineer", [t("Salesforce", "required"), t("Apex", "core"), t("Jira")])
    assert m.role is None and m.marks == ["Salesforce", "Apex"] and m.reason == ""


def test_whole_word_matching():
    for title, terms in [("Good communicator", []), ("Build tools developer", [t("Build tools", "required")]),
                         ("Aqua Security analyst", []), ("Reactive systems engineer", [])]:
        assert job_role(title, terms).role is None, title
    assert job_role("Engineer", [t("CI/CD pipelines", "required")]).role == "devops"
    assert job_role("Next.js developer", []).role == "frontend"
    assert job_role("Engineer", [t("Node.js", "required")]).role == "backend"


def test_tie_goes_to_the_role_named_first_in_the_title():
    assert job_role("Full Stack React Developer", []).role == "fullstack"
    assert job_role("React Full Stack Developer", []).role == "frontend"


def test_job_without_extraction():
    assert job_role("", []) == RoleMatch(None, [], "")


def _prompt(pid, role, updated):
    return ResumePrompt(id=pid, name=pid, role=role, body="", updatedAt=datetime(2026, 10, updated))


def test_suggest_prompt():
    prompts = [_prompt("default", None, 1), _prompt("ops-old", "devops", 2), _prompt("ops-new", "devops", 9),
               _prompt("fe", "frontend", 3)]
    assert suggest_prompt(prompts, "default", "frontend") == "fe"
    assert suggest_prompt(prompts, "default", "devops") == "ops-new"     # two of that role: the latest
    assert suggest_prompt(prompts, "ops-old", "devops") == "ops-old"     # ... unless the default is one of them
    assert suggest_prompt(prompts, "default", "data") == "default"       # no prompt for the role
    assert suggest_prompt(prompts, "default", None) == "default"         # no role
