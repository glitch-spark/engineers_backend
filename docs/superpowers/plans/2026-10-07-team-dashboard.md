# Team Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admins default to an "All users" team overview (totals + sortable per-user table), and goal attainment and plan logging join bids, interviews and earnings in that table and in everyone's "You vs team" card.

**Architecture:** Pure period/goal/logging math in `app/dashboard_stats.py`; one shared `team_metrics()` in `app/routers/dashboard.py` feeds both `GET /dashboard/compare` (extended) and the new admin-only `GET /dashboard/team`, so every number agrees. Frontend adds `TeamOverview.tsx`, extends `CompareCard.tsx`, and makes "All users" the admin default in `Dashboard.tsx`.

**Tech Stack:** FastAPI, Beanie (mongomock-motor in tests), pytest; React + TypeScript, SWR, Vite.

**Spec:** `docs/superpowers/specs/2026-10-07-team-dashboard-design.md` (metric definitions in §1 are binding).

## Global Constraints

- Repos `engineers_backend` and `engineers_app`, branch `feat/cleanup`. Do not push. Commit only your own files (`git add <paths>`, never `-A`).
- Python `.venv/Scripts/python.exe`. Unit suite: `.venv/Scripts/python.exe -m pytest tests_unit --continue-on-collection-errors -q -p no:cacheprovider`. Baseline outside this work: `test_bidder_ext.py::test_upload_url_without_storage_is_503` fails and 3 files fail to collect; nothing else may fail.
- Never touch the live database or Slack; tests use mongomock and fake `interviews_by_day` (pattern: `tests_unit/test_dashboard_compare.py`).
- Working days are Mon–Sat. Periods come from `compare_window(today, range)` with `range ∈ {"week","month","30d"}`; bad range → 422.
- Goal: per overlapping Mon–Sun week, `WeeklyPlan.goal` × (working days of that week inside the period ÷ 6), summed, rounded; 0 → `None` ("—"). Bids goal = `bidsSelf + bidsBidder`; interviews goal = `interviewsSelf + interviewsCaller`. Goal % = `round(done / goal * 100)`.
- Logging: `(logged, workingDays)` over working days in `[start, min(end, today)]`; today counts only if logged; `workingDays == 0` → value `None`.
- `None` ranks and sorts after every number.
- Users = active non-admins (`role != "admin"`, `isActive != False`); names via `_display_name` (anonymous stays anonymous).
- Frontend checks: `npx tsc -b` (only the 2 known `InterviewPrepLibrary.tsx` errors) and `npx vite build --outDir <scratchpad>`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- Today is Sunday: no working day is added for Sunday, so logging is over Mon–Sat only — test in Task 1.
- A period that extends past today ("This week" on Wednesday): Thu–Sat are not counted as missed — test in Task 1.
- A user who logged a plan with 0 bids still counts as logged that day — test in Task 2.
- Team goal % ignores users with no goal rather than treating them as 0% — test in Task 3.
- A user with net-negative earnings ranks below a user with 0 — test in Task 2.

---

### Task 1: Period, goal and logging helpers

**Files:**
- Modify: `app/dashboard_stats.py`
- Test: `tests_unit/test_dashboard_stats.py`

**Interfaces:**
- Produces: `working_days(start: date, end: date) -> list[date]`; `prorated_goal(week_goals: dict[date, int], start: date, end: date) -> int | None` (keys are Mondays); `percent(done: float, goal: int | None) -> int | None`; `logging_ratio(logged_days: set[date], start: date, end: date, today: date) -> tuple[int, int]`; `rank_rows(rows)` accepting `None` values (ranked after all numbers, sharing one rank).

- [ ] **Step 1: Write the failing tests**

