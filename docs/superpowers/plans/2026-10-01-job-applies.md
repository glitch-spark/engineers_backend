# Job Applies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upload a sheet of 300–800 job postings, score the selected resumes against each job, apply the
remote, clearance, freshness and location gates, and show a table with at most one suggested resume per
profile at or above a threshold.

**Architecture:** A backend package `app/job_apply/` holds pure, unit-tested modules (sheet, fetch, extract,
gates, resume_parse, scoring) plus a restartable asyncio worker that persists per-row progress in Mongo.
A `/job-applies` router serves runs and rows. The SPA adds a runs page and a results page that poll with SWR.

**Tech Stack:** FastAPI, Beanie/Motor, httpx, bs4, openpyxl, rapidfuzz, existing `llm_router`; React 18,
SWR, Tailwind.

**Spec:** `docs/superpowers/specs/2026-10-01-job-applies-design.md` (read it; section numbers below refer to it)

## Global Constraints

- **Python:** 3.10+ syntax, matching the repo. The venv is at `../engineers_backend/.venv`, from the backend
  worktree.
- **Data:** BSON keys are camelCase. Documents extend `TimestampedDocument`. Collections are
  `job_apply_runs`, `job_apply_rows`, `job_extraction_cache` and `job_posting_cache`.
- **Weights:** Required 32, All keywords 27, Years 13, Title 11, Education 9, Relevance 8.
- **Tier base weights:** required 3.0, core 2.0, mentioned 1.5, preferred 1.0, context 0.5.
- **Match credit:** exact 1.0, variant 0.85, fuzzy 0.70 (ratio ≥ 90, length ≥ 5).
- **Evidence multipliers:** recent role 1.0, skills/headline/summary 0.9, old role (ended more than 5 years
  ago) 0.8.
- **Defaults:** threshold 75, maxAgeDays 30, `K = 0.18`.
- **Limits:** at most 1,000 rows and 5 MB per sheet, one active run per user.
- **Concurrency:** httpx fetch 6, Playwright 2, LLM 4.
- **Worker lease:** heartbeat every 30 s; a run is stale after 2 min.
- **Network in tests:** unit tests never touch the network or Mongo. `tests/` integration tests need
  `MONGODB_URI_TEST`.
- **Commits:** end every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Frontend:** use `src/api/endpoints.ts` for API calls, SWR for fetching, and existing components
  (`PageHeader`, `MultiSelect`, `Select`, `Modal`).

## Review Focus

1. **PDF-sourced resumes.** These are single-line text with `--- Page N ---` markers and no newlines.
   Sections, date ranges and degree must still be found. Pinned in Task 2
   (`test_parse_flat_pdf_text`).
2. **Sheet quirks.** URLs may sit in hyperlink cells (`cell.hyperlink.target`) with display text like
   "Apply", and the header row may be missing. Detection must still find the URL and JD columns. Pinned in
   Task 6 (`test_hyperlink_cells`, `test_no_header_row`).
3. **Date ranges.** These use en/em dashes and many forms: "Jan 2020 – Present", "2019–2021",
   "03/2019 - 05/2022", "Sept. 2018 to Current". Pinned in Task 2 (`test_date_range_forms`).
4. **Wrapped LLM JSON.** The LLM may wrap its JSON in ```json fences or add prose. Parsing must stay lenient
   (reuse `llm_router._try_parse_jsonish`). Pinned in Task 3 (`test_parse_fenced_json`).
5. **Duplicate URLs.** The same posting may appear twice with different `utm_*` parameters, a trailing
   slash or different host case. It must be de-duplicated. Pinned in Task 6 (`test_dedupe_urls`).

---

## File Structure

Backend (`engineers_backend-job-applies/`):

| File | Responsibility |
|---|---|
| `app/job_apply/__init__.py` | Package marker |
| `app/job_apply/text.py` | Normalising, tokenising, stop-words, plural-tolerant term regex, mention counts |
| `app/job_apply/skills_dict.py` | Built-in skills → aliases, soft skills |
| `app/job_apply/sections.py` | JD section splitter, line-tier overrides, substantive text |
| `app/job_apply/schema.py` | Pydantic shapes: `JobExtraction`, `GateResult`, `TermHit`, `ComponentScore`, `ResumeScore` |
| `app/job_apply/resume_parse.py` | Resume text → `ParsedResume`, date ranges, years, degree, health |
| `app/job_apply/extract.py` | LLM prompt, parse/validate, rule-based fallback |
| `app/job_apply/gates.py` | Job and profile gates |
| `app/job_apply/scoring.py` | Term weights, matching, components, IDF, combine, knockouts, pick_best |
| `app/job_apply/pipeline.py` | `score_row` — pure orchestration of gates and scoring for one row |
| `app/job_apply/sheet.py` | xlsx/csv → `SheetRow[]` |
| `app/job_apply/fetch.py` | URL → `FetchResult` (ATS APIs, JSON-LD, HTML, Playwright) |
| `app/job_apply/worker.py` | Stage runner, lease, resume-on-start |
| `app/models/job_apply.py` | Beanie documents |
| `app/routers/job_applies.py` | API |
| `scripts/job_apply_calibrate.py` | Calibration report |
| `tests_unit/test_job_apply_*.py` | Unit tests per module |
| `tests/test_job_applies.py` | API lifecycle (real Mongo) |

Frontend (`engineers_app-job-applies/`):

| File | Responsibility |
|---|---|
| `src/api/client.ts` | Don't force a JSON Content-Type for `FormData` bodies |
| `src/api/endpoints.ts` | Job-apply types and calls |
| `src/pages/JobApplies.tsx` | Runs list and new-run panel |
| `src/pages/JobApplyRun.tsx` | Results table |
| `src/components/jobApplies/NewRunPanel.tsx` | Upload, profile/resume picker, threshold, max age |
| `src/components/jobApplies/RowDetail.tsx` | Per-resume breakdown |
| `src/components/jobApplies/format.ts` | Band colours and gate chip labels |
| `src/App.tsx`, `src/components/Sidebar.tsx` | Route and nav entry |

---

### Task 1: Text utilities, skills dictionary, JD sections

**Files:**
- Create: `app/job_apply/__init__.py`, `app/job_apply/text.py`, `app/job_apply/skills_dict.py`,
  `app/job_apply/sections.py`
- Modify: `requirements.txt` (add `openpyxl>=3.1`, `rapidfuzz>=3.9`)
- Test: `tests_unit/test_job_apply_text.py`

**Interfaces — produces:**

`text.py`
- `normalize(s: str) -> str` — NFKC, lowercase, dashes → `-`, collapses whitespace.
- `tokens(s: str) -> list[str]` — keeps `c++`, `c#`, `node.js`, `.net`, `ci/cd`.
- `singular(w: str) -> str`
- `STOPWORDS: frozenset[str]`
- `term_regex(term: str) -> re.Pattern[str]` — case-insensitive, non-alphanumeric boundaries, optional
  trailing `s`/`es`.
