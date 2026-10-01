"""Unit tests for job-apply text utilities, skills dictionary and JD sections (no database)."""

from app.job_apply.sections import line_kind, split_sections, substantive_text
from app.job_apply.skills_dict import aliases_for, find_skills
from app.job_apply.text import count_mentions, normalize, term_regex, tokens


def test_normalize_dashes_and_case():
    assert normalize("Jan 2020 – Present  NOW") == "jan 2020 - present now"


def test_tokens_keep_tech_symbols():
    assert tokens("Built C++, C#, Node.js and .NET; CI/CD!") == ["built", "c++", "c#", "node.js", "and", ".net", "ci/cd"]


def test_term_regex_plural_and_symbols():
    assert term_regex("REST API").search("built REST APIs daily")
    assert term_regex("C++").search("Modern C++ and Go")
    assert not term_regex("Go").search("Google")
    assert term_regex("Node.js").search("node.js services")
    assert not term_regex("Java").search("JavaScript")


def test_count_mentions_and_aliases():
    assert count_mentions("Postgres and PostgreSQL; postgres", aliases_for("PostgreSQL") + ("PostgreSQL",)) == 3
    assert "Postgres" in aliases_for("postgresql")
    assert "PostgreSQL" in aliases_for("postgres")


def test_find_skills():
    found = find_skills("We use Python, k8s and Postgres. React is a plus.")
    assert {"Python", "Kubernetes", "PostgreSQL", "React"} <= set(found)


def test_split_sections_kinds():
    jd = "About us\nWe build.\nRequirements:\n- 5+ years Python\n- Kubernetes is a plus\nBenefits\n- PTO"
    sections = split_sections(jd)
    assert [s.kind for s in sections] == ["context", "required", "ignored"]
    req = sections[1]
    assert [line_kind(l, req.kind) for l in req.lines] == ["required", "preferred"]
    assert "PTO" not in substantive_text(jd) and "We build" not in substantive_text(jd)
    assert "Python" in substantive_text(jd)


def test_unheaded_text_is_mentioned():
    assert [s.kind for s in split_sections("We need Python people.\nResponsibilities\n- Ship")] == ["mentioned", "core"]


def test_markdown_and_bold_headings():
    jd = "## What you'll do\n- Ship\n**Nice to have**\n- Rust"
    assert [s.kind for s in split_sections(jd)] == ["core", "preferred"]


def test_responsibility_line_override():
    assert line_kind("Must have 3 years of React", "core") == "required"
    assert line_kind("Ship features weekly", "core") == "core"
    assert line_kind("Experience with Go is a bonus", "required") == "preferred"


def test_seniority_word():
    from app.job_apply.text import seniority_word

    assert seniority_word("Senior Data Analyst") == "senior"
    assert seniority_word("Sr. Engineer") == "senior"
    assert seniority_word("Staff Software Engineer") == "staff"
    assert seniority_word("Software Engineer II") == "mid"
    assert seniority_word("Software Engineer") is None
    assert seniority_word("Head of Data") == "director"


def test_core_title():
    from app.job_apply.text import core_title

    assert core_title("Senior Data Analyst (m/f/d)") == "data analyst"
    assert core_title("Sr. Software Engineer II") == "software engineer"
    assert core_title("Lead Front-End Developer") == "front-end developer"