```python
def test_working_days_skip_sunday():
    assert working_days(date(2026, 10, 5), date(2026, 10, 11)) == [date(2026, 10, d) for d in range(5, 11)]

def test_prorated_goal_full_partial_and_none():
    assert prorated_goal({date(2026, 10, 5): 300}, date(2026, 10, 5), date(2026, 10, 11)) == 300
    # 30d ending Wed Oct 7 2026: Sep 8 (Tue) .. Oct 7 — weeks of Sep 7 (5 working days in range), Sep 14, 21, 28 (6 each), Oct 5 (3)
    goals = {date(2026, 9, 7): 300, date(2026, 9, 14): 300, date(2026, 9, 21): 300, date(2026, 9, 28): 300, date(2026, 10, 5): 300}
    assert prorated_goal(goals, date(2026, 9, 8), date(2026, 10, 7)) == 250 + 900 + 150
    assert prorated_goal({}, date(2026, 10, 5), date(2026, 10, 11)) is None
    assert prorated_goal({date(2026, 10, 5): 0}, date(2026, 10, 5), date(2026, 10, 11)) is None

def test_percent():
    assert percent(240, 300) == 80 and percent(5, None) is None and percent(0, 10) == 0

def test_logging_ratio_today_counts_only_when_logged():
    mon, wed = date(2026, 10, 5), date(2026, 10, 7)
    assert logging_ratio({mon, date(2026, 10, 6)}, mon, date(2026, 10, 11), wed) == (2, 2)
    assert logging_ratio({mon, wed}, mon, date(2026, 10, 11), wed) == (2, 3)

def test_logging_ratio_sunday_and_past_period():
    sun = date(2026, 10, 11)
    assert logging_ratio(set(), date(2026, 10, 5), sun, sun) == (0, 6)
    assert logging_ratio({date(2026, 9, 1)}, date(2026, 9, 1), date(2026, 9, 30), date(2026, 10, 7)) == (1, 26)

def test_rank_rows_none_ranks_last_together():
    rows = rank_rows([("a", "A", None), ("b", "B", 0), ("c", "C", None), ("d", "D", 5)])
    assert [(r["id"], r["rank"]) for r in rows] == [("d", 1), ("b", 2), ("a", 3), ("c", 3)]
```

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests_unit/test_dashboard_stats.py -q -p no:cacheprovider` — Expected: FAIL (ImportError / None handling).
- [ ] **Step 3: Implement** the four helpers and `None` handling in `rank_rows` in `app/dashboard_stats.py`. `prorated_goal` counts each Monday's working days inside `[start, end]`, multiplies by goal/6, sums, rounds, `None` if the sum is 0.
- [ ] **Step 4: Run** the same command — Expected: PASS; then the full unit suite — only baseline failures.
- [ ] **Step 5: Commit** `feat(dashboard): goal pro-rating and plan-logging helpers`.

### Task 2: Shared team metrics + extended `/dashboard/compare`

**Files:**
- Modify: `app/routers/dashboard.py`
- Test: `tests_unit/test_dashboard_compare.py`

**Interfaces:**
- Consumes: Task 1 helpers; existing `_logged_plans`, `interviews_by_day`, `weekly_plans_for`, `streak`, `_display_name`, `compare_window`, `_today`, `_subject_id`.
- Produces: `async def team_metrics(members: list[UserExtra], start: date, end: date, today: date) -> dict[PydanticObjectId, dict]` with keys `bids, bidsSelf, bidsBidder, interviews, earnings, goalBids, goalBidsPct, goalInterviews, goalInterviewsPct, logged, workingDays, streak` (`goalBids`/`goalInterviews` are the pro-rated goals or `None`). `async def _active_members() -> list[UserExtra]`. `/dashboard/compare` metrics gain `goalBids`, `goalInterviews` (row `value` = percent or `None`) and `logged` (row `value` = `round(logged / workingDays, 4)` or `None`, rows also carry `logged`, `workingDays`).

- [ ] **Step 1: Write the failing tests** (extend the existing file; add a `weekly(user, monday, bids, interviews)` fixture helper that inserts a `WeeklyPlan` and add `WeeklyPlan` to `init_beanie`):

```python
async def test_compare_has_goal_and_logging_metrics(db):
    pedro, maria = await make_user("Pedro"), await make_user("Maria")
    await weekly(pedro, date(2026, 10, 5), bids=60, interviews=4)
    await plan(pedro, 5, 20, 10); await plan(pedro, 6, 0, 0)       # 30 bids, 2 logged days (0-bid day still logged)
    body = (await get(pedro, interviews={pedro.id: 2})).json()
    gb = {x["name"]: x for x in body["metrics"]["goalBids"]["rows"]}
    assert gb["Pedro"]["value"] == 50 and gb["Maria"]["value"] is None and gb["Maria"]["rank"] == 2
    assert {x["name"]: x["value"] for x in body["metrics"]["goalInterviews"]["rows"]}["Pedro"] == 50
    lg = {x["name"]: x for x in body["metrics"]["logged"]["rows"]}
    assert (lg["Pedro"]["logged"], lg["Pedro"]["workingDays"], lg["Pedro"]["value"]) == (2, 2, 1.0)
    assert lg["Maria"]["value"] == 0.0 and lg["Maria"]["workingDays"] == 2

