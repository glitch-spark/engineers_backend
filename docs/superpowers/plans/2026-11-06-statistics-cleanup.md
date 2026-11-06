# Statistics Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Statistics gets a Bids tab and an Interviews tab instead of "Interview rate". Bidder bids come from Bid
Track records, and interviews split into self-taken and caller-taken.

**Architecture:**
- One backend report, `app/interview_rates.py` behind `GET /metrics/interview-rates`, is extended:
  - bidder bids from `Bid` records, through `app/bids/performance.day_counts`;
  - a self / caller split of the rounds held;
  - the first bid record's day.
- The frontend splits `InterviewRateTab.tsx` into `BidsTab.tsx` and `InterviewsTab.tsx`. Both read one SWR key, and
  the pieces they share live in `rateShared.tsx`.

**Tech Stack:** FastAPI + Beanie (tests: pytest + mongomock-motor); React 18 + SWR + Recharts + Tailwind.

**Spec:** `docs/superpowers/specs/2026-11-06-statistics-cleanup-design.md`

**Worktrees:**
- backend: `C:/Users/ADMINI~1/AppData/Local/Temp/2/claude/409b0cc9221f9860/d4b3fc45-0895-4b25-8af0-e23ddd50123e/scratchpad/run-backend`
- frontend: `.../scratchpad/run-app`

Both are on branch `feat/statistics-cleanup`. Python is
`"/c/Users/Administrator/Downloads/engineer - Copy/engineers_backend/.venv/Scripts/python"`.

## Global Constraints

- Tab order: **Bids · Interviews · Callers · Resume compare · AI usage**. Bids is the default. The tab label "Interview rate" is gone.
- Bidder bids:
  - **approved + pending** `Bid`s only;
  - counted to the bidder's owner (`Bidder.createdBy`), archived bidders included;
  - dated by the Bid Track day (the daily window ending 18:30 in the report timezone);
  - a region counts the bidders whose `profileId` is on an `Account` of that region; no profile, or a profile with no region, counts under `all` only.
- The typed `done.bidsBidder` is not read anywhere in `app/interview_rates.py`.
- `BIDDER_REGIONS` and the response field `bidderSplit` are removed.
- Self / caller interviews come from `split_rounds_by_day(...)`'s `is_caller`. Their sum equals `interviews`.
- `MIN_BIDS = 30`, the ranking rules, the period / region filters and the non-admin board are unchanged.
- Bids-tab note copy, shown when `start < bidsRecordsStart`: `Bidder bids are counted from Bid Track, which starts {Mon D, YYYY}.`
- Do not read or write the live database in tests. The local `.env` points at production.

## Review Focus

1. **A bid exactly at the 18:30 cutoff.** It belongs to the next day (Task 1 pins 18:29:59 vs 18:30).
2. **A bidder moved between profiles.** Its bids follow the current `profileId` region. That is accepted and
   documented in the docstring; there is no test.
3. **A region filter with only bidder bids and no typed self bids.** The user still appears on the board (Task 1:
   EU row with self 0, bidder 5).
4. **A period entirely before Oct 6.** Bidder bids are 0 and the note shows (Task 1's `bidsRecordsStart`, Task 4's
   note condition).
5. **An admin who owns bidders.** Their bids stay off the board like today, because rows are non-admin users only. A
   viewer who is an admin sees the board without a row (existing behaviour).

---

### Task 1: Bidder bids from Bid Track records

**Files:**
- Modify: `app/interview_rates.py` (`counts`, `interview_rates`, module docstring; remove `BIDDER_REGIONS`)
- Test: `tests_unit/test_interview_rates.py`

**Interfaces:**
- Produces:
  - `async def bidder_bids(user_ids: list, start: date, end: date, region: Region, regions: dict) -> dict[tuple[date, PydanticObjectId], int]`: `(Bid Track day, owner id) → approved + pending bids`.
  - `async def bid_records_start() -> Optional[date]`: the report-timezone date of the earliest `Bid.firstAt`; `None` with no bids.
  - The response gains `bidsRecordsStart: str | None` (ISO date) and loses `bidderSplit`.
- Consumes:
  - `app.bids.performance.day_counts(bidders, start, end, sched) -> {(bidder id str, day): (approved, pending, rejected)}`;
  - `app.bids.windows.schedule_from_settings()`.

