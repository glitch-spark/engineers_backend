"""_apply_removals: overlapping parent/child remove ids must not crash."""

from bs4 import BeautifulSoup

from app.template_annotate import _apply_markers, _apply_removals


def test_apply_removals_parent_then_child_ids_no_crash():
    soup = BeautifulSoup(
        "<div id=\"parent\" data-anno-id=\"0\">"
        "<span data-anno-id=\"1\">x</span>"
        "</div>",
        "html.parser",
    )
    _apply_markers(soup, [{"id": "1", "attr": "data-field", "value": "role"}])
    removed = _apply_removals(soup, ["0", "1"])
    assert removed >= 0
    assert soup.find(attrs={"data-field": "role"}) is not None


def test_apply_removals_deepest_first_removes_redundant_sample():
    soup = BeautifulSoup(
        "<div>"
        '<div class="job" data-anno-id="10"><span data-anno-id="11">keep</span></div>'
        '<div class="job" data-anno-id="12"><span data-anno-id="13">drop</span></div>'
        "</div>",
        "html.parser",
    )
    _apply_markers(soup, [{"id": "10", "attr": "data-repeat", "value": "company"}])
    removed = _apply_removals(soup, ["12", "13"])
    assert removed >= 1
    assert soup.find(string="drop") is None
    assert soup.find(string="keep") is not None