- `count_mentions(text: str, variants: Iterable[str]) -> int` — non-overlapping matches of any variant.

`skills_dict.py`
- `SKILLS: dict[str, tuple[str, ...]]` — canonical → aliases. About 300 entries: languages, frameworks,
  clouds, data, devops, testing, AI/ML.
- `SOFT_SKILLS: frozenset[str]`
- `aliases_for(term: str) -> tuple[str, ...]` — also matches when `term` is an alias.
- `find_skills(text: str) -> list[str]` — canonical names found.

`sections.py`
- `Kind = Literal["required", "core", "mentioned", "preferred", "context", "ignored"]`
- `@dataclass Section(kind: Kind, heading: str, lines: list[str])`
- `split_sections(jd: str) -> list[Section]` — headings are short lines (≤ 60 chars) matching the heading
  vocab, optionally ending with `:`, or markdown `#`/bold. Text before the first heading is `mentioned`.
- `line_kind(line: str, section: Kind) -> Kind` — draft §2 overrides.
- `substantive_text(jd: str) -> str` — drops `context` and `ignored` sections.

- [ ] **Step 1: Write the failing tests**

```python
def test_term_regex_plural_and_symbols():
    assert term_regex("REST API").search("built REST APIs daily")
    assert term_regex("C++").search("Modern C++ and Go")
    assert not term_regex("Go").search("Google")
    assert term_regex("Node.js").search("node.js services")

def test_count_mentions_and_aliases():
    assert count_mentions("Postgres and PostgreSQL; postgres", aliases_for("PostgreSQL") + ("PostgreSQL",)) == 3
    assert "Postgres" in aliases_for("postgresql")

def test_split_sections_kinds():
    jd = "About us\nWe build.\nRequirements:\n- 5+ years Python\n- Kubernetes is a plus\nBenefits\n- PTO"
    kinds = [s.kind for s in split_sections(jd)]
    assert kinds == ["context", "required", "ignored"]
    req = split_sections(jd)[1]
    assert [line_kind(l, req.kind) for l in req.lines] == ["required", "preferred"]
    assert "PTO" not in substantive_text(jd) and "We build" not in substantive_text(jd)

def test_responsibility_line_override():
    assert line_kind("Must have 3 years of React", "core") == "required"
    assert line_kind("Ship features weekly", "core") == "core"
```

- [ ] **Step 2: Run** `../engineers_backend/.venv/Scripts/python -m pytest tests_unit/test_job_apply_text.py -q`.
  Expect an ImportError.
- [ ] **Step 3: Implement** the three modules, then `pip install openpyxl rapidfuzz` into the venv.
- [ ] **Step 4: Run** the tests. Expect PASS.
- [ ] **Step 5: Commit** `feat(job-applies): text utils, skills dictionary, JD section splitter`.

### Task 2: Shared schema and resume parsing

**Files:**
- Create: `app/job_apply/schema.py`, `app/job_apply/resume_parse.py`
- Test: `tests_unit/test_job_apply_resume_parse.py`

**Interfaces — produces:**

`schema.py` (pydantic `BaseModel`s, following spec §3.2)
- `Tier = Literal["required", "core", "mentioned", "preferred", "context"]`
- `Term(term: str, aliases: list[str] = [], tier: Tier, soft: bool = False)`
- `MinYears(value: float, required: bool)`
- `Degree(level: int, required: bool, equivalentExperience: bool = False)` — level is 1 to 4.
- `Location(kind: Literal["country", "region"], value: str)`
- `WorkAuth(restricted: bool, countries: list[str] = [], note: str | None = None)`
- `Clearance(level: Literal["required", "preferred", "none"] = "none", note: str | None = None)`
- `JobExtraction(title: str = "", company: str = "", seniority: str | None, terms: list[Term], minYears: MinYears | None, degree: Degree | None, workMode: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown", allowedLocations: list[Location] = [], workAuthorization: WorkAuth | None, timezoneNote: str | None, clearance: Clearance = Clearance(), postedDate: date | None)`
- `GateResult(name: str, result: Literal["pass", "fail", "unknown"], reason: str = "")`
- `TermHit(term: str, tier: Tier, weight: float, credit: float, match: Literal["exact", "variant", "fuzzy", "missing"], where: Literal["recent", "skills", "old", "none"])`
- `ComponentScore(score: float, weight: int, detail: str = "")`
- `ResumeScore(accountId: str, resumeId: str, filename: str, total: int, band: str, components: dict[str, ComponentScore], terms: list[TermHit] = [], knockouts: list[str] = [], uploadedAt: datetime | None = None)`

`resume_parse.py`
- `@dataclass Role(window: str, start: date, end: date | None, text: str)` — `window` is about 80 chars
  around the date range (holds the title), `text` runs from the date range to the next one or the section
  end, and `end` is `None` for present.
