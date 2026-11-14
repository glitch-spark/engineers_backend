# Profiles Applied-by Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the Profiles list:
- a date-range filter like the Interviews page's;
- interviews split by who applied (self / bidder / not set);
- separate self and bidder interview-per-bid rates, counted from Nov 6;
- each profile's first and last apply date.

**Architecture:** `GET /accounts/metrics` gains `from` / `to` (blank = All time), per-row applied-by counts, `selfRate`
/ `bidderRate` over the window from Nov 6, and `firstApply` / `lastApply`. The rates take one extra pass with the
existing `collect_range`. The apply dates come from a new `apply_dates` over Bid Track bids and per-profile self bids.
The frontend's Period select becomes a Date range select built on the Interviews page's `listDateRange`.

**Tech Stack:** FastAPI + Beanie (pytest + mongomock-motor); React 18 + SWR + Tailwind.

**Spec:** `docs/superpowers/specs/2026-11-13-profiles-applied-by-design.md`

**Worktrees:** backend `.../scratchpad/run-backend`, frontend `.../scratchpad/run-app`, both on
`feat/profiles-applied-by`. `$PY` = `"/c/Users/Administrator/Downloads/engineer - Copy/engineers_backend/.venv/Scripts/python"`.

## Global Constraints

- `APPLIED_BY_SINCE = date(2026, 11, 6)`. The rate window is [max(range start, APPLIED_BY_SINCE), range end]. When it is
  empty, `ratesSince` is null and both rates are null.
- **Self rate** = self-applied interviews ÷ self bids; **bidder rate** = bidder-applied interviews ÷ bidder bids.
  - Both use the window only. "Not set" interviews count toward neither.
  - `rate` is rounded to 4 places, and is null at 0 bids. `lowVolume` = bids < `MIN_BIDS` (30).
- **Interviews** are rounds held (`split_rounds_by_day`). A round's applied-by is its interview's `appliedBy.kind`: self,
  bidder, or not set when missing.
- **Apply dates:** only two sources, both lifetime (not tied to the range):
  - Bid Track bids, approved + pending, on the Bid Track day of `firstAt`, for the profile's bidders, archived ones
    included;
  - logged daily plans with `done.bidsSelfByProfile[profile] > 0`.
- Range validation (400):
  - only one of from / to given;
  - an invalid date;
  - from > to;
  - more than 366 days.
