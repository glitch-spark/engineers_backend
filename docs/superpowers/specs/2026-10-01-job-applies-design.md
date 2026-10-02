# Job Applies — batch job matching and resume suggestion

Date: 2026-10-01
Branches: `feat/job-applies` in `engineers_backend` (from `dev`) and `engineers_app` (from `main`)
Status: approved design, pending spec review

## 1. Goal

A user uploads a spreadsheet of 300–800 job postings, picks profiles and resumes, and gets back a table with
one row per job. Each row shows whether the job is eligible (remote, no clearance, fresh, location allowed)
and which resumes to apply with: at most one per profile, and only those scoring at or above a threshold the
user chooses (default 75).

### Decisions already made

| Topic | Decision |
|---|---|
| Input | `.xlsx` / `.csv`. A row has either a URL only (we fetch the posting) or a URL plus the full JD text. Post date and location, when present, sit inside the JD text. |
| AI usage | One LLM call per job extracts structured requirements. Resumes are scored deterministically (no LLM per job × resume). |
| Remote / clearance / freshness | Hard filter plus a flag. The row stays visible with the reason. |
| Freshness | Posted no more than 30 days before the run. Unknown date passes, flagged "date unknown". |
| Location | Checked per profile against `Account.country` / `Account.region`. A mismatch excludes that profile for that job. An unstated location passes. |
| Suggestions | Every eligible profile contributes its single best resume when that resume is at or above the threshold. |
| Output | New page in `engineers_app`. Runs are saved and listed, and rows can be marked applied. |
| Execution | Backend run with persisted rows and a restartable background worker (approach A). |
| Parseability | Removed from the score. Shown as a per-resume health badge. |

### Out of scope

Auto-applying, Excel export of results, LinkedIn/Indeed scraping, generating tailored resumes from the
table, multi-user sharing of runs.

## 2. Domain mapping

- **Profile** is `Account` (`app/models/account.py`). Location fields: `country` (ISO-3166 alpha-2) and
  `region` (`Latam | EU | Canada | US | Asia`).
- **Resume** is `Account.resumes[]` (`Resume{id, filename, markdown, uploadedAt}`). Scoring reads `markdown`.
- Only the user's own, non-archived accounts can be selected (same ownership check as `/resume/generate`).

## 3. Scoring v2 (replaces the ATS scoring draft)

### 3.1 Overall score

```
score = round( Σ (weight × component) ÷ Σ weight )   over applicable components only
```

| Component | Weight | Measures |
|---|---|---|
| Required skills | 32 | Weighted share of the job's must-have terms found in the resume |
| All keywords | 27 | Weighted share of every screening term found |
| Years of experience | 13 | Dated-role years vs. the minimum asked |
| Job title match | 11 | Core-title similarity × seniority alignment |
| Education | 9 | Highest degree vs. minimum required |
| Content relevance | 8 | TF-IDF cosine similarity between resume and JD |
| **Total** | **100** | |

Components that don't apply are left out, and the remaining weights are re-normalised (unchanged from the
draft): Required skills when there are no required terms, Years when none are stated, Education when no
degree is stated. Bands are unchanged: 80–100 Strong, 65–79 Good, 50–64 Fair, 0–49 Weak.

**What changed from the draft:** Parseability (weight 8) is no longer part of the score, and its weight is
spread across the other components. Resumes are stored as markdown, so tables, images and columns can't be
detected, and the value is the same for every job, so it can't separate resumes. A per-resume
**health badge** keeps the text-detectable checks: email found, phone found, Experience/Education/Skills
headings present, word count ≥ 50. It uses the draft's deductions (high 20, medium 8, low 3).

### 3.2 Job extraction (LLM, once per job)

Prompt via `llm_router.complete_with_fallback(prompt, user, purpose="job_extract", validate=...)`. The
validator rejects output that isn't JSON matching the schema below, which triggers the OpenAI fallback.
Input JD is truncated to `MAX_JD_BYTES`.

Illustrative shape (comments are annotations, not part of the output):

