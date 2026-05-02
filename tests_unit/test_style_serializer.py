"""Golden tests for style_serializer.to_prose.

These pin the output shape so a refactor of the serializer can't silently
shift the prose the LLM is trained against.
"""

from app.style_serializer import to_prose


_LEGACY_DEFAULT = {
    "page": {"format": "A4", "margin": {"top": 35, "right": 30, "bottom": 30, "left": 30}},
    "sectionOrder": ["summary", "experience", "skills", "education"],
    "sectionSpacing": {"containerMb": 8, "headingMb": 4, "dividerMb": 6},
    "basicInfo": {
        "name": {"fontFamily": "Arial Black", "fontSize": 20, "fontWeight": "bold", "color": "#000", "align": "center"},
        "title": {"fontFamily": "Arial Black", "fontSize": 12, "fontWeight": "bold", "color": "#000", "align": "center", "text": "Senior Software Engineer"},
        "contact": {"fontFamily": "Tahoma", "fontSize": 11, "fontWeight": "normal", "color": "#000", "align": "center", "separator": "|"},
    },
    "sectionHeading": {"fontFamily": "Arial Black", "fontSize": 11, "fontWeight": "bold", "color": "#000", "align": "center"},
    "summary": {"fontFamily": "Tahoma", "fontSize": 11, "fontWeight": "normal", "color": "#000", "align": "justify", "boldLabels": True},
    "experience": {
        "companyName": {"fontFamily": "Arial Black", "fontSize": 11, "fontWeight": "bold", "color": "#341b74"},
        "role": {"fontFamily": "Tahoma", "fontSize": 11, "fontWeight": "normal", "color": "#000"},
        "period": {"fontFamily": "Tahoma", "fontSize": 11, "fontWeight": "normal", "color": "#000", "format": "YYYY/MM - YYYY/MM"},
        "separator": "|",
        "bullet": {"fontFamily": "Tahoma", "fontSize": 11, "fontWeight": "normal", "color": "#000", "align": "justify", "indentPx": 10, "boldKeywords": True},
    },
    "skills": {"fontFamily": "Tahoma", "fontSize": 11, "fontWeight": "normal", "color": "#000", "align": "justify", "boldCategories": True, "layout": "one-per-line"},
    "education": {
        "university": {"fontFamily": "Arial Black", "fontSize": 11, "fontWeight": "bold", "color": "#341b74"},
        "degree": {"fontFamily": "Tahoma", "fontSize": 11, "fontWeight": "normal", "color": "#000"},
        "period": {"fontFamily": "Tahoma", "fontSize": 11, "fontWeight": "normal", "color": "#000", "format": "YYYY - YYYY"},
        "separator": "|",
    },
}


def test_legacy_default_includes_required_anchors():
    """The legacy preset must mention every authoritative anchor the LLM relies on."""
    out = to_prose(_LEGACY_DEFAULT)
    for anchor in [
        "PAGE LAYOUT:",
        "SECTION SPACING SYSTEM (AUTHORITATIVE):",
        "HEADER:",
        "SECTION HEADINGS",
        "SUMMARY CONTENT:",
        "PROFESSIONAL EXPERIENCE:",
        "WORK EXPERIENCE BULLETS:",
        "SKILLS CONTENT:",
        "EDUCATION:",
        "SECTION ORDER (STRICT):",
        "FINAL ENFORCEMENT:",
    ]:
        assert anchor in out, f"missing anchor: {anchor}"


def test_section_order_propagates():
    cfg = {**_LEGACY_DEFAULT, "sectionOrder": ["experience", "skills", "summary", "education"]}
    out = to_prose(cfg)
    assert "PROFESSIONAL EXPERIENCE → SKILLS → SUMMARY → EDUCATION" in out


def test_typography_emits_color_only_when_non_black():
    cfg = {**_LEGACY_DEFAULT}
    out = to_prose(cfg)
    # company name color #341b74 must appear; default black for role/period must not.
    assert "color #341b74" in out
    assert "color #000" not in out


def test_skills_layout_comma_variant():
    cfg = {**_LEGACY_DEFAULT, "skills": {**_LEGACY_DEFAULT["skills"], "layout": "comma"}}
    out = to_prose(cfg)
    assert "joined on one line, separated by commas" in out


def test_bold_keywords_disabled_says_so():
    cfg = {**_LEGACY_DEFAULT}
    cfg["experience"] = {**cfg["experience"], "bullet": {**cfg["experience"]["bullet"], "boldKeywords": False}}
    out = to_prose(cfg)
    assert "Do not bold keywords" in out


def test_minimal_config_does_not_crash():
    """A config with only `page` should still produce a usable spec."""
    out = to_prose({"page": {"margin": {"top": 35, "right": 30, "bottom": 30, "left": 30}}})
    assert "PAGE LAYOUT:" in out
    assert "SECTION ORDER (STRICT):" in out  # falls back to default order
