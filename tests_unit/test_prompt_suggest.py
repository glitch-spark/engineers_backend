"""Unit tests for the job roles and resume-unit suggestion behind Job Applies' Prompt column (pure, no DB)."""
import os
import time
from datetime import datetime

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.job_apply.prompt_suggest import MAX_ROLES, RoleMatch, job_roles, suggest_unit  # noqa: E402
from app.job_apply.schema import Term  # noqa: E402
from app.models.account import ResumePrompt  # noqa: E402


def t(term, tier="mentioned", soft=False):
    return Term(term=term, tier=tier, soft=soft)


def test_title_decides_the_role():
    assert job_roles("Senior DevOps Engineer", []) == RoleMatch(["devops"], "DevOps — title says DevOps")


def test_skills_decide_when_the_title_is_generic():
    m = job_roles("Software Engineer", [t("Kubernetes", "required"), t("Terraform", "core"), t("Python")])
    assert m == RoleMatch(["devops"], "DevOps — skills Kubernetes, Terraform")


def test_full_stack_and_devops_job_has_both_roles():
    m = job_roles("Full Stack Engineer", [t("Kubernetes", "required"), t("Terraform", "required")])
    assert set(m.roles) == {"fullstack", "devops"}
    assert "Full stack — title says Full Stack" in m.reason and "DevOps — skills Kubernetes, Terraform" in m.reason


def test_weak_second_role_is_left_out():
    m = job_roles("DevOps Engineer", [t("Kubernetes", "required"), t("Terraform", "required"), t("React", "required")])
    assert m.roles == ["devops"]  # frontend (2) is under half of devops (7)


def test_at_most_three_roles_in_order():
    m = job_roles("Full Stack / DevOps / iOS / QA Engineer", [])
    assert MAX_ROLES == 3 and m.roles == ["fullstack", "devops", "mobile"]  # all tied: by position in the title


def test_no_role_without_enough_signal():
    assert job_roles("Engineer", [t("Jira"), t("Salesforce", "required", soft=True)]) == RoleMatch([], "")


def test_whole_word_matching():
    for title, terms in [("Good communicator", []), ("Build tools developer", [t("Build tools", "required")]),
                         ("Aqua Security analyst", []), ("Reactive systems engineer", [])]:
        assert job_roles(title, terms).roles == [], title
    assert job_roles("Engineer", [t("CI/CD pipelines", "required")]).roles == ["devops"]
    assert job_roles("Next.js developer", []).roles == ["frontend"]
    assert job_roles("Engineer", [t("Node.js", "required")]).roles == ["backend"]


def test_react_native_is_mobile_not_frontend():
    assert job_roles("React Native Developer", []).roles == ["mobile"]
    m = job_roles("Senior React Native Engineer", [t("React Native", "required"), t("TypeScript", "core")])
    assert m.roles == ["mobile"] and m.reason.startswith("Mobile — title says React Native")


def test_two_roles_in_the_title_follow_title_order():
    assert job_roles("Full Stack React Developer", []).roles == ["fullstack", "frontend"]
    assert job_roles("React Full Stack Developer", []).roles == ["frontend", "fullstack"]


def test_job_without_extraction():
    assert job_roles("", []) == RoleMatch([], "")


def test_role_detection_is_fast_enough_for_a_200_row_page():
    terms = [t(f"Skill{i}", "required") for i in range(20)] + [t("Kubernetes", "required"), t("React", "core"),
                                                              t("CI/CD", "core"), t("Python", "required"), t("AWS", "core")]
    for term in terms:
        term.aliases = [term.term + " dev"]
    start = time.perf_counter()
    for _ in range(200):
        job_roles("Senior Platform Engineer, Cloud Infrastructure", terms)
    assert time.perf_counter() - start < 0.5


def _unit(uid, roles, updated):
    return ResumePrompt(id=uid, name=uid, roles=roles, updatedAt=datetime(2026, 10, updated))


def test_suggest_unit():
    units = [_unit("default", [], 1), _unit("fsd", ["fullstack", "devops"], 2), _unit("fs", ["fullstack"], 3),
             _unit("ops", ["devops"], 4), _unit("fe", ["frontend"], 5)]
    assert suggest_unit(units, "default", ["fullstack", "devops"]) == "fsd"      # both marks
    no_pair = [u for u in units if u.id != "fsd"]
    assert suggest_unit(no_pair, "default", ["fullstack", "devops"]) == "fs"     # the job's main role
    assert suggest_unit(no_pair, "default", ["devops", "fullstack"]) == "ops"
    assert suggest_unit(units, "default", ["data"]) == "default"                 # no overlap
    assert suggest_unit(units, "default", []) == "default"                       # no job roles


def test_suggest_unit_ties_go_to_the_default_then_the_latest():
    units = [_unit("a", ["devops"], 1), _unit("b", ["devops"], 2), _unit("c", ["devops"], 9)]
    assert suggest_unit(units, "a", ["devops"]) == "a"
    assert suggest_unit(units, "x", ["devops"]) == "c"


def test_a_role_in_the_title_is_always_shown_and_comes_first():
    skills = [t("Kubernetes", "required"), t("Terraform", "required"), t("Helm", "core"), t("CI/CD", "required")]
    assert job_roles("Senior Full Stack Engineer", skills).roles == ["fullstack", "devops"]