```json
{
  "title": "Senior Data Analyst",
  "company": "Acme",
  "seniority": "senior",                       // intern|junior|mid|senior|staff|principal|director|null
  "terms": [
    {"term": "PostgreSQL", "aliases": ["Postgres", "psql"],
     "tier": "required",                       // required|core|mentioned|preferred|context
     "soft": false}
  ],
  "minYears": {"value": 5, "required": true},  // or null
  "degree": {"level": 2, "required": true, "equivalentExperience": false},  // or null; 1 assoc, 2 bachelor, 3 master, 4 doctorate
  "workMode": "remote",                        // remote|hybrid|onsite|unknown
  "allowedLocations": [{"kind": "region", "value": "Americas"}, {"kind": "country", "value": "US"}],  // [] = unstated
  "workAuthorization": {"restricted": true, "countries": ["US"], "note": "US citizens only"},     // or null
  "timezoneNote": "4h overlap with EST",       // or null; flag only
  "clearance": {"level": "required", "note": "Active Secret"},  // required|preferred|none
  "postedDate": "2026-09-20"                   // ISO date, resolved from relative phrases against the run date; or null
}
```

Prompt rules the extraction must follow:
- At most 15 `required` terms and 40 terms in total. Terms are skills, tools, technologies, methods or
  domains, not generic words.
- Tier comes from the section and the line wording, as in the draft. "Preferred / a plus / bonus / ideally"
  is `preferred`. "Required / must / minimum / at least / N years" is `required`, even inside
  Responsibilities.
- `workMode`: "remote with occasional travel or offsites" is `remote`. "Remote but must live within
  commuting distance of …" or "N days in office" is `hybrid`.
- `clearance.level = required` covers "must hold" and "must be able to obtain" a clearance.

**Fallback (rule-based)** when both LLM tiers fail: the draft's heading-based section splitter and line-wording
overrides, a built-in skills dictionary with aliases (~300 tech skills, `app/job_apply/skills_dict.py`),
capitalised unknown tool names (×0.5), and regexes for years, degree, work mode, clearance and posted date.
The row is marked `extractionSource = "rules"` and flagged *low-confidence extraction*.

**Cache.** `JobExtractionCache` is keyed by `sha256(normalised JD) + PROMPT_VERSION`. A JD seen in any
earlier run costs no LLM call.

### 3.3 Term weights

| Tier | Base weight |
|---|---|
| required | 3.0 |
| core (responsibilities) | 2.0 |
| mentioned (unheaded) | 1.5 |
| preferred | 1.0 |
| context (about the company) | 0.5 |

Adjustments, computed deterministically on the JD text using the term and its aliases:
- +0.25 per extra mention, capped at +1.0
- +1.0 if the term appears in the job title
- ×0.5 for soft skills
- ×0.5 for capitalised names detected by the rule-based fallback only

### 3.4 Finding terms in the resume

Match credit, using the best match found:

| Match | Rule | Credit |
|---|---|---|
| Exact | The posting's own spelling, case-insensitive, word-boundary, plurals ignored | 100% |
| Variant | Any LLM-supplied alias, or a skills-dictionary alias | 85% |
| Fuzzy | `rapidfuzz.fuzz.ratio ≥ 90` against resume tokens/bigrams, term length ≥ 5 | 70% |
| Missing | | 0% |

The evidence multiplier depends on where the term was found, and the best location wins:

| Where found | Multiplier |
|---|---|
| An experience bullet in a role that is current or ended within the last 5 years | 1.0 |
| Only the Skills section, headline or summary | 0.9 |
| Only bullets of roles that ended more than 5 years before the run date | 0.8 |

`term credit = match credit × evidence multiplier`.

### 3.5 Components

**Required skills / All keywords**

```
score = 100 × Σ (weight × credit) ÷ Σ weight
```

The first is over required terms only, the second over all terms. Required terms count in both.

**Years of experience**

- **From the job:** `minYears` (required beats preferred).
- **From the resume:** the union of the date ranges of roles in the Experience section, with overlaps counted
  once and "Present" meaning the run date. If no dated roles are found, the first "N+ years of experience"
  statement is used instead.

| Situation | Score |
|---|---|
| Meets or exceeds the minimum | 100 |
| No dated roles and no statement | 50 |
| Below the minimum | `max(0, 100 − 120 × (wanted − have) ÷ wanted)` |

**Job title match**

1. Normalise both titles: drop seniority words (Intern, Junior, Associate, Senior, Sr, Lead, Staff,
   Principal, I/II/III/IV) and anything in brackets, then lowercase.
2. If the job's core title appears in the resume text, similarity is 100. Otherwise similarity is the best
   `rapidfuzz.fuzz.token_set_ratio` against the resume headline and each role title.
3. Seniority levels:

   | Level | Words |
   |---|---|
   | 0 | intern |
   | 1 | junior, associate, I |
   | 2 | mid, II, or no word |
   | 3 | senior, III |
   | 4 | staff, lead |
   | 5 | principal, architect |
   | 6 | director, head, VP |

   The job's level comes from the extraction. The resume's level comes from its most recent role title, or
   the headline.
