"""Unit tests for resume parsing: sections, roles, date ranges, degree, health (no database)."""
from datetime import date

from app.job_apply.resume_parse import Role, experience_years, health, parse_date_ranges, parse_resume

RUN = date(2026, 10, 1)
FLAT = (
    "--- Page 1 --- JANE DOE Senior Software Engineer jane@x.com +1 555 123 4567 SUMMARY Backend engineer. "
    "EXPERIENCE Acme Corp Senior Software Engineer Jan 2021 – Present Built Kafka pipelines in Python. "
    "Beta Inc Software Engineer 2016 - 2020 Wrote Java services on AWS. "
    "SKILLS Python, Kafka, Terraform EDUCATION B.Sc. Computer Science, 2015"
)

MARKDOWN = """# Jane Doe
Staff Data Engineer · jane@x.com · (555) 123-4567

## Experience
**Lead Data Engineer**, Acme — 03/2019 - Present
- Built Airflow DAGs and dbt models

**Data Engineer**, Beta — 2015–2019
- Spark jobs on Hadoop

## Skills
Airflow, dbt, Spark

## Education
Master of Science in Computer Science
"""


def test_parse_flat_pdf_text():
    r = parse_resume(FLAT, RUN)
    assert len(r.roles) == 2 and r.roles[0].end is None
    assert "Kafka" in r.roles[0].text and "Java" in r.roles[1].text
    assert "Java" not in r.roles[0].text
    assert "Terraform" in r.skills and r.degree_level == 2
    assert "Senior Software Engineer" in r.headline
    assert "Page 1" not in r.text


def test_parse_markdown_resume():
    r = parse_resume(MARKDOWN, RUN)
    assert [x.start for x in r.roles] == [date(2019, 3, 1), date(2015, 1, 1)]
    assert "Lead Data Engineer" in r.roles[0].window
    assert "Airflow DAGs" in r.roles[0].text and "Spark jobs" not in r.roles[0].text
    assert "dbt" in r.skills and r.degree_level == 3
    assert "Staff Data Engineer" in r.headline


def test_date_range_forms():
    for s, years in [
        ("Jan 2020 – Present", 6.7),
        ("2019–2021", 2.0),
        ("03/2019 - 05/2022", 3.2),
        ("Sept. 2018 to Current", 8.1),
        ("June 2017 — Dec 2018", 1.5),
    ]:
        rngs = parse_date_ranges(s, RUN)
        assert len(rngs) == 1, s
        assert abs(experience_years([Role("", rngs[0][0], rngs[0][1], "")], RUN) - years) < 0.15, s


def test_overlapping_roles_counted_once():
    text = "EXPERIENCE A Jan 2018 - Dec 2020 x B Jan 2020 - Dec 2021 y"
    assert abs(experience_years(parse_resume(text, RUN).roles, RUN) - 4.0) < 0.1


def test_no_roles_gives_none():
    assert experience_years([], RUN) is None


def test_stated_years_and_degrees():
    assert parse_resume("8+ years of experience building APIs", RUN).stated_years == 8
    assert parse_resume("Over 6 years of professional experience", RUN).stated_years == 6
    assert parse_resume("EDUCATION Master of Science, MIT", RUN).degree_level == 3
    assert parse_resume("EDUCATION PhD Physics", RUN).degree_level == 4
    assert parse_resume("Used MS SQL Server daily", RUN).degree_level is None


def test_health_badge():
    assert health(FLAT + " " + FLAT[15:]).issues == []
    h = health("Just a few words")
    assert set(h.issues) == {"too_little_text", "no_email", "no_phone", "missing_headings"}
    assert h.score == 100 - 20 - 20 - 8 - 8