async def test_negative_earnings_rank_below_zero(db):
    pedro, maria = await make_user("Pedro"), await make_user("Maria")
    await tx(pedro, 6, -50.0)
    rows = (await get(pedro)).json()["metrics"]["earnings"]["rows"]
    assert [(x["name"], x["value"]) for x in rows] == [("Maria", 0), ("Pedro", -50.0)]
```

(TODAY stays `2026-10-07`, a Wednesday; in the first test today is not logged, so working days are Mon + Tue.)

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests_unit/test_dashboard_compare.py -q -p no:cacheprovider` — Expected: FAIL (KeyError 'goalBids').
- [ ] **Step 3: Implement** `_active_members`, `team_metrics` (weekly plans fetched once per overlapping Monday via `weekly_plans_for(ids, monday)`; logged days from `_logged_plans(ids, start, min(end, today))`; streak from `_logged_plans(ids, today - 400 days, today)` grouped by user) and refactor `dashboard_compare` to build all six metrics from it.
- [ ] **Step 4: Run** the file — Expected: PASS (old and new tests); full unit suite — only baseline failures.
- [ ] **Step 5: Commit** `feat(dashboard): goal attainment and plan logging in You vs team`.

### Task 3: `GET /dashboard/team` (admins only)

**Files:**
- Modify: `app/routers/dashboard.py`
- Test: `tests_unit/test_dashboard_team.py` (new; same fixture pattern as `test_dashboard_compare.py`)

**Interfaces:**
- Consumes: Task 2 `team_metrics`, `_active_members`.
- Produces: `GET /dashboard/team?range=&today=` → spec §2 JSON (`range, from, to, totals{bids, bidsSelf, bidsBidder, interviews, earnings, goalBidsPct, goalInterviewsPct, logged, workingDays}, users[{userId, name, image, bids, bidsSelf, bidsBidder, interviews, earnings, goalBidsPct, goalInterviewsPct, logged, workingDays, streak}]`), users ordered by bids desc then name.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_team_is_admin_only(db):
    pedro = await make_user("Pedro")
    assert (await get_team(pedro)).status_code == 403

async def test_team_rows_and_totals(db):
    root = await make_user("Root", "admin")
    pedro, maria, ana = await make_user("Pedro"), await make_user("Maria"), await make_user("Ana")
    await make_user("Gone", isActive=False)
    await weekly(pedro, date(2026, 10, 5), bids=100, interviews=0)
    await weekly(maria, date(2026, 10, 5), bids=100, interviews=0)
    await plan(pedro, 5, 30, 10); await plan(maria, 6, 20, 0)
    await tx(maria, 6, 200.0)
    body = (await get_team(root, interviews={ana.id: 1})).json()
    assert [u["name"] for u in body["users"]] == ["Pedro", "Maria", "Ana"]
    p = body["users"][0]
    assert (p["bids"], p["bidsSelf"], p["bidsBidder"], p["goalBidsPct"], p["goalInterviewsPct"]) == (40, 30, 10, 40, None)
    assert (p["logged"], p["workingDays"]) == (1, 2) and "streak" in p
    t = body["totals"]
    assert (t["bids"], t["interviews"], t["earnings"]) == (60, 1, 200.0)
    assert t["goalBidsPct"] == 30            # 60 done / 200 goal; Ana (no goal) ignored
    assert t["goalInterviewsPct"] is None    # nobody has an interviews goal
    assert (t["logged"], t["workingDays"]) == (2, 6)

