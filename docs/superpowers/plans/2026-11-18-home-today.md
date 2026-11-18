# Home: Today / This week / Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Home (`/dashboard`) into a daily working surface: today's tickable to-dos, this week's progress against pace, and two metrics; move the net income chart and "You vs team" to a Statistics › Earnings tab.

**Architecture:** Backend gains a draft save (`PUT /daily-plans/{day}?submit=false`, partial merge, no Slack for unsubmitted days), a `weekSaved` flag on the day view, and `/dashboard/week` switches to every stored day + plan-derived pace + routines / weekly-goal fields. The app builds `TodayCard`, `WeekCard`, `MetricsCard` from existing endpoints and reuses DayPanel's item row.

**Tech Stack:** FastAPI + Beanie (mongomock-motor unit tests), React + Vite + SWR + Tailwind (vitest + Testing Library).

**Spec:** `engineers_backend/docs/superpowers/specs/2026-11-18-home-today-design.md`

**Repos / branches:** backend `/home/eningeer/work/Engineer/engineers_backend` on `feat/home-today` (spec commit is there); app `/home/eningeer/work/Engineer/engineers_app` — create `feat/home-today` from `main` in Task 4. Work in the main checkouts (no worktrees). Never commit the app's modified `.env.development` / `.env.production`.

## Global Constraints

- Working days Mon–Fri; "after 6 PM" means ≥ 18:00 America/Chicago; dates `YYYY-MM-DD`.
- `PUT /daily-plans/{day}` `submit` defaults to `true` — existing behavior byte-for-byte unchanged when omitted.
- Draft save (`submit=false`) never sets `submittedAt` / `loggedAt`, never posts to Slack for an unsubmitted day, never touches `done`, `notes`, `targetOverride`, never freezes a result `target`.
- Response shapes change additively only (`weekSaved`, `routines`, `weeklyGoals`); existing keys and the pace keys `expected`, `behindBy`, `perDayNeeded`, `onTrack` stay.
- Pace = one source of truth with the Daily plan: bids expected-by-today = Σ `plan_auto.day_targets(...)` bids over Mon–Fri days ≤ today; past weeks = full week target.
- Streak, logging ratio and team percentile stay logged-only.
- Commit messages: single line, no co-author / "Generated with" trailers.
- Backend tests: `.venv/bin/python -m pytest -q -p no:cacheprovider tests_unit/<file>`. Known local-only failures to ignore: `test_bidder_ext.py::test_upload_url_without_storage_is_503`, `test_resume_prompts.py::...[prompts5-new-1]`; collection errors in `test_resume_experience_roles.py`, `test_style_serializer.py`, `test_template_annotate_removals.py` (pass `--ignore` for those three).
- App tests: `npx vitest run <path>`; type check `npx tsc --noEmit`.

## Review Focus

1. A tick on Home while the Daily plan tab holds typed self bids / notes → those stay untouched (Task 1 test `test_progress_keeps_done_and_notes`).
2. A tick on an already-submitted day → Slack post edited, `submittedAt` unchanged; on an unsubmitted day → no Slack call (Task 1 tests).
3. Rapid typing in a count field → one save per pause, last value wins, failed save reverts the row with a retry (Task 5 test `debounces and reverts on failure`).
4. Week with no saved weekly plan (carry-over draft) → Today read-only, backend rejects draft saves with 422 (Task 1 `test_progress_needs_saved_week`, Task 5 `read-only without a saved week`).
5. Weekend / past week pace → no days left, expected = full target; no target → no pace (Task 2 tests).

---

### Task 1: Draft save + `weekSaved` (backend)

**Files:**
- Modify: `app/plan_save.py` (add `ProgressIn`, `save_progress`; extract shared done rule)
- Modify: `app/routers/daily_plans.py` (`put_day` gains `submit`; `_day_out` adds `weekSaved`)
- Test: `tests_unit/test_daily_plan_progress.py` (new; copy the `db` / `me` fixtures and weekly-plan setup style from `tests_unit/test_daily_plan_api.py`)