- `@dataclass ParsedResume(text: str, headline: str, summary: str, skills: str, roles: list[Role], degree_level: int | None, stated_years: float | None)`
- `parse_date_ranges(text: str, run_date: date) -> list[tuple[date, date | None, int, int]]` — returns
  start, end, span start and span end.
- `parse_resume(markdown: str, run_date: date) -> ParsedResume` — headings are found in running text:
  an ALL-CAPS keyword, a line-start keyword, or `#`. Keyword groups cover
  experience/employment/work history, skills/technical skills, education, summary/profile, and
  projects/certifications. If there's no experience heading, the whole text is searched for roles.
  `headline` is the first 200 chars before the first heading, with `--- Page N ---` markers removed.
- `experience_years(roles: list[Role], run_date: date) -> float | None` — union of intervals, in years.
- `@dataclass Health(score: int, issues: list[str])`, `health(markdown: str) -> Health` — spec §3.1
  badge.

- [ ] **Step 1: Write the failing tests**

```python
RUN = date(2026, 10, 1)
FLAT = ("--- Page 1 --- JANE DOE Senior Software Engineer jane@x.com +1 555 123 4567 SUMMARY Backend engineer. "
        "EXPERIENCE Acme Corp Senior Software Engineer Jan 2021 – Present Built Kafka pipelines in Python. "
        "Beta Inc Software Engineer 2016 - 2020 Wrote Java services on AWS. "
        "SKILLS Python, Kafka, Terraform EDUCATION B.Sc. Computer Science, 2015")

def test_parse_flat_pdf_text():
    r = parse_resume(FLAT, RUN)
    assert len(r.roles) == 2 and r.roles[0].end is None
    assert "Kafka" in r.roles[0].text and "Java" in r.roles[1].text
    assert "Terraform" in r.skills and r.degree_level == 2
    assert "Senior Software Engineer" in r.headline

def test_date_range_forms():
    for s, years in [("Jan 2020 – Present", 6.7), ("2019–2021", 2.0), ("03/2019 - 05/2022", 3.2),
                     ("Sept. 2018 to Current", 8.1)]:
        rngs = parse_date_ranges(s, RUN)
        assert len(rngs) == 1
        assert abs(experience_years([Role("", rngs[0][0], rngs[0][1], "")], RUN) - years) < 0.15

def test_overlapping_roles_counted_once():
    text = "EXPERIENCE A Jan 2018 - Dec 2020 x B Jan 2020 - Dec 2021 y"
    assert abs(experience_years(parse_resume(text, RUN).roles, RUN) - 4.0) < 0.1

def test_stated_years_and_degrees():
    assert parse_resume("8+ years of experience building APIs", RUN).stated_years == 8
    assert parse_resume("EDUCATION Master of Science, MIT", RUN).degree_level == 3
    assert parse_resume("EDUCATION PhD Physics", RUN).degree_level == 4

def test_health_badge():
    assert health(FLAT).score == 100
    h = health("Just a few words")
    assert h.score == 100 - 20 - 20 - 8 - 8   # <50 words, no email, no phone, missing headings (medium)
```

- [ ] **Step 2: Run** the tests. Expect FAIL.
- [ ] **Step 3: Implement.** Month names cover `jan, january, sep, sept`, with an optional `.`.
  Present-words are `present, current, now, today`. A bare year range uses Jan → Jan of the end year.
- [ ] **Step 4: Run** the tests. Expect PASS.
- [ ] **Step 5: Commit** `feat(job-applies): shared schema and resume parser`.

### Task 3: Job extraction (LLM + rules fallback)

**Files:**
- Create: `app/job_apply/extract.py`
- Test: `tests_unit/test_job_apply_extract.py`

**Interfaces:**
- **Consumes:** `schema.JobExtraction`, `sections.*`, `skills_dict.*`,
  `llm_router.complete_with_fallback(prompt, user, *, validate, purpose) -> LlmResult` (`.text`), and
  `llm_router._try_parse_jsonish(raw) -> object | None`.
- **Produces:**
  - `PROMPT_VERSION = "job-extract-v1"`
  - `build_prompt(jd: str, run_date: date) -> str` — contains the JSON shape and the spec §3.2 rules
    verbatim.
  - `parse_extraction(raw: str) -> JobExtraction` — raises `ValueError`. Caps terms at 15 required and 40
    total. Drops empty terms.
  - `rules_extract(jd: str, run_date: date) -> JobExtraction`
  - `async extract_job(jd: str, *, user, run_date: date, llm=None) -> tuple[JobExtraction, Literal["llm", "rules"]]`
    — `llm` defaults to `complete_with_fallback`. Any exception or invalid JSON falls back to
    `rules_extract`.
  - `jd_hash(jd: str) -> str` — sha256 of `normalize(jd)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_fenced_json():
    raw = 'Here you go:\n```json\n{"title":"Data Analyst","terms":[{"term":"SQL","tier":"required"}],"workMode":"remote"}\n```'
    ex = parse_extraction(raw)
    assert ex.title == "Data Analyst" and ex.terms[0].tier == "required" and ex.workMode == "remote"

def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_extraction("sorry, I can't")

def test_caps_terms():
    terms = [{"term": f"t{i}", "tier": "required"} for i in range(30)]
    ex = parse_extraction(json.dumps({"terms": terms}))
    assert sum(t.tier == "required" for t in ex.terms) == 15

def test_rules_extract():
    jd = ("Senior Backend Engineer\nRequirements:\n- 5+ years of experience with Python and PostgreSQL\n"
          "- Bachelor's degree or equivalent experience\n- Active Secret clearance required\n"
          "Nice to have:\n- Kubernetes\nThis is a hybrid role, 3 days in office.\nPosted 10 days ago")
    ex = rules_extract(jd, date(2026, 10, 1))
    tiers = {t.term: t.tier for t in ex.terms}
    assert tiers["Python"] == "required" and tiers["Kubernetes"] == "preferred"
    assert ex.minYears == MinYears(value=5, required=True)
    assert ex.degree.level == 2 and ex.degree.equivalentExperience
    assert ex.clearance.level == "required" and ex.workMode == "hybrid"
    assert ex.postedDate == date(2026, 9, 21) and ex.seniority == "senior"

async def test_extract_job_falls_back_on_llm_error():
    async def boom(*a, **k): raise RuntimeError("down")
    ex, src = await extract_job("Requirements:\n- Python", user=None, run_date=date(2026, 10, 1), llm=boom)
    assert src == "rules" and ex.terms[0].term == "Python"
```

