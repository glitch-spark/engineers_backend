"""Unit tests for sheet parsing and column detection (in-memory xlsx/csv)."""
import io

import pytest
from openpyxl import Workbook

from app.job_apply.sheet import MAX_ROWS, SheetError, normalize_url, parse_sheet

JD = "We are hiring a backend engineer. " * 10  # > 200 chars


def xlsx(rows, hyperlinks=None):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    for (row, col), target in (hyperlinks or {}).items():
        ws.cell(row=row, column=col).hyperlink = target
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_detects_columns_with_header():
    data = xlsx([["Company", "Link", "Description"], ["Acme", "https://jobs.acme.com/1", JD], ["Beta", "https://beta.io/j/2", None]])
    rows = parse_sheet("jobs.xlsx", data)
    assert [(r.rowIndex, r.url) for r in rows] == [(2, "https://jobs.acme.com/1"), (3, "https://beta.io/j/2")]
    assert rows[0].jdText.startswith("We are hiring") and rows[1].jdText is None


def test_no_header_row():
    rows = parse_sheet("jobs.xlsx", xlsx([["https://a.com/1", JD], ["https://a.com/2", JD]]))
    assert [r.url for r in rows] == ["https://a.com/1", "https://a.com/2"] and rows[0].rowIndex == 1


def test_hyperlink_cells():
    data = xlsx([["Job", "JD"], ["Apply", JD], ["Apply", JD]], {(2, 1): "https://x.com/a", (3, 1): "https://x.com/b"})
    assert [r.url for r in parse_sheet("j.xlsx", data)] == ["https://x.com/a", "https://x.com/b"]


def test_dedupe_urls():
    data = xlsx([["https://Jobs.x.com/1?utm_source=li&id=7", JD], ["https://jobs.x.com/1/?id=7", JD], ["https://jobs.x.com/2#apply", JD]])
    rows = parse_sheet("j.xlsx", data)
    assert [r.url for r in rows] == ["https://jobs.x.com/1?id=7", "https://jobs.x.com/2"]


def test_normalize_url():
    assert normalize_url("HTTPS://Boards.Greenhouse.io/acme/jobs/1?gh_src=abc&gh_jid=1/") == "https://boards.greenhouse.io/acme/jobs/1"
    assert normalize_url(" https://a.com/x/?ref=foo&lever-source=bar ") == "https://a.com/x"


def test_csv():
    data = ("url,description\nhttps://a.com/1," + '"' + JD + '"' + "\nhttps://a.com/2,\n").encode("utf-8-sig")
    rows = parse_sheet("jobs.csv", data)
    assert [r.url for r in rows] == ["https://a.com/1", "https://a.com/2"] and rows[1].jdText is None


def test_url_only_sheet():
    rows = parse_sheet("j.xlsx", xlsx([["URL"], ["https://a.com/1"], ["https://a.com/2"]]))
    assert len(rows) == 2 and all(r.jdText is None for r in rows)


def test_rows_without_url_are_skipped():
    rows = parse_sheet("j.xlsx", xlsx([["URL", "JD"], ["https://a.com/1", JD], [None, JD], ["n/a", JD]]))
    assert [r.url for r in rows] == ["https://a.com/1"]


def test_too_many_rows_raises():
    with pytest.raises(SheetError, match="rows"):
        parse_sheet("j.xlsx", xlsx([[f"https://a.com/{i}"] for i in range(MAX_ROWS + 1)]))


def test_no_url_column_raises():
    with pytest.raises(SheetError, match="URL"):
        parse_sheet("j.xlsx", xlsx([["a", "b"], ["c", "d"]]))


def test_bad_file_type_raises():
    with pytest.raises(SheetError):
        parse_sheet("j.pdf", b"%PDF")
    with pytest.raises(SheetError):
        parse_sheet("j.xlsx", b"not a zip")


def test_hyperlink_formula_cells():
    data = xlsx([["Job", "JD"], ['=HYPERLINK("https://x.com/a","Apply")', JD], ["=HYPERLINK(\"https://x.com/b\";\"Apply\")", JD]])
    assert [r.url for r in parse_sheet("j.xlsx", data)] == ["https://x.com/a", "https://x.com/b"]


def test_row_index_matches_sheet_rows_with_blank_rows():
    rows = parse_sheet("j.xlsx", xlsx([["URL"], ["https://a.com/1"], [None], ["https://a.com/2"]]))
    assert [r.rowIndex for r in rows] == [2, 4]


def test_hash_routed_urls_stay_distinct():
    rows = parse_sheet("j.xlsx", xlsx([["https://c.com/careers#/job/1"], ["https://c.com/careers#/job/2"], ["https://c.com/careers#/job/2"]]))
    assert [r.url for r in rows] == ["https://c.com/careers#/job/1", "https://c.com/careers#/job/2"]


# ---------- messy cells (ported from job_url_filter's extract.py behaviour) ----------


def test_url_inside_prose_is_found_and_trimmed():
    rows = parse_sheet("j.xlsx", xlsx([["Job"], ["Senior dev - apply at https://jobs.lever.co/acme/1a2b."],
                                       ["(https://jobs.lever.co/acme/3c4d)"]]))
    assert [r.url for r in rows] == ["https://jobs.lever.co/acme/1a2b", "https://jobs.lever.co/acme/3c4d"]


def test_www_url_without_scheme():
    rows = parse_sheet("j.xlsx", xlsx([["Job"], ["www.acme.com/jobs/1"]]))
    assert [r.url for r in rows] == ["https://www.acme.com/jobs/1"]


def test_several_urls_in_one_cell_become_several_jobs():
    rows = parse_sheet("j.xlsx", xlsx([["Jobs", "JD"],
                                       ["https://a.com/jobs/1 https://a.com/jobs/2", JD],
                                       ["https://a.com/jobs/3", JD]]))
    assert [(r.rowIndex, r.url) for r in rows] == [(2, "https://a.com/jobs/1"), (2, "https://a.com/jobs/2"),
                                                    (3, "https://a.com/jobs/3")]
    # a description can't be split between two links, so those two are fetched
    assert [r.jdText is not None for r in rows] == [False, False, True]


def test_urls_scattered_across_columns_are_all_collected():
    # no column is mostly links: every cell is scanned, row by row, left to right
    rows = parse_sheet("j.xlsx", xlsx([["Company", "Notes", "Other"],
                                       ["Acme", "see https://a.com/jobs/1", ""],
                                       ["Beta", "", "https://b.com/jobs/2 and https://b.com/jobs/3"],
                                       ["Gamma", "phone screen", ""],
                                       ["Delta", "n/a", ""]]))
    assert [r.url for r in rows] == ["https://a.com/jobs/1", "https://b.com/jobs/2", "https://b.com/jobs/3"]


def test_sheet_counts_links_cleaned_and_duplicates():
    rows = parse_sheet("j.xlsx", xlsx([["URL"], ["https://jobs.lever.co/acme/1a2b/apply?utm_source=x"],
                                       ["https://jobs.lever.co/acme/1a2b"], ["https://a.com/jobs/2"]]))
    assert len(rows) == 2 and rows.links == 3 and rows.cleaned == 1 and rows.duplicates == 1
