"""Resume content: tailoredRole vs originalRole normalization."""

from app.resume_service import _normalize_experience_entry, _normalize_skills
from app.template_fill import fill_template


def test_normalize_prefers_tailored_role_for_display():
    exp = {
        "companyName": "Albiware Inc",
        "originalRole": "Senior Fullstack Developer",
        "tailoredRole": "Senior Software Quality Engineer",
        "period": "Oct 2023 – Oct 2025",
        "bullets": [],
        "projects": [],
        "techStack": "Python",
    }
    out = _normalize_experience_entry(exp)
    assert out["originalRole"] == "Senior Fullstack Developer"
    assert out["tailoredRole"] == "Senior Software Quality Engineer"
    assert out["role"] == "Senior Software Quality Engineer"


def test_normalize_legacy_single_role_field():
    exp = {
        "companyName": "Acme",
        "role": "Backend Engineer",
        "period": "2020 – 2021",
    }
    out = _normalize_experience_entry(exp)
    assert out["originalRole"] == "Backend Engineer"
    assert out["tailoredRole"] == "Backend Engineer"
    assert out["role"] == "Backend Engineer"


def test_normalize_skills_joins_array_items():
    out = _normalize_skills([{"category": "Languages", "items": ["Python", "SQL"]}])
    assert out[0]["items"] == "Python, SQL"


def test_fill_template_uses_tailored_role():
    html = (
        '<html><body>'
        '<div data-repeat="company">'
        '<span data-field="companyName">Co</span>'
        '<span data-field="role">Role</span>'
        '<span data-field="period">Dates</span>'
        '</div>'
        '</body></html>'
    )
    content = {
        "name": "Test",
        "title": "Senior Software Quality Engineer",
        "summary": "",
        "contactItems": [],
        "experience": [
            _normalize_experience_entry(
                {
                    "companyName": "Albiware Inc",
                    "originalRole": "Senior Fullstack Developer",
                    "tailoredRole": "Senior Software Quality Engineer",
                    "period": "Oct 2023 – Oct 2025",
                    "bullets": [],
                    "projects": [],
                }
            )
        ],
        "skills": [],
        "education": [],
    }
    filled = fill_template(html, content)
    assert "Senior Software Quality Engineer" in filled
    assert "Senior Fullstack Developer" not in filled