- [ ] **Step 1: Write the failing tests**

Add `Bid` and `Bidder` to the fixture's `document_models`. Add helpers:
- `_bidder(owner, name, profile=None, archived=False)`;
- `_bids(bidder, when_chicago: datetime, status, n)`, which inserts `n` bids with `firstAt` = `when_chicago` converted to naive UTC, `jobKey` unique and `screenshots=[]`.

```python
async def test_bidder_bids_come_from_bid_track_records_by_profile_region(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    us = await Account(name="US", region="US", createdBy=ana.id).insert()
    eu = await Account(name="EU", region="EU", createdBy=ana.id).insert()
    b_us, b_eu, b_none = await _bidder(ana, "Bo", us), await _bidder(ana, "Cy", eu), await _bidder(ana, "Di")
    b_old = await _bidder(ana, "Ed", us, archived=True)
    await _bids(b_us, chi(10, 20, 10), "approved", 3)
    await _bids(b_us, chi(10, 20, 11), "pending", 2)
    await _bids(b_us, chi(10, 20, 12), "rejected", 4)
    await _bids(b_eu, chi(10, 21, 10), "pending", 5)
    await _bids(b_none, chi(10, 22, 10), "approved", 1)
    await _bids(b_old, chi(10, 20, 10), "approved", 2)
    await _plan(ana, date(2026, 10, 20), bidsSelfUs=10, bidsSelf=10, bidsBidder=40)   # typed bidder: ignored
    row = lambda out: (out["rows"][0]["selfBids"], out["rows"][0]["bidderBids"])
    assert row(await endpoint(ana, period="week", date="2026-10-21", region="all")) == (10, 13)
    assert row(await endpoint(ana, period="week", date="2026-10-21", region="US")) == (10, 7)
    assert row(await endpoint(ana, period="week", date="2026-10-21", region="EU")) == (0, 5)
    out = await endpoint(ana, period="week", date="2026-10-21", region="all")
    assert "bidderSplit" not in out and out["totals"]["bidderBids"] == 13


async def test_a_bid_at_the_cutoff_counts_on_the_next_day(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    b = await _bidder(ana, "Bo")
    await _bids(b, chi(10, 20, 18, 29, 59), "approved", 1)
    await _bids(b, chi(10, 20, 18, 30), "approved", 1)
    out = await endpoint(ana, period="week", date="2026-10-21", region="all")
    days = {b["label"]: b["values"].get(str(ana.id), {}).get("bidder", 0) for b in out["bidsChart"]["buckets"]}
    assert (days["Tue 20"], days["Wed 21"]) == (1, 1)


async def test_bids_records_start(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    assert (await endpoint(ana, period="week", date="2026-10-21", region="all"))["bidsRecordsStart"] is None
    await _bids(await _bidder(ana, "Bo"), datetime(2026, 10, 6, 11, 54, tzinfo=CHI), "approved", 1)
    assert (await endpoint(ana, period="week", date="2026-10-21", region="all"))["bidsRecordsStart"] == "2026-10-06"
```

Add `CHI = ZoneInfo("America/Chicago")` and `chi(month, day, hh, mm=0, ss=0)` (a 2026 `datetime` in `CHI`), as in
`tests_unit/test_bids_performance.py:26,117`.

Update the existing tests that relied on typed bidder bids. The typed `bidsBidder` now adds 0:
- `test_applies_come_from_followed_up_daily_plans_by_region`: expect `(55, 0)`, `(40, 0)` and `(0, 0)`, and drop the `bidderSplit` asserts.
- `test_a_custom_range_counts_its_days`.
- `test_bids_chart_splits_self_and_bidder_per_user_per_day_oldest_user_first`: replace `bidsBidder` with records via `_bids` where the test needs bidder values.

- [ ] **Step 2: Run the tests and see them fail**

Run: `$PY -m pytest tests_unit/test_interview_rates.py -q -p no:cacheprovider`
Expected: the new tests FAIL (`bidderSplit` is still present, bidder counts come from typed plans).

- [ ] **Step 3: Implement `bidder_bids` and `bid_records_start`, then wire them in**

