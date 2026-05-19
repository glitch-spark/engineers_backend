"""PDF page inset: zero margin, body padding."""

from app.resume_service import (
    _DEFAULT_MARGIN_PT,
    _ensure_pdf_inset,
    _resolve_margin_pt,
)


def test_playwright_margins_are_zero():
    assert _resolve_margin_pt(None) == _DEFAULT_MARGIN_PT
    assert all(v == 0 for v in _DEFAULT_MARGIN_PT.values())


def test_ensure_pdf_inset_injects_padding_not_page_margin():
    html = "<html><head></head><body><p>x</p></body></html>"
    out = _ensure_pdf_inset(html)
    assert "resume-pdf-inset" in out
    assert "@page { margin: 0" in out
    assert "body { padding:" in out
    assert "margin: 0 !important" in out
