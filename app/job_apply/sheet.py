"""Uploaded .xlsx / .csv → job rows. Columns are detected, not configured: the URL column is the one whose
cells (or hyperlink targets) are mostly http(s) links; the JD column is the other column with the longest text; the
company column is the one headed "Company" / "Company Name" / "Employer" (kept as written, over any machine reading)."""
import csv
import io
import re
from dataclasses import dataclass
from typing import Optional

from app.job_apply.urls import find_urls, normalize_url

MAX_ROWS = 1000
MAX_BYTES = 5_000_000
MIN_JD_AVG_CHARS = 200  # a column must average this much text to count as the JD column
MIN_JD_CELL_CHARS = 100  # shorter cells are treated as "no JD" and the URL is fetched instead

_URL = re.compile(r"^https?://\S+$", re.I)
_COMPANY_HEADER = re.compile(r"^(company( name)?|employer|organi[sz]ation)$", re.I)
_HYPERLINK_FORMULA = re.compile(r'^=\s*HYPERLINK\(\s*"([^"]+)"(?:\s*[,;]\s*"([^"]*)")?', re.I)


class SheetError(ValueError):
    pass


@dataclass
class SheetRow:
    rowIndex: int  # 1-based row number in the sheet
    url: Optional[str]
    jdText: Optional[str]
    company: Optional[str] = None  # from the sheet's company column, as written


@dataclass
class _Cell:
    text: str
    link: Optional[str]

    @property
    def urls(self) -> list[str]:
        """The hyperlink target, then every URL written in the text ("apply at https://…", several per cell)."""
        out = [self.link.strip()] if self.link and _URL.match(self.link.strip()) else []
        for u in find_urls(self.text):
            if u not in out:
                out.append(u)
        return out

    @property
    def url(self) -> Optional[str]:
        urls = self.urls
        return urls[0] if urls else None


def _xlsx_cell(value_cell, formula_cell) -> _Cell:
    text = "" if value_cell.value is None else str(value_cell.value).strip()
    link = value_cell.hyperlink.target if value_cell.hyperlink else None
    formula = formula_cell.value if isinstance(formula_cell.value, str) else ""
    if m := _HYPERLINK_FORMULA.match(formula):
        link = link or m.group(1)
        text = text or (m.group(2) or "")
    return _Cell(text=text, link=link)


def _read_xlsx(data: bytes) -> list[list[_Cell]]:
    from openpyxl import load_workbook

    try:
        # read_only=False so hyperlink targets are available. The formula view finds =HYPERLINK("url", "text")
        # cells, which have no hyperlink object and often no cached value.
        values = load_workbook(io.BytesIO(data), read_only=False, data_only=True).active
        formulas = load_workbook(io.BytesIO(data), read_only=False, data_only=False).active
    except Exception as exc:  # noqa: BLE001 — any openpyxl failure means an unreadable file
        raise SheetError("Could not read the spreadsheet. Upload an .xlsx or .csv file.") from exc
    return [
        [_xlsx_cell(v, f) for v, f in zip(vrow, frow)]
        for vrow, frow in zip(values.iter_rows(), formulas.iter_rows())
    ]


def _read_csv(data: bytes) -> list[list[_Cell]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return [[_Cell(text=v.strip(), link=None) for v in row] for row in csv.reader(io.StringIO(text))]


def parse_sheet(filename: str, data: bytes) -> list[SheetRow]:
    return parse_sheet_ex(filename, data)[0]


def parse_sheet_ex(filename: str, data: bytes) -> tuple[list[SheetRow], bool]:
    """(rows, found_url_column). False when the links were picked up loose from any cell."""
    if len(data) > MAX_BYTES:
        raise SheetError(f"File is larger than {MAX_BYTES // 1_000_000} MB.")
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    if ext in ("xlsx", "xlsm"):
        rows = _read_xlsx(data)
    elif ext == "csv":
        rows = _read_csv(data)
    else:
        raise SheetError("Unsupported file type. Upload an .xlsx or .csv file.")

    # Keep each row's 1-based sheet number so results point at the right row even after blank rows.
    numbers = [i + 1 for i, r in enumerate(rows) if any(c.text or c.link for c in r)]
    rows = [r for r in rows if any(c.text or c.link for c in r)]
    if len(rows) > MAX_ROWS + 1:
        raise SheetError(f"Too many rows: {len(rows)} (max {MAX_ROWS}).")
    width = max((len(r) for r in rows), default=0)

    def col(i: int) -> list[_Cell]:
        return [r[i] if i < len(r) else _Cell("", None) for r in rows]

    url_col, best = None, 0
    for i in range(width):
        cells = col(i)
        if cells and cells[0].url is None:
            cells = cells[1:]  # a non-URL first cell is a header candidate; don't count it against the column
        cells = [c for c in cells if c.text or c.link]
        hits = sum(1 for c in cells if c.url)
        # One stray link doesn't make a URL column (unless the sheet has a single job row).
        if cells and hits / len(cells) >= 0.5 and hits >= min(2, len(rows) - 1) and hits > best:
            url_col, best = i, hits
    if url_col is None:
        # No column is mostly links: take every link in the sheet, row by row, left to right (no descriptions).
        loose = [(n, u) for n, r in zip(numbers, rows) for c in r for u in c.urls]
        if not loose:
            raise SheetError("No URL column found. One column must contain job links (http/https).")
        return _dedupe([SheetRow(rowIndex=n, url=u, jdText=None) for n, u in loose]), False

    has_header = col(url_col)[0].url is None
    body = rows[1:] if has_header else rows
    body_numbers = numbers[1:] if has_header else numbers

    jd_col, best_avg = None, 0.0
    for i in range(width):
        if i == url_col:
            continue
        texts = [r[i].text for r in body if i < len(r) and r[i].text]
        avg = sum(map(len, texts)) / len(texts) if texts else 0.0
        if avg >= MIN_JD_AVG_CHARS and avg > best_avg:
            jd_col, best_avg = i, avg

    company_col = next(
        (i for i, c in enumerate(rows[0]) if has_header and i not in (url_col, jd_col) and _COMPANY_HEADER.match(c.text.strip())),
        None,
    )

    out: list[SheetRow] = []
    for offset, r in enumerate(body):
        urls = r[url_col].urls if url_col < len(r) else []
        jd = r[jd_col].text if jd_col is not None and jd_col < len(r) and len(urls) == 1 else ""
        company = (r[company_col].text.strip() or None) if company_col is not None and company_col < len(r) else None
        for url in urls:  # several links in one cell are several jobs; a description can't be split between them
            out.append(SheetRow(
                rowIndex=body_numbers[offset], url=url, jdText=jd if len(jd) >= MIN_JD_CELL_CHARS else None, company=company,
            ))
    return _dedupe(out), True


class SheetRows(list):
    """The job rows, plus what reading the sheet did: links found, links whose URL was cleaned, duplicates dropped."""

    links = 0
    cleaned = 0
    duplicates = 0


def _dedupe(rows: list[SheetRow]) -> "SheetRows":
    """Clean each URL (urls.normalize_url) and keep the first row per cleaned URL."""
    out = SheetRows()
    seen: set[str] = set()
    out.links = len(rows)
    for row in rows:
        clean = normalize_url(row.url)
        out.cleaned += clean != row.url
        row.url = clean
        if row.url in seen:
            out.duplicates += 1
            continue
        seen.add(row.url)
        out.append(row)
    if len(out) > MAX_ROWS:
        raise SheetError(f"Too many rows: {len(out)} (max {MAX_ROWS}).")
    return out