- In `counts`, drop typed bidder applies: `own` only goes into `[0]`.
- Add `bidder_bids(...)` results into `out[owner][0]` and `out[owner][2]`, and into `days[(day, owner)][1]`.
- `interview_rates` returns `bidsRecordsStart` and no `bidderSplit`.
- Rewrite the docstring's bid bullet to the spec's definition.

- [ ] **Step 4: Run the tests and see them pass**

Run: `$PY -m pytest tests_unit/test_interview_rates.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/interview_rates.py tests_unit/test_interview_rates.py
git commit -m "feat(statistics): bidder bids from Bid Track records, by the bidder's profile region"
```

### Task 2: Self / caller interview split

**Files:**
- Modify: `app/interview_rates.py`
- Test: `tests_unit/test_interview_rates.py`

**Interfaces:**
- Consumes: Task 1's `counts` (per-day values become `[self bids, bidder bids, self interviews, caller interviews]`).
- Produces:
  - rows and `totals` gain `selfInterviews` and `callerInterviews`;
  - `interviewsChart` parts are `{"self": 2, "caller": 3}`;
  - `counts` returns `userId → [bids, interviews, bidder bids, caller interviews]`.

- [ ] **Step 1: Write the failing test**

```python
async def test_interviews_split_self_and_caller(db):
    # Ana, US profile; interview A: intro (self) completed Oct 20, tech (caller enabled) completed Oct 22;
    # interview B: intro (caller) canceled Oct 21 (not held)
    out = await endpoint(ana, period="week", date="2026-10-21", region="all")
    r = out["rows"][0]
    assert (r["interviews"], r["selfInterviews"], r["callerInterviews"]) == (2, 1, 1)
    assert (out["totals"]["selfInterviews"], out["totals"]["callerInterviews"]) == (1, 1)
    days = {b["label"]: b["values"][str(ana.id)] for b in out["interviewsChart"]["buckets"]}
    assert days["Tue 20"] == {"self": 1, "caller": 0} and days["Thu 22"] == {"self": 0, "caller": 1}
```

`_interview` gains an optional caller flag per round: `("tech", when, "completed", True)` sets
`caller={"enabled": True}` on that history entry.

Update `test_interviews_chart_counts_rounds_held_per_user_per_day`, which read the part `interviews`. The parts are
now `self` and `caller`.

- [ ] **Step 2: Run it and see it fail**

Run: `$PY -m pytest tests_unit/test_interview_rates.py -q -p no:cacheprovider`
Expected: FAIL with `KeyError: 'selfInterviews'`.

- [ ] **Step 3: Implement**

`counts` counts rounds with `split_rounds_by_day` in both places, so the totals and the per-day values agree. It
replaces `count_interview_rounds` there.

- [ ] **Step 4: Run the full file and the router's other users**

Run: `$PY -m pytest tests_unit/test_interview_rates.py tests_unit/test_caller_stats.py tests_unit/test_profile_metrics.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/interview_rates.py tests_unit/test_interview_rates.py
git commit -m "feat(statistics): interviews split into self-taken and caller-taken rounds"
```

### Task 3: Frontend types and shared pieces

**Files:**
- Modify: `src/api/endpoints.ts` (`InterviewRateRow`, `InterviewRates`, the `PerUserChart` doc comment)
- Create: `src/components/statistics/rateShared.tsx`
- Modify: `src/components/statistics/InterviewRateTab.tsx` (imports from `rateShared`; still compiles, removed in Task 4)

**Interfaces:**
- Produces, from `rateShared.tsx`:
  - `useInterviewRates(filters: StatsFilters): { data?: InterviewRates; error: unknown; isLoading: boolean }`, with SWR key `['interview-rates', period, date, to, region]` and `keepPreviousData`;
  - `StatsBody({ filters, onFilters, children }: { …; children: (data: InterviewRates) => ReactNode })`: the FilterBar, loading and error states;
  - `PerUserBars` (current props), `initialsFor`, `userHue`, `pct`;
  - `type Part = { key: string; label: string; faded?: boolean }`.
- Types:
  - `InterviewRateRow` gains `selfInterviews: number; callerInterviews: number`;
  - `InterviewRates.totals` gains the same;
  - `InterviewRates` gains `bidsRecordsStart: string | null` and loses `bidderSplit`.

