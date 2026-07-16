from app.resume_service import _normalize_dashes, _normalize_dashes_body_only


def test_normalize_dashes_replaces_em_dash() -> None:
    assert _normalize_dashes("Foo — Bar") == "Foo - Bar"
    assert _normalize_dashes("no change") == "no change"


def test_normalize_dashes_body_only_preserves_style_em_dash() -> None:
    html = "<html><head><style>/* A — B */</style></head><body>C — D</body></html>"
    out = _normalize_dashes_body_only(html)
    assert "A — B" in out
    assert "C - D" in out
