# Job Applies — screen jobs before choosing profiles

Date: 2026-10-02
Branches: `feat/job-screening` in `engineers_backend` (from `dev`) and `engineers_app` (from `main`)
Status: approved design, pending spec review
Builds on: `2026-10-01-job-applies-design.md`

## 1. Goal

Today a user picks profiles and resumes before anything is known about the jobs in the sheet. In a real
150-link run, 31 of the 114 "fetched" jobs were closed or error pages, 36 more could not be read, and most of
the rest were too old, so the profile choice was made blind.

New flow: the sheet is checked first. The user sees how many jobs are worth applying to, why the others were
dropped (closed, 404, not fetched, too old, on-site, clearance, ...), and the worthwhile jobs grouped by
location, each group with the profiles whose country or region fits already ticked. The user confirms, and
the same run goes on to score and tailor only the chosen job × profile pairs.

### Decisions already made

| Topic | Decision |
|---|---|
| Flow | A two-step run: screen, pause at a report, then score. Not a separate screening object. |
| Profile choice | Per location group, pre-filled with the profiles that fit. The user can untick or add any profile. |
| Unknowns (no date, work mode or location not stated) | Kept as worth applying to, flagged "needs a check". "Location not stated" is its own group. |
| Report layout | Layout A: headline count, a stacked bar of buckets, bucket chips, then one card per location group. |
| Max age | Set in the report, applied instantly to stored dates (no refetch, no LLM). |
| Threshold | Set at "Score & continue". |

### Out of scope

- Changing how jobs are fetched, extracted or scored (beyond limiting scoring to assigned pairs).
- Reusing one screening for several scoring runs.
- Location gates beyond today's `covers()` rules (e.g. time-zone matching for profiles).

## 2. Run lifecycle

```
POST /runs (sheet, maxAgeDays; no selection)
  queued → running [phase=screen: fetch → extract → job gates → screen bucket]
         → screened                      (waits for the user; does not block other runs)
POST /runs/{id}/start (assignments, resumes, threshold)
  queued → running [phase=score: score assigned pairs] → done
```

- `JobApplyRun.status` gains `"screened"`. `ACTIVE` stays `("queued", "running")`, so a screened run does not
  block creating another run (409 rule unchanged otherwise).
- New `JobApplyRun.phase: Literal["screen", "score"] = "screen"`. The worker runs fetch and extract when
  `phase == "screen"` and stops with `status="screened"`; it runs the score stage only when `phase == "score"`.
- Compatibility: if `POST /runs` is given `selection`, the run is created with `autoStart=True` and
  `assignments={"*": [every selected profile]}` (`*` = every group). When its screen phase ends it switches to
  `phase="score"` instead of pausing, i.e. today's behaviour with no pause.
- `resume_stale_runs()` resumes `queued`/`running` runs in either phase; `screened` runs are left alone.
- Screened runs expire with the normal 3-day retention (`expiresAt`).

## 3. Screening rules

### 3.1 Bucket (`JobApplyRow.screen`)

Set after extraction (and on fetch failure). Exactly one bucket, first match wins:

| Order | Bucket | When |
|---|---|---|
| 1 | `closed` | fetch failed with `JobClosed` ("job closed (...)") |
| 2 | `not_fetched` | any other fetch failure (blocked, timeout, unsupported host, no job text) |
| 3 | `read_failed` | `llm_failed` |
| 4 | `clearance` | clearance gate fails |
| 5 | `onsite` | remote gate fails (on-site or hybrid) |
| 6 | `too_old` | freshness gate fails |
| 7 | `check` | passed, but any gate is `unknown` or the location group is `none` |
| 8 | `valid` | passed everything |

`check` and `valid` together are "worth applying to". The row keeps its full gate list, so the detail view
shows every reason, not only the bucket.

`JobApplyRow.forceInclude: bool = False` ("Include anyway") moves a `clearance`, `onsite` or `too_old` row into
`check`. Not allowed for `closed`, `not_fetched` or `read_failed` (there is no job text to score).

### 3.2 Location group (`JobApplyRow.groupKey`)

From `extraction.allowedLocations`: each value normalised (countries via `normalize_country` to ISO-2, regions
via `region_key` to a canonical label), de-duplicated, sorted, joined with `+`. Examples: `GB`, `US+EU`,
`Worldwide`, `EMEA`. No locations gives `none`. Labels are made in the UI ("United Kingdom", "US · EU",
"Location not stated").

### 3.3 Profile fit

For a group and a profile with `country`/`region`:

- fits if any location in the group `covers()` the profile (the same function as the location gate, so the
  report and scoring agree);
- every profile fits `none`;
- a profile with neither country nor region fits every group, and the report shows "Set this profile's
  country for better suggestions".

Work authorization stays a per-job profile gate at scoring time (unchanged).

### 3.4 Max age

`PATCH /runs/{id}` accepts `maxAgeDays` while the run is `screened`. It recomputes the freshness gate from
each row's stored `postedDate`, then the row's `status` (`extracted` ↔ `excluded`) and `screen`. No fetch, no LLM.

## 4. Backend design

### 4.1 Models (`app/models/job_apply.py`)

- `RunStatus` adds `"screened"`. `RowStatus` adds `"unassigned"` (scored phase skipped it: no profile assigned).
- `JobApplyRun`: `phase`, `assignments: Dict[str, List[PydanticObjectId]] = {}` (groupKey → account ids; the
  key `*` applies to every group), `autoStart: bool = False`, `screenedAt: Optional[datetime]`. `selection`
  keeps its meaning (profile → resumes) and is set at start.
