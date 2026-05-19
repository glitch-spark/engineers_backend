"""Plain-text resume export in logical reading order."""

from app.resume_plain_text import content_to_plain_text, strip_decorative_prefix


def test_strip_decorative_prefix():
    assert strip_decorative_prefix("▸ Built tests") == "Built tests"
    assert strip_decorative_prefix("S O F T") == "S O F T"  # internal spaces kept


def test_content_to_plain_text_order():
    content = {
        "name": "Pedro Kenji Sato",
        "title": "Software Quality Analyst",
        "contactItems": [{"display": "a@b.com", "url": ""}],
        "summary": "Eight years in QA.",
        "skills": [{"category": "Languages", "items": ["Python", "SQL"]}],
        "experience": [
            {
                "companyName": "DayDream",
                "tailoredRole": "Senior Software Quality Engineer",
                "period": "Jul 2023 – Present",
                "bullets": ["Owned QA for checkout."],
                "projects": [],
            }
        ],
        "education": [{"school": "Keio University", "degree": "MS CS", "eduPeriod": "2012 - 2018"}],
    }
    text = content_to_plain_text(content)
    assert text.index("Pedro") < text.index("Summary") < text.index("Technical Skills")
    assert text.index("Experience") < text.index("DayDream") < text.index("Education")
    assert "Senior Software Quality Engineer" in text
    assert "• Owned QA for checkout." in text
    assert "Keio University" in text