- [ ] **Step 2: Run** the tests. Expect FAIL.
- [ ] **Step 3: Implement.** The rules fallback reads remote, hybrid and onsite phrasing in the spirit of
  spec §3.2. Relative dates (`N days/weeks/months ago`, `today`, `yesterday`, `30+ days ago`) resolve
  against `run_date`. Capitalised unknown tokens become `mentioned` terms marked `soft=False` and flagged
  with an `"auto"` alias marker, so scoring applies ×0.5. `validate` raises when `parse_extraction` fails.
- [ ] **Step 4: Run** the tests. Expect PASS.
- [ ] **Step 5: Commit** `feat(job-applies): LLM job extraction with rule-based fallback`.

### Task 4: Eligibility gates

**Files:**
- Create: `app/job_apply/gates.py`
- Test: `tests_unit/test_job_apply_gates.py`

**Interfaces — produces:**
- `COUNTRY_REGION: dict[str, str]` — ISO-2 → `Latam | EU | Canada | US | Asia`.
- `covers(loc: Location, country: str | None, region: str | None) -> bool` — spec §3.7 region table.
- `job_gates(ex: JobExtraction, *, run_date: date, max_age_days: int, posted: date | None = None) -> list[GateResult]`
  — returns the gates named `remote`, `clearance` and `freshness`. `posted` overrides `ex.postedDate`.
- `profile_gates(ex: JobExtraction, *, country: str | None, region: str | None) -> list[GateResult]` —
  returns `location` and `workAuth`.
- `blocked(gates: list[GateResult]) -> bool` — true if any gate is `fail`.

- [ ] **Step 1: Write the failing tests**

```python
def ex(**kw): return JobExtraction(**kw)
RUN = date(2026, 10, 1)

def test_remote_gate():
    assert job_gates(ex(workMode="hybrid"), run_date=RUN, max_age_days=30)[0].result == "fail"
    assert job_gates(ex(workMode="unknown"), run_date=RUN, max_age_days=30)[0].result == "unknown"

def test_clearance_gate():
    g = {x.name: x for x in job_gates(ex(workMode="remote", clearance=Clearance(level="required")), run_date=RUN, max_age_days=30)}
    assert g["clearance"].result == "fail"
    g = {x.name: x for x in job_gates(ex(clearance=Clearance(level="preferred")), run_date=RUN, max_age_days=30)}
    assert g["clearance"].result == "pass" and "preferred" in g["clearance"].reason

def test_freshness_gate():
    f = lambda d, p=None: {x.name: x for x in job_gates(ex(postedDate=d), run_date=RUN, max_age_days=30, posted=p)}["freshness"].result
    assert f(date(2026, 9, 1)) == "pass" and f(date(2026, 8, 31)) == "fail" and f(None) == "unknown"
    assert f(date(2026, 9, 25), p=date(2026, 7, 1)) == "fail"   # JSON-LD date wins

def test_location_gate():
    us = [Location(kind="country", value="US")]
    pg = lambda locs, c, r: profile_gates(ex(allowedLocations=locs), country=c, region=r)[0].result
    assert pg(us, "BR", "Latam") == "fail" and pg(us, "US", "US") == "pass"
    assert pg([Location(kind="region", value="Americas")], "BR", None) == "pass"
    assert pg([Location(kind="region", value="EMEA")], "DE", "EU") == "pass"
    assert pg([Location(kind="region", value="Worldwide")], "IN", "Asia") == "pass"
    assert pg([], "BR", "Latam") == "unknown" and pg(us, None, None) == "unknown"

def test_work_auth_gate():
    g = profile_gates(ex(workAuthorization=WorkAuth(restricted=True, countries=["US"])), country="BR", region="Latam")
    assert {x.name: x.result for x in g}["workAuth"] == "fail"
```

- [ ] **Step 2: Run** the tests. Expect FAIL.
- [ ] **Step 3: Implement.** Freshness fails when `(run_date - posted).days > max_age_days`.
- [ ] **Step 4: Run** the tests. Expect PASS.
- [ ] **Step 5: Commit** `feat(job-applies): remote, clearance, freshness and location gates`.

### Task 5: Scoring engine and row pipeline

**Files:**
- Create: `app/job_apply/scoring.py`, `app/job_apply/pipeline.py`
- Test: `tests_unit/test_job_apply_scoring.py`