4. Seniority factor:

   | Difference in levels | Factor |
   |---|---|
   | 0 or 1, or either side has no seniority word | 1.0 |
   | 2 | 0.8 |
   | 3 or more | 0.6 |

5. `score = similarity × factor`.

**Education** — unchanged from the draft. The resume's degree comes from its Education section by regex
(PhD/Doctorate=4, Master/MS/MSc/MBA/MEng=3, Bachelor/BS/BSc/BA/BEng/BTech=2, Associate/Diploma=1).

| Situation | Score |
|---|---|
| Meets or exceeds | 100 |
| No degree found | 20 (40 if equivalent experience is accepted) |
| One level short | 60 (75 if the degree is only preferred or equivalent experience is accepted) |
| Two or more levels short | 25 (40 if the degree is only preferred or equivalent experience is accepted) |

**Content relevance**

- **JD text:** the substantive sections only (About, Benefits and Legal/EEO are removed by the section
  splitter).
- **Vectors:** unigrams plus bigrams, English stop-words removed, sublinear TF, and
  IDF = `ln((1+N)/(1+df)) + 1`, where N and df are computed over **all JDs in the run**.
- **Score:** cosine similarity `s` between resume and JD, mapped to `score = 100 × (1 − e^(−s ÷ K))`.
  `K` defaults to 0.18 and is a named constant, re-tuned during calibration (§3.9), because TF-IDF shifts
  the similarity distribution compared with the draft's raw counts.

### 3.6 Knockout risks (warnings, don't change the score)

- A required, non-soft term is missing.
- Experience is more than 0.5 years below a *required* minimum.
- The degree is below a *required* level and equivalent experience isn't accepted.

### 3.7 Eligibility gates (decide eligibility, never change the score)

Each gate returns `pass | fail | unknown` with a reason. `unknown` counts as pass and shows a flag.

| Gate | Scope | Fail when | Unknown when |
|---|---|---|---|
| Remote only | job | `workMode` is hybrid or onsite | `workMode` is unknown |
| Clearance | job | `clearance.level = required` | — (`preferred` shows a flag only) |
| Freshness | job | posted more than `maxAgeDays` (default 30) days before the run | no date found |
| Location | profile | `allowedLocations` is non-empty and none covers the profile's country or region | `allowedLocations` is empty, or the profile has no country or region |
| Work authorization | profile | `workAuthorization.restricted` and the profile's country isn't in `countries` | the profile has no country |

- **Posted-date source priority:** JSON-LD `datePosted` from the fetch, then the ATS API date, then the
  LLM's `postedDate`.
- **Region coverage:**
  - `Worldwide` or `Anywhere` covers every profile.
  - `Americas` covers US, Canada and Latam.
  - `North America` covers US and Canada.
  - `EMEA` and `Europe` cover EU.
  - `APAC` covers Asia.
  - A country entry matches `Account.country`, or the profile's region when the country falls in that
    region.
- `timezoneNote` is shown as a flag only.

### 3.8 Suggestion rule

For each job that passes every job-level gate, and for each selected profile that passes its profile gates:

1. Score every selected resume of that profile.
2. Pick the profile's best resume by `score`. Ties go to fewer knockouts, then the newest `uploadedAt`.
3. Store it in `bestByProfile[]` whatever its score.

The suggestions shown are the `bestByProfile` entries at or above the run's threshold, sorted by score.
Because all scores are stored, changing the threshold needs no re-scoring. Rows that fail a job-level gate
keep their `scores` for inspection, but get an empty `bestByProfile` and `topScore = null`, so they never
show suggestions.

### 3.9 Calibration (part of the work)

 `scripts/job_apply_calibrate.py` takes a CSV of hand-labelled
(job, resume, verdict: apply / maybe / skip) pairs, scores them, and prints the score distribution per
verdict and the best cutoff. The 75 default and `K` are confirmed or adjusted with about 30 labelled pairs
before release.

## 4. Backend design (`engineers_backend`)

### 4.1 Models (`app/models/job_apply.py`, registered in `DOCUMENT_MODELS`)