async def test_team_bad_range_is_422(db):
    root = await make_user("Root", "admin")
    assert (await get_team(root, range="year")).status_code == 422
```

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests_unit/test_dashboard_team.py -q -p no:cacheprovider` — Expected: FAIL (404).
- [ ] **Step 3: Implement** the route: admin check (403), `compare_window` (422), `team_metrics`, totals (team goal % = sum done ÷ sum goal over users whose goal is not `None`, `None` when none).
- [ ] **Step 4: Run** the file — Expected: PASS; full unit suite — only baseline failures.
- [ ] **Step 5: Commit** `feat(dashboard): admin team endpoint`.

### Task 4: Frontend — six-board "You vs team"

**Files:**
- Modify: `engineers_app/src/api/endpoints.ts`, `engineers_app/src/components/dashboard/CompareCard.tsx`

**Interfaces:**
- Consumes: Task 2 response.
- Produces: `DashboardCompare['metrics']` with `goalBids`, `goalInterviews`, `logged`; compare row type gains optional `logged?: number; workingDays?: number`, `value: number | null`; `DashboardTeam` type and `getDashboardTeam(params: { range: DashboardCompareRange; today?: string })` (used by Task 5).

- [ ] **Step 1: Implement** the types and six boards in order Bids, Interviews, Earnings, Bids goal %, Interviews goal %, Days logged (3-column grid on `md`, stacked below). Goal boards format `"92%"` / `"—"`; logging board `"5 / 6"` / `"—"`.
- [ ] **Step 2: Run** `npx tsc -b` and `npx vite build --outDir <scratchpad>/fe-build` — Expected: only the 2 known errors; build succeeds.
- [ ] **Step 3: Commit** (frontend) `feat(dashboard): goal and logging boards in You vs team`.

### Task 5: Frontend — admin "All users" team overview

**Files:**
- Create: `engineers_app/src/components/dashboard/TeamOverview.tsx`
- Modify: `engineers_app/src/pages/Dashboard.tsx`

**Interfaces:**
- Consumes: Task 4 `getDashboardTeam`, `DashboardTeam`, `DashboardCompareRange`; existing `Segmented`, `NameWithAvatar`.
- Produces: `TeamOverview({ onPick }: { onPick: (userId: string) => void })`.

- [ ] **Step 1: Implement** `TeamOverview` per spec §3: period switch (default This week); totals strip (Bids, Interviews, Earnings, Bids goal %, Interviews goal %, Days logged); table columns User, Bids (`title` tooltip `self X · bidder Y`), Interviews, Earnings, Bids goal %, Interviews goal %, Days logged, Streak; sortable headers (default Bids desc, click toggles, `null` last both ways, `aria-sort` set); amber text for goal % < 50 and logged < half of working days; name is a button calling `onPick`; table in an `overflow-x-auto` wrapper; skeleton and Retry states like `CompareCard`.
- [ ] **Step 2: Implement** `Dashboard.tsx`: picker first option `All users` (value `""`, the default); `""` → `<TeamOverview onPick={setSelectedUserId} />`; otherwise the existing cards for that user.
- [ ] **Step 3: Run** `npx tsc -b` and `vite build` — Expected: as Global Constraints.
- [ ] **Step 4: Commit** (frontend) `feat(dashboard): All users team overview for admins`.