**Interfaces:**
- **Consumes:** Tasks 1, 2 and 4.
- **Produces (`scoring.py`):**
  - Constants: `WEIGHTS: dict[str, int]` (Global Constraints), `TIER_WEIGHT`, `K = 0.18`,
    `CREDIT = {"exact": 1.0, "variant": 0.85, "fuzzy": 0.70}`,
    `WHERE_MULT = {"recent": 1.0, "skills": 0.9, "old": 0.8}`.
  - `@dataclass WeightedTerm(term: Term, weight: float, variants: tuple[str, ...])`
  - `term_weights(ex: JobExtraction, jd: str) -> list[WeightedTerm]` — spec §3.3.
  - `match_term(wt: WeightedTerm, r: ParsedResume, run_date: date) -> TermHit` — spec §3.4, best location
    wins.
  - `weighted_share(hits: list[TermHit]) -> float | None` — 100 × Σw·c / Σw, or `None` when the list is
    empty.
  - `years_score(req: MinYears | None, have: float | None) -> float | None`
  - `title_score(ex: JobExtraction, r: ParsedResume) -> tuple[float, str]`
  - `seniority_level(title: str) -> int | None`
  - `education_score(req: Degree | None, have: int | None) -> float | None`
  - `@dataclass Idf(idf: dict[str, float], default: float)`, `build_idf(docs: list[str]) -> Idf`
  - `relevance(resume_text: str, jd_text: str, idf: Idf, k: float = K) -> tuple[float, float]` —
    returns score and cosine.
  - `combine(parts: dict[str, float | None], weights: dict[str, int] = WEIGHTS) -> int`
  - `band(total: int) -> str` — `strong | good | fair | weak`.
  - `knockouts(ex, hits, have_years, have_degree) -> list[str]`
  - `score_resume(ex, jd: str, r: ParsedResume, idf: Idf, *, run_date, account_id, resume_id, filename, uploaded_at) -> ResumeScore`
  - `pick_best(scores: list[ResumeScore]) -> ResumeScore | None` — spec §3.8 tie-break.
- **Produces (`pipeline.py`):**
  - `@dataclass ResumeInput(account_id: str, resume_id: str, filename: str, uploaded_at: datetime | None, parsed: ParsedResume)`
  - `@dataclass ProfileInfo(account_id: str, name: str, country: str | None, region: str | None)`
  - `@dataclass RowResult(profile_gates: list[dict], scores: list[ResumeScore], best_by_profile: list[ResumeScore], top_score: int | None)`
    — each `profile_gates` item is `{"accountId": str, "gates": list[GateResult]}`.
  - `score_row(ex, jd, *, job_blocked: bool, resumes: list[ResumeInput], profiles: dict[str, ProfileInfo], idf: Idf, run_date: date) -> RowResult`
    — scores always; best_by_profile is empty when `job_blocked`; otherwise it holds only profiles whose
    gates aren't blocked.

- [ ] **Step 1: Write the failing tests**

```python
def test_worked_example_old_weights():
    old = {"required": 30, "keywords": 25, "years": 12, "title": 10, "education": 8, "relevance": 7, "parse": 8}
    parts = {"required": 56, "keywords": 40, "years": 100, "title": 43, "education": None, "relevance": 60, "parse": 80}
    assert combine(parts, old) == 58

def test_term_weights_adjustments():
    ex = JobExtraction(title="Python Developer", terms=[Term(term="Python", tier="required"),
                       Term(term="communication", tier="required", soft=True)])
    w = {x.term.term: x.weight for x in term_weights(ex, "Python Python Python. communication")}
    assert w["Python"] == 3.0 + 0.5 + 1.0          # 2 extra mentions, in title
    assert w["communication"] == 1.5                # soft ×0.5

def test_match_credit_and_evidence():
    r = parse_resume("EXPERIENCE Acme Jan 2023 - Present Postgres tuning. Old Co 2010 - 2015 Perl scripts. "
                     "SKILLS Kubernetis, Docker", date(2026, 10, 1))
    m = lambda term, aliases=(): match_term(WeightedTerm(Term(term=term, tier="required", aliases=list(aliases)), 3.0,
                                            (term, *aliases)), r, date(2026, 10, 1))
    assert m("PostgreSQL", ["Postgres"]).credit == pytest.approx(0.85)        # variant, recent
    assert m("Perl").credit == pytest.approx(0.8)                             # exact, old role
    assert m("Docker").credit == pytest.approx(0.9)                           # skills only
    assert m("Kubernetes").credit == pytest.approx(0.7 * 0.9) and m("Kubernetes").match == "fuzzy"
    assert m("Rust").credit == 0

def test_years_education_title():
    assert years_score(MinYears(value=5, required=True), 3) == pytest.approx(52)
    assert years_score(MinYears(value=5, required=True), None) == 50
    assert education_score(Degree(level=3, required=True), 2) == 60
    assert education_score(Degree(level=2, required=False), None) == 20
    assert education_score(Degree(level=2, required=True, equivalentExperience=True), None) == 40
    r = parse_resume("Staff Software Engineer EXPERIENCE X Staff Software Engineer 2015 - Present", date(2026, 10, 1))
    s, _ = title_score(JobExtraction(title="Junior Software Engineer", seniority="junior"), r)
    assert s == pytest.approx(60)                   # core title present (100) × 0.6 (level diff 3)

def test_relevance_idf_downweights_boilerplate():
    jds = ["fast-paced team python kafka", "fast-paced team java spring", "fast-paced team react css"]
    idf = build_idf(jds)
    hi, _ = relevance("python kafka pipelines", jds[0], idf)
    lo, _ = relevance("fast-paced team player", jds[0], idf)
    assert hi > lo

def test_pick_best_tiebreak():
    a = ResumeScore(accountId="p", resumeId="a", filename="a", total=80, band="strong", components={}, knockouts=["x"])
    b = a.model_copy(update={"resumeId": "b", "knockouts": []})
    assert pick_best([a, b]).resumeId == "b"

def test_score_row_one_per_profile_and_blocking():
    # two resumes in profile P1, one in P2 whose location gate fails → best_by_profile has only P1's best
    ...
    assert [s.accountId for s in res.best_by_profile] == ["P1"] and res.top_score == res.best_by_profile[0].total
    assert score_row(..., job_blocked=True, ...).best_by_profile == []
```

  `test_score_row_one_per_profile_and_blocking` builds two `ResumeInput`s for `P1` with different text and
  one for `P2` with `ProfileInfo(country="BR")`, against `allowedLocations=[US]`.