- `JobApplyRow`: `screen: Optional[ScreenBucket]`, `groupKey: Optional[str]`, `forceInclude: bool = False`.

### 4.2 Package `app/job_apply/`

New `screening.py` (pure, no I/O):

- `bucket(row) -> ScreenBucket` (rules of §3.1).
- `group_key(locations) -> str` (§3.2).
- `profile_fits(group_key, country, region) -> bool` (§3.3).
- `report(rows, profiles) -> ScreeningReport`: bucket counts, groups (key, job count, `check` count, fitting
  profile ids), sorted by job count.

`worker.py`:

- `_fetch_one`: reuse a cached posting only if `fetchedAt` is within 24 hours (`POSTING_REUSE`), so closures
  are noticed; the extraction cache (keyed on the JD hash) still avoids paying the LLM twice.
- Fetch failures and extraction set `row.screen` and `row.groupKey`.
- `run_worker`: `phase == "screen"` runs fetch + extract and sets `screenedAt`; then, if `autoStart`, it sets
  `phase="score"` and continues, otherwise it sets `status="screened"` and stops. `phase == "score"` runs
  `_score_stage` only.
- `_score_stage`: for each worthwhile row (`screen` in `valid`/`check`), the resumes considered are those of the
  profiles in `assignments[row.groupKey]` plus `assignments["*"]`. A row with none gets `status="unassigned"`.
  Rows in other buckets are not scored.

### 4.3 API (`app/routers/job_applies.py`)

| Method | Path | Change |
|---|---|---|
| POST | `/runs` | `selection` optional. Without it: `phase="screen"`. Takes `maxAgeDays`. |
| GET | `/runs/{id}/screening` | New. The `ScreeningReport` for the run, computed from its rows. |
| GET | `/runs/{id}/rows` | New filter `screen=<bucket>` for the bucket lists. |
| PATCH | `/runs/{id}` | Also `maxAgeDays` (only while `screened`). |
| PUT | `/rows/{id}/include` | New. Sets `forceInclude` (§3.1), recomputes `screen`. |
| POST | `/runs/{id}/start` | New. Body: `assignments`, `selection` (profile → resumes), `threshold`. Only from `screened`; 409 if another run is active. Sets `phase="score"`, `status="queued"`, starts the worker. |
| POST | `/runs/{id}/retry-failed` | Allowed from `screened`: resets `not_fetched`/`read_failed` rows, reruns the screen phase, pauses again. |

`ScreeningReport`:

```json
{
  "total": 150, "worth": 31, "check": 12,
  "buckets": {"valid": 19, "check": 12, "closed": 59, "not_fetched": 17, "read_failed": 0,
              "clearance": 1, "onsite": 8, "too_old": 34},
  "maxAgeDays": 30,
  "groups": [{"key": "GB", "jobs": 9, "check": 2, "fits": ["<accountId>"]}],
  "profiles": [{"id": "<accountId>", "name": "Sam", "country": "GB", "region": null, "resumes": 3}]
}
```

## 5. Frontend design (`engineers_app`)

- **New run panel**: step 1 Job sheet (preview unchanged), step 2 **Check jobs**. Profiles, filters and
  threshold leave the panel. Starting navigates to `/job-applies/{id}`.
- **Run page, screening**: progress card ("Checking 64 / 150 · 50 read · 3 closed so far") with Cancel. The
  runs list shows "Checking…", then a "Ready to review" badge when `screened`.
- **Run page, screened** (Layout A):
  - headline "31 of 150 jobs worth applying to · 12 need a check";
  - stacked bar of buckets; chips per bucket with counts; the Too old chip has the max-age selector;
  - each chip opens a list of its jobs (title, company, link, reason) with "Include anyway" where allowed;
    Not fetched has "Retry N";
  - one card per location group: label, job count, "needs a check" count, a checkbox per profile (fitting
    ones ticked, others shown with "doesn't fit");
  - footer: per-profile job totals, resume picker per profile (today's), threshold, **Score & continue**
    (disabled when nothing is worth applying to or nothing is assigned).
- **Run page, scoring/done**: today's progress and results; the results table gains the job's location group
  and assigned profiles. "Back to screening" opens the report read-only.
- Empty states: no worthwhile jobs (report says so, buckets browsable); no profiles (groups shown, link to add
  a profile).

## 6. Error handling

- Screening failures are row-level and land in buckets; the run itself fails only on unexpected errors
  (existing behaviour: `status="failed"`, retry resumes).
- `/start` on a run that is not `screened`, or with no assignments that reach a worthwhile row: 409/400 with a
  message the UI shows.
- Cancel works in both phases (existing endpoint; `screened` runs can be deleted instead).

## 7. Testing

- `tests_unit/test_job_apply_screening.py`: bucket order and `forceInclude`; `group_key` normalisation
  (names vs codes, duplicates, regions); `profile_fits` (covers, `none`, profile without country); report counts.
- `tests_unit/test_job_apply_worker.py`: screen phase stops at `screened`; score phase scores only assigned
  pairs and marks `unassigned`; 24-hour posting reuse; max-age recompute.
- `tests/test_job_applies.py` (needs `MONGODB_URI_TEST`): create without selection → screened → report →
  start → done; create with selection still runs straight through; 409 rules; retry from `screened`.
- Frontend: `npm run lint` (tsc), then a manual run of the 150-link sheet.