**Interfaces:**
- Produces: `class ProgressIn(BaseModel): results: dict[str, ItemResultIn] = {}; focus: Optional[list[str]] = None`
- Produces: `async def save_progress(user: UserExtra, when: date, body: ProgressIn) -> None`
- Produces: `put_day(day, user, payload, submit: bool = True)` — a plain `bool = True` default (FastAPI reads it as a query param; tests call `put_day` directly, so do **not** use `Query(True)`).
- Produces: day view key `weekSaved: bool` = a **saved** weekly plan exists for the day's week (`weekly is not None and weekly.id is not None`; the carry-over draft has no id).
- Produces: `def result_done(kind: str, value: Optional[float], sent_done: bool, target: Optional[float], old: Optional[ItemResult]) -> bool` in `plan_save.py`, used by both `save_day` and `save_progress` (the existing inner `done()` rule, unchanged).

- [ ] **Step 1: Write failing tests** in `tests_unit/test_daily_plan_progress.py` (saved week with routine `r1` check, routine `r2` count target 50, weekly goal `g1`):

```python
async def test_progress_saves_only_sent_results_without_submitting(db, me, week):
    out = await put_day("2026-11-10", me, {"results": {"r1": {"done": True}}}, submit=False)
    plan = await DailyPlan.find_one({"userId": me.id})
    assert plan.results["r1"].done is True and plan.results["r1"].target is None  # not frozen
    assert plan.submittedAt is None and plan.loggedAt is None and plan.targetOverride is None
    assert db == []                      # _sync_slack never called
    assert out["submitted"] is False and out["weekSaved"] is True

async def test_progress_merges_per_item(db, me, week):
    await put_day("2026-11-10", me, {"results": {"r1": {"done": True}}}, submit=False)
    await put_day("2026-11-10", me, {"results": {"r2": {"value": 50}}}, submit=False)
    plan = await DailyPlan.find_one({"userId": me.id})
    assert plan.results["r1"].done is True and plan.results["r2"].done is True  # 50 >= 50

async def test_progress_keeps_done_and_notes(db, me, week):
    await put_day("2026-11-10", me, {"results": {}, "focus": [], "notes": "hi",
                                     "done": {"bidsSelf": 7}})           # a normal submit
    await put_day("2026-11-10", me, {"results": {"r1": {"done": True}}}, submit=False)
    plan = await DailyPlan.find_one({"userId": me.id})
    assert plan.notes == "hi" and plan.done.bidsSelf == 7

async def test_progress_on_submitted_day_edits_post_keeps_submitted_at(db, me, week):
    await put_day("2026-11-10", me, {"results": {}, "focus": [], "notes": "", "done": {}})
    first = (await DailyPlan.find_one({"userId": me.id})).submittedAt
    await put_day("2026-11-10", me, {"results": {"r1": {"done": True}}}, submit=False)
    assert len(db) == 2 and (await DailyPlan.find_one({"userId": me.id})).submittedAt == first

async def test_progress_unknown_item_is_422(db, me, week):
    with pytest.raises(HTTPException) as e:
        await put_day("2026-11-10", me, {"results": {"nope": {"done": True}}}, submit=False)
    assert e.value.status_code == 422

async def test_progress_needs_saved_week(db, me):   # no WeeklyPlan document at all
    with pytest.raises(HTTPException) as e:
        await put_day("2026-11-10", me, {"results": {}}, submit=False)
    assert e.value.status_code == 422

async def test_progress_focus_only_when_sent(db, me, week):
    await put_day("2026-11-10", me, {"focus": ["g1"]}, submit=False)
    await put_day("2026-11-10", me, {"results": {"r1": {"done": True}}}, submit=False)
    assert (await DailyPlan.find_one({"userId": me.id})).focus == ["g1"]

async def test_progress_future_day_is_422(db, me, week):
    with pytest.raises(HTTPException):
        await put_day("2026-11-20", me, {"results": {}}, submit=False)
```

(Adapt the `db` fixture so the list records `_sync_slack` calls, as in `test_daily_plan_api.py`. For the submitted-day test, `save_progress` re-renders via `self_bids.resync_slack`, which calls `daily_plans._sync_slack` — the monkeypatched fake.)

- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q -p no:cacheprovider tests_unit/test_daily_plan_progress.py` — Expected: FAIL (`put_day() got an unexpected keyword argument 'submit'`).

- [ ] **Step 3: Implement.**
  - Extract `result_done(...)` from `save_day`'s inner `done()`; `save_day` calls it (behavior unchanged — `test_daily_plan_api.py` must stay green).
  - `save_progress`: saved week via `weekly_plans_for([user.id], monday_of(when))` (not `week_or_draft`) — none → `HTTPException(422, "Set this week's plan first")`. Items from `day_items(weekly, plan, when)`; any unknown result id or focus id not a `scope == "goal"` item → 422. Each sent result → `ItemResult(done=result_done(...), value=r.value, target=old.target if old else None)`. One `update_one` on `{userId, date}` with `$set` of `results.<id>` per sent id, `focus` only when `body.focus is not None` (deduped, goal ids only), `updatedAt`; `upsert=True` with `$setOnInsert` `year`, `weekNumber` (`app_week_key`), `createdAt`, `loggedAt: None`; on `DuplicateKeyError` retry the update without upsert (same pattern as `plan_bidder.write_through_bidder`). Then, if the stored plan has `submittedAt`, `await self_bids.resync_slack(user, when)`. Log `plan progress: user=%s day=%s ids=%d ms=%d`.
  - `put_day`: when `submit` is false → `parse_body(ProgressIn, payload)`, same `require_writer` and future-day check, `await save_progress(...)`, return `await _view(user.id, when)`. Docstring notes the two modes.
  - `_day_out`: add `"weekSaved"`.

- [ ] **Step 4: Run** the new file and `tests_unit/test_daily_plan_api.py tests_unit/test_slack_daily_plan.py` — Expected: all PASS.

- [ ] **Step 5: Commit** `git add app/plan_save.py app/routers/daily_plans.py tests_unit/test_daily_plan_progress.py && git commit -m "feat: draft day save (submit=false) and weekSaved on day view"`

---

### Task 2: `/dashboard/week` — every stored day, plan pace, routines, weekly goals (backend)

**Files:**
- Modify: `app/dashboard_stats.py` (add `plan_pace`)
- Modify: `app/plan_auto.py` (add `routines_tally`)
- Modify: `app/routers/dashboard.py` (`dashboard_week`)
- Test: `tests_unit/test_dashboard_stats.py` (pace), `tests_unit/test_dashboard_week.py` (new, mongomock; call `dashboard_week(user, userId=None, today="2026-11-18")` directly)

**Interfaces:**
- Produces: `def plan_pace(actual: int, target: Optional[int], expected: int, days_left: int) -> Optional[dict]` → `None` when not `target`; else `{"expected", "behindBy": max(0, expected - actual), "perDayNeeded": ceil(max(0, target - actual) / days_left) if days_left else None, "onTrack": actual >= expected}`.
- Produces: `def routines_tally(weekly, plans: dict[date, Any], days: list[date]) -> tuple[int, int]` — (done, expected): for each day in `days` (caller passes Mon–Fri ≤ today), every `scope == "routine"` entry of `day_items(weekly, plans.get(d), d)` adds 1 to expected; done when its result is done, or value ≥ (result target or item target) for count / hours.
- Produces: response keys `routines: {"done": int, "expected": int}` and `weeklyGoals: [{"id", "text", "kind", "value", "target", "done"}]` (from `goal_progress`; `[]` without a saved week).
- Consumes: `plan_auto.day_targets`, `day_items`, `goal_progress`, `weekly_plans_for`.

- [ ] **Step 1: Failing tests.**

```python
# test_dashboard_stats.py
def test_plan_pace_behind_and_per_day():
    assert plan_pace(700, 2000, 1200, 2) == {"expected": 1200, "behindBy": 500, "perDayNeeded": 650, "onTrack": False}
def test_plan_pace_no_days_left():
    assert plan_pace(2100, 2000, 2000, 0)["perDayNeeded"] is None
def test_plan_pace_no_target():
    assert plan_pace(5, None, 0, 3) is None
```

```python
# test_dashboard_week.py — week of Mon 2026-11-16, saved WeeklyPlan goal bidsSelf=500 bidsBidder=500
# interviewsSelf=10, routines [r1 check, r2 count 50], weeklyGoals [g1 count 250]; today = Wed 11-18.
async def test_counts_unsubmitted_days(db, me):
    # Mon submitted (loggedAt set) done.bidsSelf=100; Tue draft (loggedAt None) done.bidsBidder=80
    out = await dashboard_week(me, None, "2026-11-18")
    assert out["bids"]["total"] == 180