- [ ] **Step 2: Run** the tests. Expect FAIL.
- [ ] **Step 3: Implement.**
  - Fuzzy matching compares each variant (length ≥ 5) against resume tokens, plus bigrams for multi-word
    terms, using `rapidfuzz.fuzz.ratio`.
  - Title matching uses the core title (seniority words removed) with
    `rapidfuzz.fuzz.token_set_ratio` against the headline and each `Role.window`.
  - The resume's seniority comes from `roles[0].window`, falling back to the headline. Roles are sorted by
    start date, newest first.
  - TF-IDF is unigrams plus bigrams, with tf = 1 + ln(count).
- [ ] **Step 4: Run** the tests. Expect PASS.
- [ ] **Step 5: Commit** `feat(job-applies): scoring engine v2 and row pipeline`.

### Task 6: Sheet parsing

**Files:**
- Create: `app/job_apply/sheet.py`
- Test: `tests_unit/test_job_apply_sheet.py` (it builds xlsx in memory with openpyxl)

**Interfaces — produces:**
- `class SheetError(ValueError)`
- `@dataclass SheetRow(rowIndex: int, url: str | None, jdText: str | None)`
- `MAX_ROWS = 1000`, `MAX_BYTES = 5_000_000`
- `normalize_url(url: str) -> str` — lowercase host; strip `utm_*`, `gh_src`, `source`, `ref`, `lever-source`
  and fragments; strip a trailing slash.
- `parse_sheet(filename: str, data: bytes) -> list[SheetRow]` — reads `.xlsx` (first sheet, `read_only=False`
  so hyperlinks are readable) or `.csv` (utf-8-sig). The URL column is where ≥ 50% of non-empty cells (or
  their hyperlink targets) match `^https?://`. The JD column is the other column with the longest average
  text, provided that average is ≥ 200 chars; otherwise there's no JD column. A first row with no URL in
  the URL column is treated as a header. Rows without a URL are skipped. Duplicates by `normalize_url`
  keep the first. Raises `SheetError` with a clear message when there's no URL column, there are too many
  rows, the file is too large, or the type is wrong.

- [ ] **Step 1: Write the failing tests**: `test_detects_columns_with_header`, `test_no_header_row`,
  `test_hyperlink_cells` (display text "Apply", target URL), `test_dedupe_urls`
  (`https://Jobs.x.com/1?utm_source=li` and `https://jobs.x.com/1/` collapse), `test_csv`,
  `test_url_only_sheet` (`jdText is None`), `test_too_many_rows_raises`, `test_no_url_column_raises`.
- [ ] **Step 2: Run** the tests. Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expect PASS.
- [ ] **Step 5: Commit** `feat(job-applies): sheet parsing with column detection`.

### Task 7: Fetching URL-only postings

**Files:**
- Create: `app/job_apply/fetch.py`
- Test: `tests_unit/test_job_apply_fetch.py` (uses `httpx.MockTransport`, with no network)

**Interfaces — produces:**
- `class FetchError(Exception)`
- `@dataclass FetchResult(text: str, source: Literal["ats_api", "html", "browser"], postedDate: date | None = None, title: str = "", company: str = "", location: str = "")`
- `route(url: str) -> tuple[str, dict]` — returns a kind (`greenhouse | lever | ashby | workable |
  smartrecruiters | workday | unsupported | generic`) and its params (board, job id, tenant, and so on).
- `parse_jsonld(html: str) -> dict | None` — the first `JobPosting` object, handling `@graph` and lists.
- `html_to_text(html: str) -> str` — bs4. Drops script, style, nav, header and footer.
- `async fetch_job(url: str, *, client: httpx.AsyncClient, browser: Callable[[str], Awaitable[str]] | None = None) -> FetchResult`
  - **ATS endpoints:**
    - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}`, reading `content` as
      HTML-escaped text and `updated_at`.
    - Lever: `https://api.lever.co/v0/postings/{company}/{id}`, reading `descriptionPlain` plus lists, and
      `createdAt` in ms.
    - Ashby: `https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=false`, then find the
      job by id (`descriptionPlain`, `publishedAt`, `isRemote`, `workplaceType`).
    - Workable: `https://apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}`.
    - SmartRecruiters: `https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}`.
    - Workday: `https://{host}/wday/cxs/{tenant}/{site}/job/{path}` (`jobPostingInfo.jobDescription`,
      `startDate`).
  - **Order:** if an ATS call fails, fall back to the generic path. The generic path is a GET with a
    browser UA and a 20 s timeout; it uses JSON-LD `description` and `datePosted` if present, otherwise
    `html_to_text`. If the text is under 400 chars and `browser` is given, the browser HTML is used
    (`source = "browser"`).
  - **Unsupported:** `linkedin.com` and `indeed.*` raise `FetchError("unsupported host")`. Fewer than
    200 chars of final text raises `FetchError("no job text")`.
- `async browser_html(url: str) -> str` — Playwright chromium, guarded by a module-level semaphore of 2,
  30 s timeout.

- [ ] **Step 1: Write the failing tests**: `test_route_kinds` (one URL per ATS plus linkedin plus generic),
  `test_greenhouse_api`, `test_lever_api`, `test_generic_jsonld_date`, `test_generic_text_fallback`,
  `test_unsupported_host_raises`, `test_ats_failure_falls_back_to_generic`. Fixtures are inline JSON/HTML
  strings.
- [ ] **Step 2: Run** the tests. Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expect PASS.
- [ ] **Step 5: Commit** `feat(job-applies): fetch postings via ATS APIs, JSON-LD and HTML`.

### Task 8: Models and worker

**Files:**
- Create: `app/models/job_apply.py`, `app/job_apply/worker.py`
- Modify: `app/models/__init__.py` (export), `app/db.py` (`DOCUMENT_MODELS`), `app/main.py` (the lifespan
  calls `worker.resume_stale_runs()` after `init_db()` as a background task, skipped when the env var
  `JOB_APPLY_WORKER_DISABLED=1`)
- Test: `tests_unit/test_job_apply_worker.py` (pure helpers only)

**Interfaces — produces:**