- `weeks=4|8|12|all` keeps working. `from` / `to` win when both are given.
- **Frontend:**
  - Date range options, in this order: All time (default), This week, Previous week, This month, Previous month,
    This year, Custom. No Next week.
  - Weeks are Monday–Sunday and months are calendar months (the Interviews list's `listDateRange`).
  - The choice is remembered in localStorage, wrapped in try/catch.
- Columns: Name · Region · Applies · Bids · Interviews · Self intv/bid · Bidder intv/bid · Resume score.
- No test or check writes to the live database.

## Review Focus

1. **A self bid and a self-applied round on Nov 5 vs Nov 6.** Only the Nov 6 ones count in the rate (Task 3 test).
2. **A period that ends before Nov 6:** the rates show `—`, while the interview split still shows (Task 3 test,
   Task 4 render).
3. **A bid at 19:00 Chicago:** its apply date is the next day (Task 2 test).
4. **An old tab sending `weeks=4` after the deploy:** it still works (Task 3 keeps the existing tests).
5. **A custom range with From after To:** the frontend shows an inline error and doesn't request (Task 4). The
   backend returns 400 (Task 3 test).

---

### Task 1: Interview rounds split by who applied

**Files:**
- Modify: `app/profile_metrics.py`: `interview_rounds`, `_COUNTS`, `_days`, `summarize`, `lifetime`
- Test: `tests_unit/test_profile_metrics_applied.py` (create; same fixture as `test_profile_metrics_range.py`)

**Interfaces:**
- Produces:
  - `interview_rounds(account_ids, start, end) -> {(account id, day): [self, caller, selfApplied, bidderApplied, unsetApplied]}`;
  - `_COUNTS` gains `"selfAppliedInterviews"`, `"bidderAppliedInterviews"` and `"unsetAppliedInterviews"`;
  - `summarize(...)` rows gain those three totals;
  - `lifetime(...)` totals carry them too.

- [ ] **Step 1: Write the failing test**

```python
async def test_rounds_split_by_who_applied(db):
    ana, a = await _owner_profile()                       # helper: UserExtra + Account(region="US")
    await _iv(ana, a, applied={"kind": "self"}, rounds=[at(11, 5), at(11, 7)])
    await _iv(ana, a, applied={"kind": "bidder", "bidderName": "Bo"}, rounds=[at(11, 7)], caller=True)
    await _iv(ana, a, applied=None, rounds=[at(11, 9)])
    out = await collect_range([a], date(2026, 11, 2), date(2026, 11, 9))
    row = summarize(str(a.id), *out[str(a.id)])
    assert (row["interviews"], row["selfAppliedInterviews"], row["bidderAppliedInterviews"], row["unsetAppliedInterviews"]) == (4, 2, 1, 1)
    assert (row["selfInterviews"], row["callerInterviews"]) == (3, 1)        # who took them, unchanged
```

(`at(m, d)` = `datetime(2026, m, d, 15)`. `_iv` inserts an Interview with one completed intro round per date;
the last round has caller enabled when `caller=True`.)

- [ ] **Step 2: Run it and see it fail**

Run: `$PY -m pytest tests_unit/test_profile_metrics_applied.py -q -p no:cacheprovider`
Expected: FAIL with `KeyError: 'selfAppliedInterviews'`.

- [ ] **Step 3: Implement.** `interview_rounds` reads `iv.appliedBy`. `_days` and `lifetime` fill the three new
  counts. `summarize` totals them.

- [ ] **Step 4: Run it and the existing profile-metrics tests**

Run: `$PY -m pytest tests_unit/test_profile_metrics_applied.py tests_unit/test_profile_metrics.py tests_unit/test_profile_metrics_range.py tests_unit/test_profile_metrics_router.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**: `feat(profiles): interview rounds split by who applied`

### Task 2: First and last apply dates

**Files:**
- Modify: `app/profile_metrics.py` (add `apply_dates`)
- Test: `tests_unit/test_profile_metrics_applied.py`

**Interfaces:**
- Produces: `async def apply_dates(accounts: list[Account | ProfileSpan]) -> dict[str, tuple[Optional[date], Optional[date]]]`
  (account id → (first, last)). It runs one aggregation over `bids` (min / max `firstAt` per bidder, approved +
  pending) and one scan of logged daily plans for the owners. Bid days come from `bid_report_day`.

- [ ] **Step 1: Write the failing test**

```python
async def test_apply_dates_from_bids_and_self_bids(db):
    ana, a = await _owner_profile()
    b = await Account(name="B", createdBy=ana.id, region="US").insert()
    c = await Account(name="C", createdBy=ana.id, region="US").insert()
    old = await _bidder(ana, a, archived=True)
    await _bid(old, chi(10, 10, 10), "approved")
    await _bid(old, chi(11, 2, 19), "pending")          # after the 18:30 cutoff: Nov 3
    await _bid(old, chi(10, 1, 10), "rejected")         # not an apply
    await _plan(ana, date(2026, 10, 30), {str(a.id): 3})
    await _plan(ana, date(2026, 11, 9), {str(a.id): 5}, logged=False)   # not followed up: no
    await _plan(ana, date(2026, 11, 8), {str(b.id): 2})
    out = await apply_dates([a, b, c])
    assert out[str(a.id)] == (date(2026, 10, 10), date(2026, 11, 3))
    assert out[str(b.id)] == (date(2026, 11, 8), date(2026, 11, 8))
    assert out[str(c.id)] == (None, None)
```

- [ ] **Step 2: Run it and see it fail** (`ImportError: apply_dates`).
- [ ] **Step 3: Implement `apply_dates`.**
- [ ] **Step 4: Run Task 1's command.** Expected: all pass.
- [ ] **Step 5: Commit**: `feat(profiles): first and last apply dates from bids and self bids`

### Task 3: List endpoint: date range, per-source rates, apply dates

**Files:**
- Modify: `app/routers/accounts.py` (`list_profile_metrics`); `app/profile_metrics.py` (add `APPLIED_BY_SINCE`,
  `source_rates`)
- Test: `tests_unit/test_profile_metrics_router.py`

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces:
  - `list_profile_metrics(user, weeks="4", userId="", from_="", to="")`, where `from_` / `to` are
    `Annotated[str, Query(alias="from")] = ""` and `Annotated[str, Query()] = ""`, so direct calls work without them;
  - response `{start, end, weeks, rows, ratesSince, selfBidsSince?}`. With from / to, `weeks` is `null`.
  - each row gains `selfRate`, `bidderRate` (`{interviews, bids, rate, lowVolume}` or null) and `firstApply` /
    `lastApply` (ISO or null).
  - `def source_rates(counts: dict) -> tuple[dict, dict]` turns summed window counts into `(selfRate, bidderRate)`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_list_date_range_and_rates_from_nov_6(db, today):     # today fixture: router._today -> Nov 13
    ana, a = await _owner_profile()
    await _plan(ana, date(2026, 11, 5), {str(a.id): 5}); await _plan(ana, date(2026, 11, 7), {str(a.id): 10})
    await _iv(ana, a, applied={"kind": "self"}, rounds=[at(11, 5)]); await _iv(ana, a, applied={"kind": "self"}, rounds=[at(11, 7)])
    bo = await _bidder(ana, a); await _bids(bo, chi(11, 7, 10), "approved", 20)
    await _iv(ana, a, applied={"kind": "bidder", "bidderName": "Bo"}, rounds=[at(11, 7)])
    await _iv(ana, a, applied=None, rounds=[at(11, 7)])
    out = await list_profile_metrics(ana, from_="2026-11-02", to="2026-11-08", userId="")
    row = out["rows"][0]
    assert (out["start"], out["end"], out["ratesSince"]) == ("2026-11-02", "2026-11-08", "2026-11-06")
    assert (row["interviews"], row["selfAppliedInterviews"], row["bidderAppliedInterviews"], row["unsetAppliedInterviews"]) == (4, 2, 1, 1)
    assert row["selfRate"] == {"interviews": 1, "bids": 10, "rate": 0.1, "lowVolume": True}
    assert row["bidderRate"] == {"interviews": 1, "bids": 20, "rate": 0.05, "lowVolume": True}
    assert (row["firstApply"], row["lastApply"]) == ("2026-11-05", "2026-11-07")


async def test_list_range_before_nov_6_has_no_rates(db, today):
    ana, a = await _owner_profile()
    out = await list_profile_metrics(ana, from_="2026-10-01", to="2026-10-31", userId="")
    assert out["ratesSince"] is None and out["rows"][0]["selfRate"] is None and out["rows"][0]["bidderRate"] is None


async def test_list_all_time_rates_since_nov_6(db, today):
    ana, a = await _owner_profile()
    out = await list_profile_metrics(ana, weeks="all", userId="")
    assert out["ratesSince"] == "2026-11-06" and "selfRate" in out["rows"][0]


async def test_list_bad_ranges(db, today):
    ana, _ = await _owner_profile()
    for f, t in [("2026-11-02", ""), ("", "2026-11-02"), ("2026-11-09", "2026-11-02"), ("nope", "2026-11-02"),
                 ("2025-01-01", "2026-11-02")]:
        with pytest.raises(HTTPException) as e:
            await list_profile_metrics(ana, from_=f, to=t, userId="")
        assert e.value.status_code == 400, (f, t)
```

Existing `weeks=4` / `weeks="all"` tests stay as they are, and must keep passing.

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
  - **Range path:** `collect_range` over [from, to].
  - **All time:** `lifetime` (end = today).
  - **Weeks:** `collect` (end = the span's last day).
  - **Rates:** one extra `collect_range` over [max(start, APPLIED_BY_SINCE), end] when that window isn't empty.
  - **Apply dates:** `apply_dates` for every row.
- [ ] **Step 4: Run Task 1's command.** Expected: all pass.
- [ ] **Step 5: Commit**: `feat(profiles): list metrics by date range, self and bidder rates from Nov 6, apply dates`

### Task 4: Profiles list: Date range filter and the new columns

**Files:**
- Modify: `src/lib/interviewFilters.ts` (`listDateRange` takes `Pick<InterviewFilters, 'range' | 'from' | 'to'>`)
- Modify: `src/lib/profileMetrics.ts`:
  - replace `WEEK_OPTIONS` / `ListPeriod` / `readListPeriod` / `writeListPeriod` with `PROFILE_RANGES`,
    `ProfileRange`, `readProfileRange` and `writeProfileRange`;
  - add `profileRangeDates(r: ProfileRange, now: Date): { from: string; to: string } | null` (null = All time).
- Modify: `src/api/endpoints.ts`:
  - `getProfileMetrics(params: { from?: string; to?: string; userId?: string })`;
  - `ProfileMetricsRow` gains the Task 1–3 fields; the response gains `ratesSince`.
- Modify: `src/pages/Accounts.tsx`

**Interfaces:**
- `type ProfileRange = { preset: 'all' | 'this_week' | 'prev_week' | 'this_month' | 'prev_month' | 'this_year' | 'custom'; from: string; to: string }`.
  The localStorage key is `profiles.range`, and the default is `{ preset: 'all', from: '', to: '' }`.
- SWR key: `['account-metrics', from ?? 'all', to ?? '', userId]`.

- [ ] **Step 1: Filter.**
  - The Period select becomes a "Date range" select with the options and order from Global Constraints.
  - Custom shows From / To date inputs. From > To shows `From must be on or before To` inline and doesn't request.
- [ ] **Step 2: Columns.**
  - **Applies** (after Region): the first date (`Oct 10, 2026`), with `last: N days ago` (or `last: today`) under it.
    `—` with no applies. Exact dates in the tooltip. Sorts by `lastApply`.
  - **Interviews:** the total, with `N self · N bidder · N not set` under it. The tooltip holds
    `N self-taken · N caller-taken` and the per-week average.
  - **Self intv/bid** and **Bidder intv/bid:** the rate via `fmtRate`, faded when `lowVolume`.
    - Tooltip: `Since Nov 6: N interviews ÷ N bids`.
    - `—` when null, with the tooltip `"Applied by" started Nov 6` when `ratesSince` is null, else `No bids since Nov 6`.
  - Sort keys: `'lastApply' | 'bids' | 'interviews' | 'selfRate' | 'bidderRate' | 'score'`.
  - The "self bids per profile since …" note keeps showing on Bids under All time.
- [ ] **Step 3: Verify.** `npm run lint && npm run build` pass. Grep shows no `WEEK_OPTIONS` or `readListPeriod` left.
- [ ] **Step 4: Commit**: `feat(profiles): date range filter, interviews by who applied, self and bidder rates, apply dates`

### Task 5: Verification

- [ ] Full backend unit suite. Expected: only the 2 known `.env` failures.
- [ ] Read-only live check, on one profile with tagged interviews since Nov 6:
  - the list endpoint's `selfRate` / `bidderRate` / applied split / apply dates for All time and for this week;
  - the same numbers counted directly from `interviews`, `bids` and `dailyplans`;
  - print both sides.
- [ ] Restart the local backend on this branch (Slack off) **after the last backend commit**, then check that the
  list loads at http://localhost:3001/accounts.