```python
class RunSelection(BaseModel):
    accountId: PydanticObjectId
    resumeIds: List[PydanticObjectId]          # empty = all resumes of the account

class JobApplyRun(TimestampedDocument):
    userId: PydanticObjectId
    fileName: str
    status: Literal["queued", "running", "done", "failed", "cancelled"]
    selection: List[RunSelection]
    threshold: int = 75
    maxAgeDays: int = 30
    runDate: datetime                           # "today" for freshness and "Present"
    counts: RunCounts                           # total, fetched, extracted, scored, excluded, failed
    heartbeatAt: Optional[datetime] = None      # worker lease
    error: Optional[str] = None
    class Settings: name = "job_apply_runs"

class JobApplyRow(TimestampedDocument):
    runId: PydanticObjectId
    userId: PydanticObjectId
    rowIndex: int
    url: Optional[str]
    jdText: Optional[str]
    jdSource: Optional[Literal["sheet", "ats_api", "html", "browser"]]
    status: Literal["pending", "fetched", "extracted", "scored", "excluded", "fetch_failed", "llm_failed"]
    statusReason: Optional[str]
    extraction: Optional[JobExtraction]          # typed §3.2 schema
    extractionSource: Optional[Literal["llm", "rules", "cache"]]
    gates: List[GateResult]                      # job-level gates
    scores: List[ResumeScore]                    # accountId, resumeId, total, components{}, knockouts[]
    profileGates: List[ProfileGateResult]        # accountId + location/workauth results
    bestByProfile: List[ResumeScore]
    topScore: Optional[int]                      # max over bestByProfile, indexed, for the "has suggestions" filter
    applied: bool = False
    class Settings: name = "job_apply_rows"      # indexes: (runId, rowIndex), (runId, status), (runId, topScore)

class JobExtractionCache(TimestampedDocument):   # key: jdHash + promptVersion
class JobPostingCache(TimestampedDocument):      # key: normalised URL → jdText, postedDate, source; TTL 7 days
```

### 4.2 Package `app/job_apply/`

| Module | Responsibility | I/O |
|---|---|---|
| `sheet.py` | Parse `.xlsx` (openpyxl, read-only) / `.csv`. Detect the URL column (most cells match `^https?://`) and the JD column (longest average text, ≥ 200 chars). Normalise URLs (strip tracking params) and drop duplicates. Max 1,000 rows, 5 MB. | none |
| `fetch.py` | URL → JD text plus optional posted date and location. Routing: Greenhouse (`boards-api`), Lever (`api.lever.co/v0/postings`), Ashby (`posting-api`), Workable, SmartRecruiters, Workday (`/wday/cxs/...`) JSON APIs, then `httpx` plus JSON-LD `JobPosting`, then visible text (bs4), then Playwright (concurrency 2). LinkedIn and Indeed hosts return `fetch_failed: unsupported host`. Uses `JobPostingCache`. | network |
| `sections.py` | Heading-based section splitter and line-wording overrides from the draft. Used by the fallback and by relevance. | none |
| `extract.py` | Builds the prompt, validates and parses the JSON, checks the cache, and falls back to rules. | LLM |
| `skills_dict.py` | Built-in skills plus aliases for the fallback and variant matching. | none |
| `resume_parse.py` | Markdown → headline, summary, roles (`title`, `start`, `end`, `bullets`), skills, degree level. Health badge. | none |
| `scoring.py` | Pure functions for §3.3–3.6 and §3.8. `build_idf(jds)`, `score_resume(job, resume, idf, run_date)`, `pick_best(scores)`. | none |
| `gates.py` | Pure functions for §3.7. | none |
| `worker.py` | Runs the stages. Uses the lease, cancellation and resume-on-start. | DB, network, LLM |

### 4.3 Worker

1. **Fetch** rows with no `jdText` (httpx concurrency 6, Playwright 2), giving `fetched` or `fetch_failed`.
2. **Extract** with an `asyncio.Semaphore(4)` LLM pool, giving `extracted` or `llm_failed`. Job-level gates
   are applied here, and rows that fail a gate become `excluded` (they are still scored, so the breakdown
   stays inspectable).
3. **IDF** is built once, from every extracted row's substantive text.
4. **Score** every row × selected resume, apply the profile gates, and compute `bestByProfile` and
   `topScore`. Rows become `scored` or stay `excluded`.
5. The run becomes `done`.

**Rules that hold across stages:**
- **Progress:** each row is saved as soon as it changes stage, and `counts` is updated through `$inc`.
- **Cancellation:** the run status is re-read every 25 rows, and the worker stops if it is `cancelled`.
- **Lease:** `heartbeatAt` is refreshed every 30 s. On `lifespan` startup, any run that is `queued` or
  `running` with `heartbeatAt` older than 2 min is resumed. Rows already past a stage are skipped, so a
  restart redoes only the rows in flight.