`models/job_apply.py`
- `RunSelection(accountId: PydanticObjectId, resumeIds: list[PydanticObjectId] = [])`
- `RunCounts(total=0, fetched=0, extracted=0, scored=0, excluded=0, failed=0)`
- `JobApplyRun` — fields per spec §4.1. `Settings.name = "job_apply_runs"`, index `(userId, createdAt)`.
- `JobApplyRow` — fields per spec §4.1, plus `title: str = ""`, `company: str = ""`,
  `postedDate: date | None`, `extractionSource`. `scores`, `bestByProfile`, `gates` and `profileGates`
  use the schema models. Indexes: `(runId, rowIndex)`, `(runId, status)`, `(runId, topScore)`.
- `JobExtractionCache(key: Indexed(str, unique=True), extraction: JobExtraction)`
- `JobPostingCache(url: Indexed(str, unique=True), text: str, source: str, postedDate: date | None, fetchedAt: datetime)`

`worker.py`
- `FETCH = fetch.fetch_job`, `EXTRACT = extract.extract_job` (module attributes, so tests and the API test
  can monkeypatch them).
- `async start_run(run_id: PydanticObjectId) -> None` — `asyncio.create_task(run_worker(run_id))`, with
  the task held in a module set.
- `async run_worker(run_id) -> None` — runs the stages from spec §4.3, using the semaphores from Global
  Constraints. Every stage selects only rows whose status is still before it, so it's idempotent on resume.
  - **Fetch:** checks `JobPostingCache` (7-day TTL) first.
  - **Extract:** checks `JobExtractionCache` with key `jd_hash + PROMPT_VERSION` first. The posted date
    priority is fetch, then extraction. Rows failing job gates are marked `excluded`.
  - **Score:** loads the accounts, filters resumes by selection, parses each resume once, builds the IDF
    from `substantive_text` of every row with an extraction, calls `pipeline.score_row`, and saves.
  - **Progress:** counts go through `$inc`. A heartbeat task updates `heartbeatAt` every 30 s. The run's
    status is checked every 25 rows, and the worker stops on `cancelled`. Any exception sets
    `status="failed"` and `error`.
- `async resume_stale_runs() -> int` — runs with status `queued`/`running` and `heartbeatAt` null or older
  than 2 min get `start_run`.
- Pure helpers, unit-tested:
  - `stage_rows(statuses: list[str], stage: Literal["fetch", "extract", "score"]) -> list[int]` — the
    indexes needing that stage.
  - `posted_for_row(fetched: date | None, extracted: date | None) -> date | None`

- [ ] **Step 1: Write the failing tests**: `test_stage_rows_idempotent`:
  - fetch selects `pending` rows without jdText
  - extract selects `pending | fetched`
  - score selects `extracted`, plus `excluded` rows without scores

  And `test_posted_priority`.
- [ ] **Step 2: Run** the tests. Expect FAIL.
- [ ] **Step 3: Implement** the models, registration and worker. For DB calls, follow the
  `ResumeApplication` patterns in `app/routers/resume.py`.
- [ ] **Step 4: Run** `python -m pytest tests_unit -q`. All tests pass, including the existing ones, and
  `python -c "import app.main"` succeeds.
- [ ] **Step 5: Commit** `feat(job-applies): run/row models and restartable worker`.

### Task 9: API router and integration test

**Files:**
- Create: `app/routers/job_applies.py`, `tests/test_job_applies.py`
- Modify: `app/main.py` (`include_router`), `tests/conftest.py` (add the four new models to `db_clean`, and
  set `JOB_APPLY_WORKER_DISABLED=1` before importing the app)

**Interfaces:**
- **Consumes:** Tasks 6 and 8, and the `_load_owned_account` pattern (ownership; admins may select any
  account).
- **Produces:** the endpoints in spec §4.4, plus `GET /job-applies/rows/{id}` for full detail.
  - **Rows list:** `GET /runs/{id}/rows?view=&accountId=&minScore=&page=&limit=` (default limit 50, max 200)
    excludes `jdText` and `scores.terms`. Each row includes `suggestions` = the `bestByProfile` entries
    with `total >= run.threshold`.
  - **View filters:**
    - `suggested`: `topScore >= threshold`
    - `excluded`: `status == "excluded"`, or a scored row with no suggestions whose profile gates all
      failed
    - `failed`: `fetch_failed | llm_failed`
  - **Responses:** use the camelCase field names from the models, with `_id` as a string, matching the
    other routers. Errors follow the existing `{error}` shape.

- [ ] **Step 1: Write the failing integration test** `tests/test_job_applies.py`. It monkeypatches
  `worker.EXTRACT` and `worker.FETCH` with fakes, then calls `await worker.run_worker(run_id)` directly
  instead of the background task.
  - `test_create_run_and_results`: 3 rows (remote+US, hybrid, URL-only) and 1 account with 2 resumes. The
    remote row has exactly one suggestion, the hybrid row is excluded, the URL-only row is fetched.
  - `test_threshold_patch_changes_suggestions`
  - `test_mark_applied`
  - `test_cancel_and_retry_failed`
  - `test_second_active_run_409`
  - `test_cannot_select_foreign_account_404`
  - `test_bad_file_400`
- [ ] **Step 2: Run** `python -m pytest tests/test_job_applies.py -q`. It is skipped here when
  `MONGODB_URI_TEST` is unset (conftest raises), so record that it was not run. Also run
  `python -c "from app.main import app; print([r.path for r in app.routes if 'job-applies' in r.path])"`
  and expect all the paths listed.
- [ ] **Step 3: Implement** the router.
- [ ] **Step 4: Run** the unit suite plus the import/route check again. Expect PASS.
- [ ] **Step 5: Commit** `feat(job-applies): /job-applies API`.

### Task 10: Calibration script

**Files:**
- Create: `scripts/job_apply_calibrate.py`
- Test: none beyond `--help` running. It is a report script.