async def test_bid_pace_uses_day_targets_and_overrides(db, me):
    # Tue has targetOverride bidsSelf=300 bidsBidder=0; Mon/Wed derived = 100 + 100 each
    out = await dashboard_week(me, None, "2026-11-18")
    assert out["bids"]["pace"]["expected"] == 200 + 300 + 200
    assert out["bids"]["pace"]["perDayNeeded"] == ceil((1000 - out["bids"]["total"]) / 2)

async def test_interview_pace_is_target_times_workdays_over_five(db, me):
    out = await dashboard_week(me, None, "2026-11-18")
    assert out["interviews"]["pace"]["expected"] == 6        # ceil(10 * 3 / 5)

async def test_weekend_expected_is_full_week(db, me):
    out = await dashboard_week(me, None, "2026-11-21")       # Saturday
    assert out["bids"]["pace"]["expected"] == 1000 and out["bids"]["pace"]["perDayNeeded"] is None

async def test_routines_and_weekly_goals(db, me):
    # Mon: r1 done, r2 value 50; Tue: r1 done; Wed: nothing
    out = await dashboard_week(me, None, "2026-11-18")
    assert out["routines"] == {"done": 3, "expected": 6}
    assert out["weeklyGoals"][0]["id"] == "g1" and out["weeklyGoals"][0]["target"] == 250

async def test_percentile_and_streak_stay_logged_only(db, me):
    # only the Tue draft exists → streak 0, bids percentile computed from logged docs only
    out = await dashboard_week(me, None, "2026-11-18")
    assert out["streak"] == 0