- [ ] **Step 1: Move the shared code into `rateShared.tsx` and update the types**

Moving `PerUserBars`, `PerUserTip`, `initialsFor`, `userHue`, `seriesColors`, `USER_HUES`, `FADED_OPACITY`, `pct`
and `niceCeil` doesn't change their behaviour. `InterviewRateTab` replaces `data.bidderSplit` with `true`.

- [ ] **Step 2: Verify**

Run: `npm run lint` in `run-app`.
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add src/api/endpoints.ts src/components/statistics/rateShared.tsx src/components/statistics/InterviewRateTab.tsx
git commit -m "refactor(statistics): shared rate pieces and the new report fields"
```

### Task 4: Bids and Interviews tabs

**Files:**
- Create: `src/components/statistics/BidsTab.tsx`, `src/components/statistics/InterviewsTab.tsx`
- Modify: `src/pages/Statistics.tsx`
- Delete: `src/components/statistics/InterviewRateTab.tsx`

**Interfaces:**
- Consumes: Task 3's `useInterviewRates`, `StatsBody`, `PerUserBars`, `pct`.
- Produces: `BidsTab({ filters, onFilters })` and `InterviewsTab({ filters, onFilters })`, with the same props as the old tab.

- [ ] **Step 1: `BidsTab`**

1. Four `stat-card` tiles: Bids (`totals.bids`), Self, Bidder, and Bidder share (`bidderBids / bids`, `pct`).
2. `PerUserBars` titled "Bids per user", with parts `[{ key: 'self', label: 'self' }, { key: 'bidder', label: 'bidder', faded: true }]`.
3. The note (Global Constraints copy) when `bidsRecordsStart && start < bidsRecordsStart`.
4. A sortable table with columns User, Self, Bidder, Total, Bidder share. Rows sort by total, descending by default;
   "You" is highlighted as in the old table; there is a team total row.

- [ ] **Step 2: `InterviewsTab`**

1. Tiles: Interviews, Self-taken, Caller-taken, and Interviews per bid (`totals.rate`, `pct`).
2. `PerUserBars` titled "Interviews per user", with parts `[{ key: 'self', label: 'self' }, { key: 'caller', label: 'caller', faded: true }]`.
3. The old `RateScatter`, moved here unchanged.
4. The old `RateTable`, plus Self and Caller columns after Interviews. Its Bids column header becomes `Bids` and shows
   `bids`. The region note from the old tab moves to the top of this tab and loses its bidder sentence.

- [ ] **Step 3: `Statistics.tsx`**

Tabs: `bids` (default, `BidsTab`), `interviews` (`InterviewsTab`), `callers`, `resume-compare`, `ai-usage`, in that
order. Delete `InterviewRateTab.tsx`.

- [ ] **Step 4: Verify**

Run: `npm run lint && npm run build` in `run-app`.
Expected: no type errors, the build succeeds, and `grep -rn "InterviewRateTab\|bidderSplit" src` prints nothing.

- [ ] **Step 5: Commit**

```bash
git add -A src
git commit -m "feat(statistics): Bids and Interviews tabs replace Interview rate"
```

### Task 5: Verification against live data (read-only)

- [ ] **Step 1: Run the full backend unit suite**

Run: `$PY -m pytest tests_unit -q -p no:cacheprovider --ignore=tests_unit/test_resume_experience_roles.py --ignore=tests_unit/test_style_serializer.py --ignore=tests_unit/test_template_annotate_removals.py`
Expected: only the 2 known `.env` failures (`test_upload_url_without_storage_is_503`, `test_apply_prompts_rejects_bad_lists[prompts5-new-1]`).

- [ ] **Step 2: Compare with Bidders → Performance on live data, read-only**

Write a scratchpad script. It uses `init_beanie(..., skip_indexes=True)` and does no writes. For October 2026 and
region `all`, it calls:
- `interview_rates(viewer=<any admin>, "month", date(2026,10,1), "all")`;
- `performance(<all bidders of non-admin owners, archived included>, "month", date(2026,10,1), date(2026,10,31), …)`.

Expected:
- the team `bidderBids` equals the Performance `totals.total` for the same bidders;
- every row's `selfInterviews + callerInterviews == interviews`.

Report any difference with its cause; don't hide it.