**Interface:** `python -m scripts.job_apply_calibrate labels.csv [--k 0.18]`.
- **CSV columns:** `jd_path_or_text`, `resume_path`, `verdict` (`apply | maybe | skip`).
- **Extraction:** uses `rules_extract` by default; `--llm` uses `extract_job` with OpenAI.
- **Output:** a per-verdict min/median/max table, and the cutoff that maximises
  (apply ≥ cutoff) + (skip < cutoff) accuracy.

- [ ] **Step 1: Implement**, then run it on a 3-row fixture CSV in the scratchpad and confirm the table
  prints.
- [ ] **Step 2: Commit** `feat(job-applies): calibration report script`.

### Task 11: Frontend API layer

**Files:**
- Modify: `src/api/client.ts` (skip the default JSON Content-Type when `body instanceof FormData`),
  `src/api/endpoints.ts`

**Interfaces — produces (in `endpoints.ts`):**
- **Types:** `JobApplyRun`, `JobApplyRunStatus`, `JobApplyRowStatus`, `GateResult`, `ComponentScore`,
  `TermHit`, `ResumeScore`, `JobApplyRow` (list shape with `suggestions: ResumeScore[]`), `JobApplyRowDetail`.
  Field names mirror the backend.
- **Calls:**
  - `createJobApplyRun(file: File, selection: {accountId: string; resumeIds: string[]}[], threshold: number, maxAgeDays: number)`
  - `listJobApplyRuns()`, `getJobApplyRun(id)`
  - `listJobApplyRows(id, params: {view?, accountId?, minScore?, page?, limit?})`
  - `getJobApplyRow(rowId)`, `updateJobApplyRun(id, {threshold})`, `cancelJobApplyRun(id)`,
    `retryJobApplyRun(id)`, `deleteJobApplyRun(id)`, `setJobApplyRowApplied(rowId, applied)`
- [ ] **Step 1: Implement.** Link `node_modules` from `../engineers_app` if it is missing:
  `cmd //c mklink /J node_modules ..\\engineers_app\\node_modules`.
- [ ] **Step 2: Run** `npm run lint`. Expect no errors.
- [ ] **Step 3: Commit** `feat(job-applies): API client for job applies`.

### Task 12: Runs page and new-run panel

**Files:**
- Create: `src/pages/JobApplies.tsx`, `src/components/jobApplies/NewRunPanel.tsx`,
  `src/components/jobApplies/format.ts`
- Modify: `src/App.tsx` (`/job-applies`, `/job-applies/:runId` under `Protected`), `src/components/Sidebar.tsx`
  (a "Job Applies" entry after "Resumes")

**Behaviour:**
- **New-run panel:**
  - **Profiles:** loaded with `listAccounts({limit: 200})`; each response row has
    `resumes[{id, filename}]`, `country` and `region`. Profile `MultiSelect` options show
    `name · country`.
  - **Resumes:** for each picked profile, a checkbox list of its resumes, all checked by default.
  - **Settings:** a threshold range input (50–95, default 75, shows the value) and max age (number,
    default 30).
  - **Upload:** the file input accepts `.xlsx,.csv`. Submit is disabled until there's a file and at least
    one resume. On 202 it navigates to `/job-applies/{runId}`. On 409 it shows a toast with the API
    message.
- **Runs list:** a table of file name, created date, a status badge, a progress bar
  (`(fetched/extracted/scored)/total`), suggested count (`counts.scored - counts.excluded`) and a delete
  action (`ConfirmDialog`). SWR refreshes every 3 s while any run is queued or running.
- `format.ts`: `bandClass(total)`, `gateChip(g: GateResult) -> {label, tone: 'fail' | 'warn' | 'ok'}`,
  `STATUS_LABEL`.

- [ ] **Step 1: Implement.**
- [ ] **Step 2: Run** `npm run lint`. Expect no errors.
- [ ] **Step 3: Commit** `feat(job-applies): runs page with new-run panel`.

### Task 13: Results page

**Files:**
- Create: `src/pages/JobApplyRun.tsx`, `src/components/jobApplies/RowDetail.tsx`

**Behaviour:**
- **Header:** file name, status, progress, and Cancel while the run is active. "Retry failed" appears when
  `counts.failed > 0`.
- **Toolbar:**
  - view tabs: All / Suggested / Excluded / Failed
  - profile `Select`
  - threshold range, debounced 400 ms, then `updateJobApplyRun`, then a `mutate` of the rows
- **Table:**
  - **Job:** title and company, linked to the URL (`target=_blank rel=noopener`).
  - **Posting details:** posted (with a "date unknown" chip), work mode, location (from `allowedLocations`).
  - **Gate chips:** job gates, plus profile gates that failed.
  - **Suggestions:** chips `Profile · filename · score`, coloured by band.
  - **Applied:** a checkbox.
  - **Paging:** 50 rows per page with pagination. Rows refresh every 3 s while the run is active.
- **Row expand:** fetches `getJobApplyRow` (`RowDetail`). For each resume score, it shows the components
  table (score, weight, points lost = weight × (100 − score) ÷ Σ applicable weights), matched and missing
  terms grouped by tier, and knockouts. It also shows the extraction source and a low-confidence note for
  `rules`.

- [ ] **Step 1: Implement.**
- [ ] **Step 2: Run** `npm run lint` and `npm run build`. Expect both to succeed.
- [ ] **Step 3: Commit** `feat(job-applies): results table with breakdown`.

### Task 14: End-to-end smoke and review

- [ ] **Step 1:** Run the backend unit suite `python -m pytest tests_unit -q`. Expect all tests to pass.
- [ ] **Step 2:** Run a smoke script in the scratchpad that runs `parse_sheet`, `rules_extract`,
  `job_gates`, `parse_resume` and `score_row` on a 5-row sample sheet and two sample resumes. Print the
  table and check it looks sane.
- [ ] **Step 3:** Request a whole-branch code review (superpowers:requesting-code-review) of both repos,
  then fix the findings.