```

- [ ] **Step 2: Run** both files — Expected: FAIL (`plan_pace` / `routines` missing).

- [ ] **Step 3: Implement.** In `dashboard_week`: actual sums use **all** DailyPlan docs of `sid` in Mon–Sun (new small query; keep `_logged_plans` for streak / population / scores). Bids expected = Σ over `d` in Mon–Fri with `d ≤ day` of `day_targets(plans_by_day.get(d), weekly_plan, d)` `bidsSelf + bidsBidder`; days_left = Mon–Fri days after `day`. Interviews expected = `ceil(target * n_workdays_elapsed / 5)` (n capped at 5). Past weeks fall out naturally (all five days ≤ today). Pass `plan_pace(...)`. Remove the `pace` import if unused (keep `dashboard_stats.pace` itself only if another caller exists — none today, so delete it and its three tests).

- [ ] **Step 4: Run** `tests_unit/test_dashboard_stats.py tests_unit/test_dashboard_week.py tests_unit/test_dashboard_compare.py tests_unit/test_dashboard_team.py` — Expected: PASS.

- [ ] **Step 5: Commit** `git commit -m "feat: dashboard week counts every day, plan-based pace, routines and weekly goals"`

---

### Task 3: Backend suite check

- [ ] **Step 1: Run** `.venv/bin/python -m pytest -q -p no:cacheprovider tests_unit --ignore=tests_unit/test_resume_experience_roles.py --ignore=tests_unit/test_style_serializer.py --ignore=tests_unit/test_template_annotate_removals.py` — Expected: only the two known local failures.

(No commit; folded here so app tasks start from a green backend. If anything else fails, fix in the owning task's files and amend that task's commit.)

---

### Task 4: App API + shared ItemRow

**Files:**
- Create: `src/components/report/ItemRow.tsx` (move `ItemRow` out of `DayPanel.tsx`, export default; no behavior change)
- Modify: `src/components/report/DayPanel.tsx` (import it)
- Modify: `src/api/endpoints.ts`
- Test: existing `src/components/report/DayPanel.test.tsx` must stay green

**Interfaces:**
- Produces: `export interface DayProgressInput { results?: Record<string, { done?: boolean; value?: number | null }>; focus?: string[] }`
- Produces: `export const putDayProgress = (date: string, body: DayProgressInput) => putJSON<DayPlan>(\`/daily-plans/${date}?submit=false\`, body);`
- Produces: `DayPlan.weekSaved: boolean`; `DashboardWeek.routines: { done: number; expected: number }`; `DashboardWeek.weeklyGoals: { id: string; text: string; kind: PlanItemKind; value: number; target: number | null; done: boolean }[]`
- Produces: `ItemRow` props unchanged: `{ item: DayItem; result: {done; value}; week?: ItemProgress; disabled: boolean; onChange }`

- [ ] **Step 1:** `cd /home/eningeer/work/Engineer/engineers_app && git checkout main && git pull --ff-only spark main && git checkout -b feat/home-today` (pull with `-c credential.helper= -c 'credential.helper=!gh auth git-credential'` if the stored login fails).
- [ ] **Step 2:** Move `ItemRow`, add types and `putDayProgress`.
- [ ] **Step 3: Run** `npx tsc --noEmit && npx vitest run src/components/report` — Expected: PASS.
- [ ] **Step 4: Commit** `git add src/api/endpoints.ts src/components/report/ItemRow.tsx src/components/report/DayPanel.tsx && git commit -m "refactor: shared ItemRow; draft day save and week fields in API types"`

---

### Task 5: TodayCard

**Files:**
- Create: `src/components/home/TodayCard.tsx`, `src/components/home/TodayCard.test.tsx`

**Interfaces:**
- Consumes: `api.getDayPlan(today)`, `api.putDayProgress`, `api.listInterviewRounds({ from: today, to: today, creatorId: user.id })`, `ItemRow`, `todayInputValue()` from `src/lib/week`, `useAuth()`.
- Produces: `export default function TodayCard()` (always the signed-in user; no props).

Behavior (from spec "Today card"): header `Today · Wed Nov 18` with status — not submitted: "Update not posted yet" (amber class only when Mon–Fri and Chicago time ≥ 18:00) + link **Write my update** → `/report?day=<today>`; submitted (`submitted === true`): "✓ Posted" + link **Edit update** (same href). *(Ruling vs spec's "✓ Posted 6:12 PM": the day view has no `submittedAt` time, and adding one isn't worth a field; plain "✓ Posted".)* Rows: Self bids `done.bidsSelf / targets.bidsSelf` read-only + "edit" link (same href); Bidder bids `bidsBidderAuto / targets.bidsBidder` read-only; Interviews `interviews.self + interviews.caller / (targets.interviewsSelf + targets.interviewsCaller)` with today's rounds as `h:mm a Company` joined by " · " (sorted by `scheduledAt`, omitted when none); then `ItemRow` for every `scope === 'routine'` item and every goal item in `focus`. `weekSaved === false` → rows `disabled` and a line "No plan for this week yet — Set this week's plan" linking `/plans`. Edits: optimistic local state; per-item debounce 800 ms → `putDayProgress(today, { results: { [id]: r } })`; on success `mutate` with the returned `DayPlan`; on failure restore the last saved result for that item and show inline "Couldn't save — Retry" (Retry resends).

- [ ] **Step 1: Failing tests** (mock `../../api/endpoints` and `../../auth/useAuth` as `DayPanel.test.tsx` does; fake timers; system time `2026-11-18T19:00:00-06:00`):
  - `shows targets, bidder auto count and today's interviews` → text `52 / 80`, `86 / 120`, `2:00 PM Acme · 4:30 PM Globex`.
  - `ticking a routine saves a draft once after the pause` → click "Bidder Management", advance 800 ms → `putDayProgress` called once with `('2026-11-18', { results: { r1: { done: true, value: null } } })`.
  - `debounces and reverts on failure` → type 3, 31 in "Email outreach" within 800 ms → one call with value 31; reject it → input back to saved value and "Retry" shown; click Retry → called again with 31.
  - `read-only without a saved week` → `weekSaved: false` → checkbox disabled and link "Set this week's plan" to `/plans`.
  - `amber after 6 PM when not posted; posted state links Edit update` → class check on the status at 19:00; with `submitted: true` text "✓ Posted" and link "Edit update" href `/report?day=2026-11-18`.
- [ ] **Step 2: Run** `npx vitest run src/components/home/TodayCard.test.tsx` — Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `TodayCard`.
- [ ] **Step 4: Run** same — Expected: PASS; `npx tsc --noEmit` clean.
- [ ] **Step 5: Commit** `git commit -m "feat: Home Today card with draft saves"`

---

### Task 6: WeekCard + MetricsCard

**Files:**
- Create: `src/components/home/WeekCard.tsx`, `src/components/home/MetricsCard.tsx`, `src/components/home/HomeCards.test.tsx`

**Interfaces:**
- Consumes: `api.getDashboardWeek({ today })`, `api.getDashboardNetMonthly({ today })`, `todayInputValue()`.
- Produces: `export default function WeekCard()`, `export default function MetricsCard()`.

WeekCard: title `This week · Nov 16 – Nov 20` (Mon–Fri) and `day N of 5` (Mon–Fri only). Bids and Interviews rows: `total / target`, "▼N vs pace" (`pace.behindBy > 0`) or "▲N vs pace" (`total - pace.expected`, shown when ≥ 0), bar width `min(100, total/target)`, pace tick at `expected/target`; no target → just the number, no arrow, no bar. `Routines done / expected` (hidden when expected = 0). Weekly goals list: check kind → ✓ / ☐; count / hours → `value / target` (+ ✓ when `done` or value ≥ target). MetricsCard: "Earnings this month" = last element of `months` `net`, vs previous element: `▲12%` / `▼12%` (rounded; hidden when previous is 0), link "Statistics" → `/statistics?tab=earnings`; "Pipeline" = `stages` with count > 0 except `canceled`, as `3 Tech · 2 Final · 1 Offer` (API `label` as is), link → `/interviews`; no stages → "No interviews in progress".

- [ ] **Step 1: Failing tests** in `HomeCards.test.tsx`:
  - `bids behind pace` → total 734, target 2000, pace `{expected: 800, behindBy: 66}` → text `734 / 2000` and `▼66 vs pace`.
  - `ahead of pace` → interviews 5/10, expected 4 → `▲1 vs pace`.
  - `no target shows only the count` → target null, pace null → `3`, no "vs pace".
  - `routines and weekly goals` → `11 / 15`; goal `140 / 250`; check goal done → `✓`.
  - `earnings vs last month and pipeline` → months `[..., {net: 4300}, {net: 4820}]` → `$4,820` and `▲12%`; stages tech 3, final 2, offer 1, canceled 4 → `3 Tech · 2 Final · 1 Offer` (no Canceled).
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — Expected: PASS; tsc clean.
- [ ] **Step 5: Commit** `git commit -m "feat: Home This week and Metrics cards"`

---

### Task 7: Assemble Home + Statistics › Earnings

**Files:**
- Modify: `src/pages/Dashboard.tsx` — render `TodayCard`, `WeekCard`, `MetricsCard` for everyone; for admins, below them, `TeamOverview`. The old admin user `<select>` and `selectedUserId` state are removed (the per-user cards they switched to are gone). `TeamOverview onPick(userId)` navigates to `/statistics?tab=earnings&user=<userId>`.
- Modify: `src/pages/Statistics.tsx` — tab `{ key: 'earnings', label: 'Earnings' }` after Interviews, `TAB_KEYS` updated, renders `<EarningsTab />`.
- Create: `src/components/statistics/EarningsTab.tsx` — `NetIncomeCard` + `CompareCard` (both with `userId` undefined = self; admins get a user `<select>` like the old Dashboard's, initialised from the `?user=` URL param, passing the chosen id).
- Delete: `src/components/dashboard/ThisWeekCard.tsx` (no remaining importers — grep first).
- Test: `src/pages/Dashboard.test.tsx` (new), `src/pages/Statistics` tab test inline in `EarningsTab.test.tsx` (new)

**Interfaces:**
- Consumes: Tasks 5–6 components; existing `NetIncomeCard({userId})`, `CompareCard({userId})`, `TeamOverview`, `useTeamLookup`.

- [ ] **Step 1: Failing tests:**
  - `Dashboard.test.tsx`: `staff see Today, This week and Metrics only` (mock the three cards as stubs, `useAuth` role staff) → three stubs rendered, no TeamOverview. `admins also see the team table below` → TeamOverview stub after the Metrics stub.
  - `EarningsTab.test.tsx`: `renders net income and you-vs-team` (stub both cards) → both present; admin `<select>` change → both stubs receive the chosen `userId`.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement**; `grep -rn "ThisWeekCard" src` → only the deleted file; delete it.
- [ ] **Step 4: Run** `npx tsc --noEmit && npx vitest run` — Expected: all PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: new Home layout; net income and you-vs-team move to Statistics Earnings tab"`

---

### Task 8: Run it

- [ ] **Step 1:** Backend dev server (already running on 8080 with `--reload`, on branch `feat/home-today`) and Vite on 5173. Open `/dashboard` signed in as a staff user; tick a routine; confirm via `GET /daily-plans/<today>` that the result saved and `submitted` is still false, and no Slack post was created (`slackTs` null). Open `/statistics?tab=earnings`.
- [ ] **Step 2:** Note results in the task report. No commit.
