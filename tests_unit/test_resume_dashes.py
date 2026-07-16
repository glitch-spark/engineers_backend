from app.resume_service import _normalize_dashes


def test_normalize_dashes_replaces_em_dash() -> None:
    assert _normalize_dashes("Foo — Bar") == "Foo - Bar"
    assert _normalize_dashes("no change") == "no change"