- **Limits:** one active run per user, so `POST /runs` returns 409 while one is running.

### 4.4 API (`app/routers/job_applies.py`, prefix `/job-applies`, JWT)

| Method | Path | Purpose |
|---|---|---|
| POST | `/runs` | multipart: `file`, `selection` (JSON), `threshold`, `maxAgeDays`. Validates ownership, parses the sheet, inserts the run and rows, starts the worker. Returns 202 `{runId}`. Returns 400 for a bad file or column detection failure, 409 for an active run. |
| GET | `/runs` | The user's runs, newest first, with counts. |
| GET | `/runs/{id}` | Run plus counts (polled every 3 s while running). |
| GET | `/runs/{id}/rows` | Paginated. Filters: `view=all\|suggested\|excluded\|failed`, `accountId`, `minScore`. Suggestions are computed against the run threshold. |
| PATCH | `/runs/{id}` | `{threshold}`. Instant, with no re-scoring. |
| POST | `/runs/{id}/cancel` | Sets the run to cancelled. |
| POST | `/runs/{id}/retry-failed` | Resets `fetch_failed` / `llm_failed` rows to pending and restarts the worker. |
| DELETE | `/runs/{id}` | Deletes the run and its rows. |
| PATCH | `/rows/{id}` | `{applied}`. |

**New dependencies:** `openpyxl`, `rapidfuzz`.

## 5. Frontend design (`engineers_app`)

- **Routes:** `/job-applies` (runs list and new run) and `/job-applies/:runId` (results). A sidebar entry
  "Job Applies".
- **Files:** `src/pages/JobApplies.tsx`, `src/pages/JobApplyRun.tsx`, `src/components/jobApplies/*`.
  Endpoints and types go in `src/api/endpoints.ts`.
- **New run panel:**
  - file input (`.xlsx`, `.csv`)
  - profile and resume picker (`MultiSelect`), grouped by profile, with all resumes of a picked profile
    selected by default
  - threshold slider (default 75)
  - max age (default 30 days)
- **Runs list:** file name, date, status, a progress bar from `counts`, and a suggested-jobs count. SWR
  `refreshInterval: 3000` while any run is active.
- **Results table:**
  - **Job column:** title and company (linked to the URL).
  - **Posting columns:** posted (with a "date unknown" chip), work mode, location.
  - **Gate chips:** red for fail, amber for unknown or a flag.
  - **Suggestions:** chips `Profile · resume filename · score` with a band colour.
  - **Applied:** a checkbox.
- **Toolbar:** view tabs (All / Suggested / Excluded / Failed), a profile filter, and the threshold slider
  (PATCHes the run, debounced). A "Retry failed" button appears when there are failures.
- **Row expand:**
  - **Per resume:** a component breakdown (score, weight, points lost), matched and missing terms grouped by
    tier, and knockout risks.
  - **Per row:** the profile gate results, and the extraction source (LLM, rules or cache).

## 6. Error handling

- A row failure never fails the run. The row records its status and reason. The run becomes `failed` only on
  an unexpected worker exception, which is recorded in `error`. Retry resumes the run.
- LLM output that won't validate goes to the OpenAI fallback. If that also fails, the rule-based extraction
  is used. A row is marked `llm_failed` only if the rule-based extraction also throws.
- An account archived or deleted mid-run has its scores skipped and is noted on the run.

## 7. Testing

- **`tests_unit/test_job_apply_scoring.py`:**
  - the draft's worked example reproduces 58 with the old weights through a weights parameter
  - term weighting, match credit and evidence multipliers
  - years with overlapping roles, title with seniority factors, education cases, knockouts
- **`tests_unit/test_job_apply_gates.py`:**
  - remote, hybrid and onsite phrasings
  - clearance required / obtainable / preferred
  - freshness with absolute, relative and missing dates
  - region coverage table, work authorization
- **`tests_unit/test_job_apply_resume_parse.py`:** fixture resumes taken from real profile markdown shapes.
- **`tests_unit/test_job_apply_sheet.py`:** column detection, duplicates, limits.
- **`tests_unit/test_job_apply_fetch.py`:** URL routing and JSON-LD parsing on saved HTML/JSON fixtures, with
  no network.
- **`tests/test_job_applies.py`:** the API lifecycle with a fake LLM and fake fetcher: create, then poll to
  done, then filter rows, change the threshold, mark applied, cancel, retry, and the 409 on a concurrent run.
- **Frontend:** `npm run build` / typecheck, plus a manual run on a sample sheet.
