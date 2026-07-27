from app.llm_router import parse_screening_answers
from app.resume_service import _normalize_dashes, _normalize_dashes_body_only


def test_normalize_dashes_replaces_em_dash() -> None:
    assert _normalize_dashes("Foo — Bar") == "Foo - Bar"
    assert _normalize_dashes("no change") == "no change"


def test_normalize_dashes_replaces_en_figure_and_horizontal() -> None:
    # en –  figure ‒  horizontal ―
    assert _normalize_dashes("A – B") == "A - B"
    assert _normalize_dashes("A \u2012 B") == "A - B"
    assert _normalize_dashes("A \u2015 B") == "A - B"
    assert _normalize_dashes("a—b–c‒d―e") == "a-b-c-d-e"


def test_normalize_dashes_body_only_preserves_style_em_dash() -> None:
    html = "<html><head><style>/* A — B */</style></head><body>C — D</body></html>"
    out = _normalize_dashes_body_only(html)
    assert "A — B" in out
    assert "C - D" in out


def test_normalize_dashes_body_only_preserves_style_en_dash() -> None:
    html = (
        "<html><head><style>/* A – B */</style></head>"
        "<body>C – D — E</body></html>"
    )
    out = _normalize_dashes_body_only(html)
    assert "A – B" in out
    assert "C - D - E" in out


def test_parse_screening_answers_normalizes_long_dashes() -> None:
    raw = '{"answers": ["Built APIs — fast", "Led team – 5 eng"]}'
    out = parse_screening_answers(raw, ["q1", "q2"])
    assert out == ["Built APIs - fast", "Led team - 5 eng"]
