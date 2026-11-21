# Admin Weekly Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The admin plans every person's week in one grid, a new `log` item type records profile reviews in the daily standup, and a per-person switch hands planning back to the person.

**Architecture:** Each person's `WeeklyPlan` stays the only home of their plan. The backend gains a `log` item kind with per-day entries, an admin-only team read / bulk save that reuses the existing single-week save, and `UserExtra.plansOwnWeek` gating the person's own writes. The app gains a setup grid whose columns are derived client-side by matching item names across people; all grid edits are pure functions over one in-memory team state, saved as changed rows.

**Tech Stack:** Backend: Python, FastAPI, Beanie / MongoDB, pytest + mongomock-motor (`tests_unit/`). App: React 18, TypeScript, SWR, Tailwind, Vitest + Testing Library.

**Spec:** `engineers_backend/docs/superpowers/specs/2026-11-21-admin-weekly-planning-design.md` (read it first). Prototype of the screens: `engineers_app/.superpowers/brainstorm/1220572-1795296821/content/plan-setup-v4.html`.

## Global Constraints

- Two repos: `/home/eningeer/work/Engineer/engineers_backend` (default branch `dev`) and `/home/eningeer/work/Engineer/engineers_app` (default branch `main`). Work on branch `feat/admin-weekly-planning` in each main checkout (no worktrees). Never push.
- Commit messages: one line, no body, no co-author or "Generated with" trailer.
- The local backend runs against the **production database**. Never exercise a write endpoint against the running server to "try it"; all behaviour is proven by unit tests on mongomock. No migration scripts; new fields must default cleanly on existing documents.
- `MAX_ITEMS` = 20 per list, `MAX_ITEM_LENGTH` = 200 stay. Log entries: note 1..200 chars, at most 20 per result.
- Items are never deleted, only archived (`_merge_items`).
- Admins still cannot submit a day or own a plan (`require_writer` stays on every existing write).
- Backend tests: `cd engineers_backend && .venv/bin/pytest tests_unit/<file> -q`. App tests: `cd engineers_app && npx vitest run <file>`; types: `npm run lint`.
- New users in backend tests that must edit their own week need `plansOwnWeek=True` (the default becomes `False` in Task 5).
- Exact copy: 403 detail `"Your week is planned by your admin"`; Week panel note `"Planned by your admin"`; Slack log hint `"One line per profile: Profile name: what changed"`.

## Review Focus

1. Publishing twice (double click / retry) must not create a second plan, duplicate items or new ids — Task 6 test `test_team_save_twice_is_the_same`.
2. A person with a typed US / EU / LATAM split whose total the admin did **not** change must keep the split after a publish — Task 6 test `test_team_save_keeps_split_when_total_unchanged`.
3. Two people with the same item name but different kinds (e.g. "Outreach" count vs check) must not be merged into one grid column — Task 10 test `different kind is a different column`.
4. A Slack submission for a day whose log entries were made in the app with profiles must not lose entries the form did not show, and a blank Slack field must clear them only when the field was shown — Task 3 tests `test_form_roundtrip_log` and `test_keep_unshown_keeps_entries`.
5. A log entry pointing at another user's profile id must be rejected, and an entry for a profile deleted since must not make the day unsaveable — Task 2 tests `test_entries_reject_foreign_profile` and `test_entries_drop_deleted_profile`.

---

## File Structure

Backend (`engineers_backend/`):
- `app/models/plan_common.py` — `log` kind, `LogEntry`, `ItemResult.entries`, `result_is_done`.
- `app/plan_auto.py` — done rules use `result_is_done`; `_draft` carries recurring weekly goals.
- `app/plan_save.py` — entries in `ItemResultIn`, `save_day`, `save_progress`.
- `app/slack_bot.py` — post lines for a log item.
- `app/standup/modal.py` — form field, read, keep-unshown for a log item.
- `app/models/user_extra.py` — `plansOwnWeek`.
- `app/week_save.py` (new) — `save_week`, the one writer of a `WeeklyPlan`, used by the person's PUT and the team PUT.
- `app/routers/weekly_plans.py` — gate on `plansOwnWeek`; team read / save routes.
- `app/alerts_service.py` — Monday alert skips admin-planned people.

App (`engineers_app/src/`):
- `api/endpoints.ts` — types and calls.
- `components/report/LogEntries.tsx` (new) — the profile + note rows.
- `components/report/ItemRow.tsx`, `DayPanel.tsx`, `components/home/MyHome.tsx`, `PlanItemsEditor.tsx` — log kind.
- `components/report/WeekPanel.tsx` — view-only / admin-edit modes.
- `lib/teamGrid.ts` (new) — pure grid model.
- `components/report/SetupGrid.tsx` (new) — the grid screen.
- `pages/Report.tsx` — `view=setup`.

---

### Task 1: Log kind and the done rule (backend model)

**Files:**
- Modify: `engineers_backend/app/models/plan_common.py`, `engineers_backend/app/plan_auto.py`
- Test: `engineers_backend/tests_unit/test_plan_common.py`, `engineers_backend/tests_unit/test_plan_auto.py`

**Interfaces:**
- Produces: `LogEntry(profileId: Optional[str] = None, note: str)`; `ItemResult.entries: list[LogEntry] = []`; `PlanItem.kind: Literal["check", "count", "hours", "log"]`; `result_is_done(kind: str, r: Optional[ItemResult], target: Optional[float]) -> bool` in `plan_common.py`.

- [ ] **Step 1: Create the branch**

```bash
cd /home/eningeer/work/Engineer/engineers_backend && git checkout -b feat/admin-weekly-planning
```

- [ ] **Step 2: Write the failing tests** (append to `tests_unit/test_plan_common.py`)

```python
from app.models.plan_common import ItemResult, LogEntry, PlanItem, result_is_done

def test_log_kind_and_entries():
    assert PlanItem(text="Profile review", kind="log").kind == "log"
    r = ItemResult(entries=[{"profileId": "a" * 24, "note": " rewrote summary "}])
    assert r.entries[0].note == "rewrote summary"
    assert ItemResult().entries == []          # old documents read fine

def test_log_entry_limits():
    import pytest
    with pytest.raises(ValueError):
        LogEntry(note="   ")
    with pytest.raises(ValueError):
        LogEntry(note="x" * 201)
    with pytest.raises(ValueError):
        ItemResult(entries=[{"note": "n"}] * 21)

def test_result_is_done():
    assert result_is_done("log", ItemResult(entries=[{"note": "n"}]), None)
    assert not result_is_done("log", ItemResult(done=True), None)   # a tick without entries is not done
    assert not result_is_done("log", None, None)
    assert result_is_done("check", ItemResult(done=True), None)
    assert result_is_done("count", ItemResult(value=50), 50)
    assert not result_is_done("count", ItemResult(value=49), 50)
    assert result_is_done("hours", ItemResult(done=True, value=1), 2)  # ticked short
```

Append to `tests_unit/test_plan_auto.py` (use that file's existing helpers for building a `WeeklyPlan` and `DailyPlan`; `MON` is a Monday there):

```python
def test_log_routine_tally_and_goal_progress():
    # a log routine with one entry counts 1/1 in items_tally; with a result but no entries 0/1
    # a log weekly goal with entries on any day reads done in goal_progress, value = number of entries that week
```

Assertions: `items_tally(weekly, day_with_entry, MON) == (1, 1)`, `items_tally(weekly, day_empty_result, MON) == (0, 1)`, `goal_progress(weekly, [d1, d2])[goal_id] == {"value": 3, "done": True, "target": None, ...}` for 2 + 1 entries (keep whatever extra keys `goal_progress` already returns).

- [ ] **Step 3: Run them, expect failure**

Run: `.venv/bin/pytest tests_unit/test_plan_common.py tests_unit/test_plan_auto.py -q`
Expected: FAIL (`LogEntry` / `result_is_done` not importable).

- [ ] **Step 4: Implement**

In `plan_common.py`: `LogEntry` (note stripped, 1..200, reuse `MAX_ITEM_LENGTH`), `MAX_LOG_ENTRIES = 20`, `entries` on `ItemResult` with a length validator, `"log"` in `PlanItem.kind`, and:

```python
def result_is_done(kind: str, r: Optional[ItemResult], target: Optional[float]) -> bool:
    """A day's result is done: log = has entries; otherwise ticked, or its value at / over `target`."""
    if r is None:
        return False
    if kind == "log":
        return bool(r.entries)
    return bool(r.done or (r.value is not None and target is not None and r.value >= target))
```

In `plan_auto.py`: `_routine_done` delegates to `result_is_done(item.kind, r, frozen_or_item_target)`; `items_tally`'s weekly-goal `hit` and `routines_grid` treat a log result as hit when it has entries; `goal_progress` for a log goal sums `len(entries)` as `value` and is done when the sum > 0.

- [ ] **Step 5: Run the two files plus the wider plan tests**

Run: `.venv/bin/pytest tests_unit/test_plan_common.py tests_unit/test_plan_auto.py tests_unit/test_daily_plan_api.py tests_unit/test_dashboard_week.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/models/plan_common.py app/plan_auto.py tests_unit/test_plan_common.py tests_unit/test_plan_auto.py
git commit -m "feat: plan items gain a log kind whose day result is a list of profile entries"
```

---

### Task 2: Saving and reading log entries on a day

**Files:**
- Modify: `engineers_backend/app/plan_save.py`
- Test: `engineers_backend/tests_unit/test_daily_plan_api.py`, `engineers_backend/tests_unit/test_daily_plan_progress.py`

**Interfaces:**
- Consumes: Task 1 (`LogEntry`, `ItemResult.entries`, `result_is_done`).
- Produces: `ItemResultIn.entries: Optional[list[LogEntry]] = None` (None = not sent, keep stored; `[]` = clear). `GET /daily-plans/{day}` items already carry `result.entries` through `_item_out`'s `model_dump()`.

- [ ] **Step 1: Write the failing tests** (in `test_daily_plan_api.py`, with its `db` / `me` fixtures; create an `Account(createdBy=me.id, name="John", region="US")` for the profile and a saved `WeeklyPlan` with one `log` routine `rid`)

```python
async def test_day_saves_log_entries(db, me): 
    # put_day with results={rid: {"entries": [{"profileId": pid, "note": "rewrote summary"}]}}
    # → get_day item result: done True, entries == [{"profileId": pid, "note": "rewrote summary"}]

async def test_entries_not_sent_are_kept(db, me):
    # second put_day with results={rid: {}} keeps the entry; with {"entries": []} clears it and done is False

async def test_entries_reject_foreign_profile(db, me):
    # profileId of an Account created by another user → HTTPException 422

async def test_entries_drop_deleted_profile(db, me):
    # profileId that is a valid ObjectId but no Account at all → saved with profileId None, note kept, no error

async def test_log_tick_without_entries_is_not_done(db, me):
    # results={rid: {"done": True}} → stored done False
```

In `test_daily_plan_progress.py`: `test_progress_saves_entries` — `put_day(..., submit=False)` with entries stores them under `results.<rid>.entries` and leaves other results untouched.

- [ ] **Step 2: Run, expect failure**

Run: `.venv/bin/pytest tests_unit/test_daily_plan_api.py tests_unit/test_daily_plan_progress.py -q -k "entries or log_"`
Expected: FAIL.

- [ ] **Step 3: Implement in `plan_save.py`**

- `ItemResultIn.entries`.
- `async def _clean_entries(user_id, sent: list[LogEntry]) -> list[LogEntry]`: against `owner_profiles(user_id)` ids — own profile → kept; id belonging to another user's `Account` → 422 `"Unknown profile"`; id of no account → `profileId=None`.
- `save_day` and `save_progress`: for a `log` item, entries = cleaned sent entries when sent, else the stored ones; `done = bool(entries)`; `value`/`target` stay `None`. Non-log items ignore `entries`.

- [ ] **Step 4: Run the files**

Run: `.venv/bin/pytest tests_unit/test_daily_plan_api.py tests_unit/test_daily_plan_progress.py tests_unit/test_daily_plan_by_profile.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/plan_save.py tests_unit/test_daily_plan_api.py tests_unit/test_daily_plan_progress.py
git commit -m "feat: a day save stores a log item's profile entries and rejects another user's profile"
```

---

### Task 3: Log items in the Slack post and the Slack form

**Files:**
- Modify: `engineers_backend/app/slack_bot.py` (`_item_line`, `daily_plan_text`), `engineers_backend/app/standup/modal.py` (`_item_block`, `read_submission`, `submission_errors`, `_keep_unshown`), `engineers_backend/app/routers/daily_plans.py` (`_sync_slack` passes profile names)
- Test: `engineers_backend/tests_unit/test_slack_daily_plan.py`, `engineers_backend/tests_unit/test_standup_modal.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces: `daily_plan_text(..., profile_names: dict[str, str] = {})` (profile id → name) and the same keyword through `sync_daily_plan_message`; Slack form block id `item:<id>` with action id `log` (multiline plain text).

- [ ] **Step 1: Write the failing tests**

`test_slack_daily_plan.py`:

```python
def test_log_routine_lists_entries():
    # routine "Profile review" kind log, result entries [{profileId: P1, note: "Rewrote summary"}, {profileId: None, note: "Maria: new headline"}]
    # profile_names={P1: "John D."}
    # text contains a "Profile review" line ending "✅ 2 profiles",
    # then "• John D.: Rewrote summary" and "• Maria: new headline" on the next two lines

def test_log_routine_without_entries_is_not_done():
    # no result → line "Profile review" + "❌ not done" (same wording the check routine uses); the item is not omitted

def test_log_one_entry_is_singular():
    # "✅ 1 profile"
```

`test_standup_modal.py`:

```python
async def test_form_shows_log_as_multiline(...):
    # build_view: block_id "item:<rid>", element type plain_text_input, multiline True, action_id "log",
    # hint "One line per profile: Profile name: what changed", optional True
    # with stored entries [{profileId: P1, "Rewrote summary"}, {None, "free line"}] initial_value == "John D.: Rewrote summary\nfree line"

def test_form_roundtrip_log():
    # read_submission of a view whose log field is "John: a\n\n  Maria: b  " → results[rid].entries notes ["John: a", "Maria: b"], profileId None
    # blank field → entries == [] (sent, clears)

async def test_keep_unshown_keeps_entries(...):
    # a saved day with entries on an item the submitted form did not contain keeps them after _keep_unshown

def test_log_line_too_long_is_a_field_error():
    # a 201-char line → submission_errors == {"item:<rid>": "Each line is limited to 200 characters"}
    # 21 lines → {"item:<rid>": "At most 20 lines"}
```

- [ ] **Step 2: Run, expect failure**

Run: `.venv/bin/pytest tests_unit/test_slack_daily_plan.py tests_unit/test_standup_modal.py -q`
Expected: new tests FAIL.

- [ ] **Step 3: Implement**

- `_item_line` returns the head line; a log item's entry lines are appended by `daily_plan_text` right after it, each `f"     • {name}: {note}"` (name from `profile_names`, omitted with its colon when `profileId` is None or unknown). Weekly-goal log items print `"✅ N profiles today"` when the day has entries, else `"in focus"` like a check goal.
- `_sync_slack` builds `profile_names` from `owner_profiles(owner.id)` and passes it; `self_bids.resync_slack` goes through the same function, so no second call site.
- `modal.py`: log branch in `_item_block` (archived log → context line `"<text> (removed) · N profiles"`); `read_submission` maps action `log` to `ItemResultIn(entries=[...])`; `submission_errors` adds the two messages above; `_keep_unshown` copies `entries` in `kept`.

- [ ] **Step 4: Run**

Run: `.venv/bin/pytest tests_unit/test_slack_daily_plan.py tests_unit/test_standup_modal.py tests_unit/test_slack_interactions.py tests_unit/test_standup_follow_ups.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/slack_bot.py app/standup/modal.py app/routers/daily_plans.py tests_unit/test_slack_daily_plan.py tests_unit/test_standup_modal.py
git commit -m "feat: the standup post lists a log item's profile entries and the Slack form takes them one per line"
```

---

### Task 4: Recurring weekly goals carry over

**Files:**
- Modify: `engineers_backend/app/plan_auto.py` (`_draft`)
- Test: `engineers_backend/tests_unit/test_plan_auto.py`

- [ ] **Step 1: Write the failing test**

```python
def test_draft_carries_count_and_hours_weekly_goals():
    # prev.weeklyGoals: count "Email outreach" target 250 (id g1), hours "Research" target 4 (g2),
    # check "Background check" (g3), log "Profile audit" (g4), archived count "Old" (g5)
    # _draft(prev, next_monday).weeklyGoals ids == ["g1", "g2"], targets kept
```

- [ ] **Step 2: Run, expect failure** — `.venv/bin/pytest tests_unit/test_plan_auto.py -q -k carries` → FAIL.

- [ ] **Step 3: Implement** — `_draft` copies live `weeklyGoals` whose kind is `count` or `hours`. Update the docstrings of `draft_week` and the module header of `routers/weekly_plans.py` that say weekly goals are not carried.

- [ ] **Step 4: Run** — `.venv/bin/pytest tests_unit/test_plan_auto.py tests_unit/test_daily_plan_api.py -q` → PASS (fix any existing test that asserted no weekly goals in a draft only if its goals were count / hours).

- [ ] **Step 5: Commit**

```bash
git add app/plan_auto.py app/routers/weekly_plans.py tests_unit/test_plan_auto.py
git commit -m "feat: a new week's draft carries last week's count and hours weekly goals"
```

---

### Task 5: The Plans-own-week switch gates the person's own writes

**Files:**
- Create: `engineers_backend/app/week_save.py`
- Modify: `engineers_backend/app/models/user_extra.py`, `engineers_backend/app/routers/weekly_plans.py`, `engineers_backend/app/alerts_service.py`
- Test: `engineers_backend/tests_unit/test_daily_plan_api.py`, `engineers_backend/tests_unit/test_alerts_tick.py`

**Interfaces:**
- Produces: `UserExtra.plansOwnWeek: bool = False`. `week_save.save_week(uid, monday: date, *, goal: Optional[Counts], routines: Optional[list[PlanItemIn]], weekly_goals: Optional[list[PlanItemIn]], recap: Optional[str]) -> WeeklyPlan` — `None` for any argument means "leave as stored (or as the carry-over draft on a first save)". `PlanItemIn`, `PlanItemsIn`, `WeekIn` and `_merge_items` move to `week_save.py` (re-exported from the router so existing imports keep working). `GET /weekly-plans/{week}` response gains `"plansOwnWeek": bool` (the subject's).

- [ ] **Step 1: Write the failing tests** (`test_daily_plan_api.py`; set `plansOwnWeek=True` in the `me` fixture so existing tests keep passing, and add a second fixture `planned` = a user with the default)

```python
async def test_admin_planned_person_cannot_change_week(db, planned):
    # no saved plan: put_week(WEEK, planned, {...goal...}) → HTTPException 403, detail "Your week is planned by your admin"

async def test_admin_planned_person_can_write_recap(db, planned):
    # with a saved WeeklyPlan (goal bidsSelf 600, one routine): put_week with goal bidsSelf 10, routines [], recapNotes "good week"
    # → stored goal.bidsSelf == 600, routine still live, recapNotes == "good week"

async def test_admin_planned_person_cannot_delete_week(db, planned):   # 403

async def test_get_week_tells_who_plans(db, me, planned):
    # get_week for me → out["plansOwnWeek"] is True; for planned → False
```

`test_alerts_tick.py`: `test_weekly_plan_alert_skips_admin_planned` — on a Monday-morning tick, a user with `plansOwnWeek=False` and no plan gets no `weekly_plan` notification; one with `True` does.

- [ ] **Step 2: Run, expect failure**

Run: `.venv/bin/pytest tests_unit/test_daily_plan_api.py tests_unit/test_alerts_tick.py -q`
Expected: new tests FAIL.

- [ ] **Step 3: Implement**

- `user_extra.py`: the field, next to the Slack settings, with a comment `# False: the admin plans this person's week (Plans page setup grid)`.
- `week_save.py`: move the body of `put_week` into `save_week` (snapshot → base = plan or draft → merge → insert / save). It logs one line: `week save: user=%s week=%s routines=%d goals=%d ms=%d`.
- `put_week`: `require_writer`; when `user.plansOwnWeek` is false → 403 unless the week has a saved plan, then `save_week(..., goal=None, routines=None, weekly_goals=None, recap=body.recapNotes)`; else `save_week` with all four.
- `delete_week`: 403 when false.
- `_week_out`: takes the subject's flag; `get_week` loads it (`user.plansOwnWeek` for self, else from the `UserExtra` of `uid`).
- `_tick_weekly_plans`: `if not user.plansOwnWeek: continue`.

- [ ] **Step 4: Run every plan / alert test**

Run: `.venv/bin/pytest tests_unit -q -k "plan or alert or home or dashboard or report"`
Expected: PASS. Any other test file that calls `put_week` with a default user gets `plansOwnWeek=True` on that user.

- [ ] **Step 5: Commit**

```bash
git add app/week_save.py app/models/user_extra.py app/routers/weekly_plans.py app/alerts_service.py tests_unit
git commit -m "feat: a person whose week the admin plans can only write their recap notes, and gets no Monday plan alert"
```

---

### Task 6: Team read and bulk save for the grid

**Files:**
- Modify: `engineers_backend/app/routers/weekly_plans.py`
- Test: `engineers_backend/tests_unit/test_weekly_plan_team.py` (new; copy the `db` fixture pattern from `test_daily_plan_api.py`)

**Interfaces:**
- Consumes: `save_week` (Task 5), `weeks_or_drafts`, `weekly_plans_for` (`plan_auto`), `require_admin` (`app.deps`).
- Produces (declare both routes **above** `/{week_start}`):
  - `GET /weekly-plans/team/{week_start}` → `{"weekStart": "YYYY-MM-DD", "users": [{"userId", "name", "plansOwnWeek", "exists", "goal": {"bidsSelf", "bidsBidder", "interviewsSelf"}, "routines": [PlanItem], "weeklyGoals": [PlanItem]}]}`; active non-admin users sorted by name; live (non-archived) items only; `exists` = has a saved plan.
  - `PUT /weekly-plans/team/{week_start}` body `{"plans": [{"userId": str, "plansOwnWeek": bool, "goal": {"bidsSelf": int, "bidsBidder": int, "interviewsSelf": int}, "routines": [PlanItemIn], "weeklyGoals": [PlanItemIn]}]}` (1..100 rows) → `{"results": [{"userId", "ok": bool, "error": Optional[str]}]}`.
  - Python handlers named `get_team_week(week_start, admin)` and `put_team_week(week_start, admin, payload)`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_team_read_saved_draft_and_empty(db, admin, ana, bo, cy):
    # ana: saved plan this week; bo: plan last week only (draft); cy: nothing
    # users in name order; ana exists True; bo exists False with last week's goal + routines; cy exists False, empty lists, zero goal
    # archived items are left out; an admin and an inactive user are not listed

async def test_team_read_is_admin_only(db, ana):          # staff caller → 403 from require_admin

async def test_team_save_creates_and_updates(db, admin, ana, bo):
    # ana (saved) gets a new routine; bo (draft) saved → WeeklyPlan exists for bo, routine ids == the draft's ids
    # results all ok

async def test_team_save_twice_is_the_same(db, admin, ana):
    # send the response of get_team_week back unchanged, twice → one WeeklyPlan document for ana, same item ids, no archived items

async def test_team_save_archives_removed_and_keeps_hidden_fields(db, admin, ana):
    # ana: recapNotes "r", goal interviewsCaller 4, an archived routine with a day result
    # save without one live routine → that routine archived, not deleted; recapNotes "r"; interviewsCaller 4; old archived item still there

async def test_team_save_keeps_split_when_total_unchanged(db, admin, ana):
    # ana goal bidsSelf 600 = US 400 + LATAM 200; row sends bidsSelf 600 → regions kept
    # row sends bidsSelf 650 → bidsSelf 650, US/EU/LATAM all 0

async def test_team_save_writes_the_switch(db, admin, ana):
    # plansOwnWeek True in the row → UserExtra updated; unchanged value → no user write

async def test_team_save_row_failures_do_not_stop_the_rest(db, admin, ana):
    # rows: unknown user id, an admin's id, a row with 21 routines, ana valid
    # → three results ok False with messages "User not found", "Admins have no plan", and the items-limit message; ana ok True and saved

async def test_team_save_skips_empty_row_without_a_plan(db, admin, cy):
    # cy has no plan; row all zero and no items → ok True, no WeeklyPlan created (the switch is still written)

async def test_team_save_retype_is_remove_plus_add(db, admin, ana):
    # row drops id r1 and sends a new item (no id) with the same text and kind "check" → r1 archived, new id live
```

- [ ] **Step 2: Run, expect failure** — `.venv/bin/pytest tests_unit/test_weekly_plan_team.py -q` → FAIL (handlers missing).

- [ ] **Step 3: Implement**

- Rows are validated one by one (`parse` each row inside a `try` that turns `HTTPException` / `ValidationError` into that row's `error` string; any other exception is logged with `log.exception` and reported as `"Could not save"`), so one bad row never fails the call.
- Goal rule per row: `bidsBidder` and `interviewsSelf` are set from the row; `interviewsCaller` kept; if the row's `bidsSelf` equals the stored (or draft) `goal.bidsSelf` the whole stored bid split is kept, else `bidsSelf` = the row's and the three region fields = 0.
- Each row calls `save_week(uid, monday, goal=…, routines=…, weekly_goals=…, recap=None)`.
- One summary log line: `team week save: admin=%s week=%s ok=%d failed=%d ms=%d` (no names, no item text).
- The read uses `weeks_or_drafts` (two queries for all users), not a per-user loop.

- [ ] **Step 4: Run** — `.venv/bin/pytest tests_unit/test_weekly_plan_team.py tests_unit/test_daily_plan_api.py -q` → PASS.

- [ ] **Step 5: Run the whole backend suite**

Run: `.venv/bin/pytest tests_unit -q`
Expected: PASS, no new failures against `dev`.

- [ ] **Step 6: Commit**

```bash
git add app/routers/weekly_plans.py tests_unit/test_weekly_plan_team.py
git commit -m "feat: GET and PUT /weekly-plans/team/{week} let an admin read and save every person's week in one call"
```

---

### Task 7: App types and API calls

**Files:**
- Modify: `engineers_app/src/api/endpoints.ts`

**Interfaces:**
- Produces:

```ts
export type PlanItemKind = 'check' | 'count' | 'hours' | 'log';
export interface LogEntry { profileId: string | null; note: string }
// ItemResult gains: entries?: LogEntry[]
// DayPlanInput / DayProgressInput results gain: entries?: LogEntry[]
// WeekPlan gains: plansOwnWeek: boolean
export interface TeamGoal { bidsSelf: number; bidsBidder: number; interviewsSelf: number }
export interface TeamWeekUser { userId: string; name: string | null; plansOwnWeek: boolean; exists: boolean; goal: TeamGoal; routines: PlanItem[]; weeklyGoals: PlanItem[] }
export interface TeamWeekRowIn { userId: string; plansOwnWeek: boolean; goal: TeamGoal; routines: PlanItemIn[]; weeklyGoals: PlanItemIn[] }
export const getTeamWeek = (weekStart: string) => apiFetch<{ weekStart: string; users: TeamWeekUser[] }>(`/weekly-plans/team/${weekStart}`);
export const putTeamWeek = (weekStart: string, plans: TeamWeekRowIn[]) =>
  putJSON<{ results: { userId: string; ok: boolean; error: string | null }[] }>(`/weekly-plans/team/${weekStart}`, { plans });
```

- [ ] **Step 1: Create the branch** — `cd /home/eningeer/work/Engineer/engineers_app && git checkout -b feat/admin-weekly-planning`
- [ ] **Step 2: Add the types and calls above.**
- [ ] **Step 3: Type-check** — `npm run lint` → no errors (fix every `switch` / comparison on `PlanItemKind` the compiler now flags by treating `'log'` like `'check'` for targets; behaviour comes in Task 8).
- [ ] **Step 4: Commit** — `git add src && git commit -m "feat: plan types gain the log kind, log entries and the team week calls"`

---

### Task 8: Log items in the day panel, Home and the item editor

**Files:**
- Create: `engineers_app/src/components/report/LogEntries.tsx`
- Modify: `engineers_app/src/components/report/ItemRow.tsx`, `DayPanel.tsx`, `PlanItemsEditor.tsx`, `engineers_app/src/components/home/MyHome.tsx`
- Test: `engineers_app/src/components/report/LogEntries.test.tsx` (new), `ItemRow.test.tsx`, `PlanItemsEditor.test.tsx`, `DayPanel.test.tsx`

**Interfaces:**
- Consumes: Task 7 types; `PlanProfile` (`{id, name, region, archived}`) already returned with the day read.
- Produces: `LogEntries({ entries, profiles, disabled, onChange }: { entries: LogEntry[]; profiles: PlanProfile[]; disabled: boolean; onChange: (next: LogEntry[]) => void })`. `ItemRow`'s `result` / `onChange` value type becomes `{ done: boolean; value: number | null; entries?: LogEntry[] }` and it takes an optional `profiles?: PlanProfile[]`.

- [ ] **Step 1: Write the failing tests**

`LogEntries.test.tsx`:

```tsx
it('adds an entry for the first unused profile', …)   // click "Add profile" → onChange([...entries, { profileId: <first profile not yet used, else first>, note: '' }])
it('edits a note', …)                                 // typing in "What changed" → onChange with that note
it('removes an entry', …)                             // click "Remove entry 1"
it('shows an entry without a profile as text only', …) // profileId null → no select, the note input only
it('stops at 20 entries', …)                          // with 20 entries the Add button is absent
it('archived profiles are offered only when already picked', …)
```

`ItemRow.test.tsx`: `a log item is done when it has an entry with a note` — rendering a `kind: 'log'` item and adding a note calls `onChange({ done: true, value: null, entries: [...] })`; removing the last entry → `done: false`. The checkbox of a log row is read-only (`disabled`).

`PlanItemsEditor.test.tsx`: `the kind select offers Log and a log item has no target input`.

`DayPanel.test.tsx`: `saving sends a log item's entries with empty notes dropped` — the `putDayPlan` mock receives `results[id].entries` without the blank row.

- [ ] **Step 2: Run, expect failure** — `npx vitest run src/components/report` → new tests FAIL.

- [ ] **Step 3: Implement** — `LogEntries` rows: `<select aria-label="Profile N">` + `<input aria-label="What changed N" maxLength={200}>` + remove button; "Add profile" link button. `ItemRow` renders the check-style label row followed by `LogEntries` for `kind === 'log'`. `DayPanel` and `MyHome` pass `profiles` and keep `entries` in their per-item state, dropping blank-note entries on save. `PlanItemsEditor`: `<option value="log">Log</option>`; target hidden for `log` like `check` (also in `cleanItems`). Read-only list shows nothing after the text for a log item.

- [ ] **Step 4: Run** — `npx vitest run src/components/report src/components/home && npm run lint` → PASS.

- [ ] **Step 5: Commit** — `git add src && git commit -m "feat: a log item takes one row per profile reviewed in the day panel and on Home"`

---

### Task 9: Week panel modes (view-only for the planned person, editable for the admin)

**Files:**
- Modify: `engineers_app/src/components/report/WeekPanel.tsx`, `engineers_app/src/pages/Report.tsx`
- Test: `engineers_app/src/components/report/WeekPanel.test.tsx`

**Interfaces:**
- Consumes: `WeekPlan.plansOwnWeek`, `putTeamWeek`, `putWeekPlan`.
- Produces: `WeekPanel` prop `mode: 'own' | 'admin' | 'view'` replacing `readOnly` (`own` = the signed-in person's week; `admin` = an admin on someone's week; `view` = a teammate looking).

- [ ] **Step 1: Write the failing tests**

```tsx
it('own + plansOwnWeek false: targets and items are read-only, the note "Planned by your admin" shows, recap is editable and Save sends only the recap', …)
// before the week is saved (data.exists false) the recap is read-only too and there is no Save button
it('own + plansOwnWeek true: unchanged — Save week calls putWeekPlan with goal, routines, weeklyGoals, recapNotes', …)
it('admin: fields are editable and Save calls putTeamWeek(weekStart, [one row for userId]) with plansOwnWeek as loaded; recap is read-only', …)
it('admin: a failed row shows its error and keeps the panel open', …)   // results[0].ok false, error "…" → role="alert"
it('view: everything read-only, Close only', …)
```

- [ ] **Step 2: Run, expect failure** — `npx vitest run src/components/report/WeekPanel.test.tsx` → FAIL.

- [ ] **Step 3: Implement** — `Report.tsx` passes `mode` (`admin` when `isAdmin && viewUser`, `own` when no `viewUser`, else `view`). In `admin` mode the row's goal is `{ bidsSelf, bidsBidder, interviewsSelf }` from the form's goal; the region split inputs and the caller-interview input are shown read-only with the hint "Edited by the person" (the team endpoint does not write them).

- [ ] **Step 4: Run** — `npx vitest run src/components/report src/pages/Report.test.tsx && npm run lint` → PASS.

- [ ] **Step 5: Commit** — `git add src && git commit -m "feat: the Week panel is view-only for a person the admin plans, and editable for the admin on anyone's week"`

---

### Task 10: The grid model (pure functions)

**Files:**
- Create: `engineers_app/src/lib/teamGrid.ts`
- Test: `engineers_app/src/lib/teamGrid.test.ts`

**Interfaces:**
- Consumes: `TeamWeekUser`, `TeamWeekRowIn`, `PlanItemIn`, `PlanItemKind` (Task 7).
- Produces:

```ts
export type Scope = 'routine' | 'goal';
export type TargetKey = 'bidsSelf' | 'bidsBidder' | 'interviewsSelf';
export interface Column { key: string; text: string; kind: PlanItemKind; scope: Scope }   // key = `${scope}|${kind}|${normalised text}`
export interface GridState { users: TeamWeekUser[]; pinned: Column[] }                  // pinned = columns added in this session
export const columnKey: (text: string, kind: PlanItemKind, scope: Scope) => string;     // trim, collapse spaces, lower-case
export function columns(s: GridState): Column[];            // routines first then goals; first-seen order; shared by ≥2 users, or pinned
export function ownItems(s: GridState, userId: string): { scope: Scope; item: PlanItemIn }[];  // that user's items not in any column
export function cell(s: GridState, userId: string, c: Column): { has: boolean; target: number | null };
export function setCell(s: GridState, userId: string, c: Column, v: { has: boolean; target: number | null }): GridState;
export function setTarget(s: GridState, userId: string, k: TargetKey, n: number): GridState;
export function fillColumn(s: GridState, c: Column, v: { has: boolean; target: number | null }): GridState;
export function fillTarget(s: GridState, k: TargetKey, n: number): GridState;
export function addColumn(s: GridState, text: string, kind: PlanItemKind, scope: Scope): GridState;   // pins; no cells filled
export function addOwnItem(s: GridState, userId: string, text: string, kind: PlanItemKind, scope: Scope, target: number | null): GridState;
export function removeOwnItem(s: GridState, userId: string, scope: Scope, index: number): GridState;
export function renameColumn(s: GridState, c: Column, text: string): GridState;                        // keeps item ids
export function retypeColumn(s: GridState, c: Column, kind: PlanItemKind, scope: Scope): GridState;    // drops ids (server archives + creates)
export function removeColumn(s: GridState, c: Column): GridState;
export function setOwnWeek(s: GridState, userId: string, on: boolean): GridState;
export function isEmptyRow(u: TeamWeekUser): boolean;                                                   // zero targets and no items
export function rowsToSave(saved: TeamWeekUser[], s: GridState): TeamWeekRowIn[];                       // edited rows + rows without a saved plan that are not empty
export function changed(prev: TeamWeekUser[] | undefined, s: GridState, userId: string, c: Column | TargetKey): { was: string } | null;
export function weekStatus(saved: TeamWeekUser[], s: GridState): 'published' | 'draft' | 'none';
```

Rules the tests pin: `setCell` with `has: true` on a user lacking the item appends a new item (no id) to that user's list for the column's scope; `has: false` removes it from the list (the server archives by omission). For `count` / `hours` columns `has` is `target !== null`. All functions return new objects (no mutation). A user at 20 items in a scope is left unchanged by `setCell` / `fillColumn` / `addOwnItem`.

- [ ] **Step 1: Write the failing tests** — one `it` per rule, using three users `A`, `B`, `C`:

```ts
it('same name in two people is one column; a name one person has is an own item', …)
it('names match ignoring case and extra spaces', …)                 // "Email  outreach" + "email outreach" → one column, first spelling shown
it('different kind is a different column', …)                        // "Outreach" count vs check → not merged; both are own items
it('a routine and a weekly goal with the same name are different columns', …)
it('an added column stays while only one cell is filled', …)
it('setCell adds, re-targets and removes the item for that person only', …)
it('fillColumn sets every person and skips a person already at 20 items', …)
it('renameColumn keeps each item id; retypeColumn drops ids and moves the item to the other list when the scope changes', …)
it('removeColumn removes the item from everyone who has it', …)
it('rowsToSave: untouched saved rows are left out; a draft row is included; an empty row without a plan is left out; a toggled switch alone is included', …)
it('rowsToSave sends goal as bidsSelf, bidsBidder, interviewsSelf only', …)
it('changed: a different target reads "was 600"; a newly given item reads "was —"; an unchanged cell is null; no previous week → null', …)
it('weekStatus: all non-empty rows saved and unedited → published; a draft or an edit → draft; nobody has anything → none', …)
```

- [ ] **Step 2: Run, expect failure** — `npx vitest run src/lib/teamGrid.test.ts` → FAIL.
- [ ] **Step 3: Implement `teamGrid.ts`.** "Edited" in `rowsToSave` = the row's JSON differs from the same user's row in `saved`.
- [ ] **Step 4: Run** — `npx vitest run src/lib/teamGrid.test.ts && npm run lint` → PASS.
- [ ] **Step 5: Commit** — `git add src/lib && git commit -m "feat: the team grid model derives shared columns from item names and tracks which rows to save"`

---

### Task 11: The setup grid screen

**Files:**
- Create: `engineers_app/src/components/report/SetupGrid.tsx`
- Modify: `engineers_app/src/pages/Report.tsx`
- Test: `engineers_app/src/components/report/SetupGrid.test.tsx` (new), `engineers_app/src/pages/Report.test.tsx`

**Interfaces:**
- Consumes: Task 10 (all of `teamGrid.ts`), `getTeamWeek`, `putTeamWeek`.
- Produces: `SetupGrid({ week }: { week: string })`; route `/plans?view=setup&week=<monday>` (admins only; a non-admin with `view=setup` sees their own board).

- [ ] **Step 1: Write the failing tests** (mock `../../api/endpoints`; week `2026-11-23`, previous `2026-11-16`)

```tsx
it('loads this week and last week and shows people as rows and shared items as columns', …)
it('shows the status chip: "Draft · copied from last week" when a row has no saved plan, "Published" when all saved, "Not planned yet" when empty', …)
it('typing a target marks the cell changed with "was 600"', …)
it('the Fill all row sets the column for everyone', …)
it('Add item for everyone adds a column; for one person adds a chip under "Only this person"', …)
it('the column menu renames, re-types, switches daily / weekly and removes', …)
it('Publish week sends only rowsToSave and re-reads the week', …)          // putTeamWeek called once with the edited + draft rows
it('a failed row shows its error beside the person and stays edited; saved rows are not re-sent on the next Publish', …)
it('Publish is disabled while saving and when there is nothing to save', …)
it('leaving with unsaved edits asks first', …)                              // beforeunload handler registered while dirty
it('load failure shows "Couldn\'t load the team." with Retry', …)
```

`Report.test.tsx`: `an admin sees "Plan the week" on the team table and view=setup renders the grid; staff with view=setup get their own board`.

- [ ] **Step 2: Run, expect failure** — `npx vitest run src/components/report/SetupGrid.test.tsx src/pages/Report.test.tsx` → FAIL.

- [ ] **Step 3: Implement**

Layout follows the prototype (`plan-setup-v4.html`), built with the app's existing classes (`panel`, `btn`, `btn-outline`, `input`, `card-title`, `text-muted`), not the prototype's CSS:
- Header: the page's existing week stepper, the status chip, **Publish week**.
- Table: sticky Person column; "Plans own week" checkbox column; group headers **Week targets** (Bids self, Bidder bids, Interviews) / **Daily routines** / **Weekly goals**; **Only this person** (chips with remove, "+ add"); a trailing "+ item" header button. Last row "Fill all".
- Number cells: `<input type="number" min={0}>` with `aria-label="<person> <column>"`; check / log cells: checkbox with the same label. Empty number = the person does not have the item.
- Column header is a `<button>` opening a small menu (Rename, Change type, Make weekly goal / Make daily routine, Remove from this week). Rename and add use an inline form (name ≤ 200, type select, when select, for select), not `prompt()`.
- State: `saved` (from SWR) and `GridState` (edited copy, reset whenever `saved` changes). SWR `revalidateOnFocus` is on only while there are no unsaved edits.
- Publish: `putTeamWeek(week, rowsToSave(saved, state))`, then `mutate()`; per-row errors kept in component state keyed by userId and shown under the person's name with `role="alert"`; a thrown request error shows "Could not publish the week." above the table.
- Phone width: the table scrolls horizontally inside its panel; the page does not.

`Report.tsx`: `view=setup` → `<SetupGrid week={week} />`; the admin's team-table header gets `<Link to={`/plans?week=${week}&view=setup`}>Plan the week</Link>` and the grid header gets "← Team".

- [ ] **Step 4: Run everything in the app**

Run: `npm test && npm run lint`
Expected: PASS.

- [ ] **Step 5: Look at it in the running app (read-only)**

The app is at `http://localhost:5173`, backend at `http://localhost:8080` (production data). Sign-in is the user's; ask them to open `/plans?view=setup` and confirm the grid loads with real people and last week's items. **Do not click Publish** during this check unless the user says to: it writes real plans.

- [ ] **Step 6: Commit** — `git add src && git commit -m "feat: admins plan every person's week in one grid on the Plans page and publish it in one save"`

---

### Task 12: Release notes for the user

**Files:**
- Modify: `engineers_backend/docs/superpowers/specs/2026-11-21-admin-weekly-planning-design.md` (status line → "implemented")

- [ ] **Step 1:** Run both suites once more from clean: `cd engineers_backend && .venv/bin/pytest tests_unit -q` and `cd engineers_app && npm test && npm run lint`. Expected: PASS.
- [ ] **Step 2:** Set the spec's status line and commit in the backend: `git commit -am "docs: admin weekly planning design marked implemented"`.
- [ ] **Step 3:** Report to the user, without pushing: both branch names, the two things that change on deploy (everyone becomes admin-planned until switched; the Monday plan alert stops for them), and that backend and app must ship together (the app sends `entries` and calls `/weekly-plans/team/*`).

---

## Addendum tasks (spec Design §7, approved 2026-11-21) — run before Task 12

### Task 13: Items record who added them; an admin-planned person edits only their own

**Files:**
- Modify: `engineers_backend/app/models/plan_common.py`, `engineers_backend/app/week_save.py`, `engineers_backend/app/routers/weekly_plans.py`
- Test: `engineers_backend/tests_unit/test_daily_plan_api.py`, `engineers_backend/tests_unit/test_weekly_plan_team.py`, `engineers_backend/tests_unit/test_plan_auto.py`

**Interfaces:**
- Produces: `PlanItem.addedBy: Literal["admin", "self"] = "admin"` (also on `PlanItemIn`, but the server never trusts a sent value — see rules). `save_week(..., *, added_by: Literal["admin", "self"], locked: bool = False)` — `added_by` is what new items get; `locked=True` is the admin-planned person's save.
- Every week read (`GET /weekly-plans/{week}`, team read, day items) returns `addedBy` per item.

Rules (spec §7):
- Known id → keeps its stored `addedBy`, whoever saves. New item → `added_by` of the save (team save: `"admin"`; person save: `"self"`). A kind change (remove + add) keeps the old item's `addedBy` on the new item.
- `locked=True` (person, `plansOwnWeek` false, week has a saved plan): goal taken as stored; every stored `"admin"` item kept as stored, in place, whatever the body says (not archived when missing, not renamed / re-targeted / retyped when sent changed); `"self"` items follow the body; sending an `"admin"` item's id with changes is not an error (ignored). The 20-item limit applies to the merged list → 422 with the existing limit message. Week without a saved plan → 403 as today. Recap as today.
- Carry-over draft keeps `addedBy`.

- [ ] **Step 1: Failing tests**

```python
# test_plan_common / test_plan_auto
def test_added_by_defaults_to_admin_and_carries():  # PlanItem(text="x").addedBy == "admin"; _draft keeps "self" on a routine

# test_daily_plan_api (planned = plansOwnWeek False, week saved by the admin with routine a1 "admin")
async def test_planned_person_adds_own_item(db, planned):       # body routines [a1 as stored, {text "Discord", kind check}] → new item addedBy "self", a1 unchanged
async def test_planned_person_cannot_change_admin_items(db, planned):  # body renames a1, drops it, changes goal → a1 unchanged & live, goal unchanged
async def test_planned_person_edits_and_archives_own(db, planned):     # own item s1: rename kept; omitted later → archived
async def test_planned_person_limit_counts_both(db, planned):   # 15 admin + 6 own routines → 422
async def test_own_week_person_new_items_are_self(db, me):      # plansOwnWeek True: new item addedBy "self", can edit admin items

# test_weekly_plan_team
async def test_team_save_keeps_added_by(db, admin, ana):        # ana has own item s1 ("self"); team save sends it (renamed) → still "self", renamed; new item → "admin"
async def test_team_read_returns_added_by(db, admin, ana):
```

- [ ] **Step 2: Run, expect failure** — `.venv/bin/pytest tests_unit/test_daily_plan_api.py tests_unit/test_weekly_plan_team.py tests_unit/test_plan_auto.py tests_unit/test_plan_common.py -q`
- [ ] **Step 3: Implement** — field; `_merge_items` gains the `added_by` / `locked` handling (one place); `put_week` calls `save_week(..., goal=None, routines=body.routines, weekly_goals=body.weeklyGoals, recap=body.recapNotes, added_by="self", locked=True)` for an admin-planned person with a saved plan; `plansOwnWeek` true → `added_by="self"`; team save → `added_by="admin"`. Update the 403-recap docstring / comments.
- [ ] **Step 4: Run** the Step 2 files, then the full suite (constraints.md) — only known env failures.
- [ ] **Step 5: Commit** — `feat: plan items record who added them and an admin-planned person can add and edit only their own`

---

### Task 14: Week panel — the planned person's own items

**Files:**
- Modify: `engineers_app/src/api/endpoints.ts`, `engineers_app/src/components/report/WeekPanel.tsx`, `engineers_app/src/components/report/PlanItemsEditor.tsx`
- Test: `engineers_app/src/components/report/WeekPanel.test.tsx`, `engineers_app/src/components/report/PlanItemsEditor.test.tsx`

**Interfaces:**
- Consumes: Task 13 (`addedBy` on every item from week reads).
- Produces: `PlanItem.addedBy: 'admin' | 'self'` (`PlanItemIn.addedBy?`); `PlanItemsEditor` prop `lockedFor?: (item: PlanItemIn) => boolean` — locked rows render read-only with a 🔒 and no archive button; unlocked rows edit as today; new rows are `addedBy: 'self'`.

Behaviour (spec §7, prototype `person-view-v3.html` case A):
- `own` mode, `plansOwnWeek` false, week exists: note "Planned by your admin"; targets read-only; routines and weekly goals use `PlanItemsEditor` with `lockedFor = (i) => i.addedBy !== 'self'`; "+ Add item" adds the person's own; Save sends the whole week (`putWeekPlan`) with recap. Week not saved: view only, as today.
- `own` mode, `plansOwnWeek` true, and `admin` / `view` modes: unchanged.

- [ ] **Step 1: Failing tests**

```tsx
it('planned person: admin items are locked, own items editable, Add item adds an own item, Save sends both', …)  // putWeekPlan body routines include the locked item unchanged + the new { addedBy: 'self' }
it('planned person before the week is published: no Add item, no Save', …)
it('PlanItemsEditor: a locked row has no inputs and no archive button', …)
```

- [ ] **Step 2: Run, expect failure** — `npx vitest run src/components/report`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — `npx vitest run && npm run lint`
- [ ] **Step 5: Commit** — `feat: a person the admin plans can add and edit their own items in the Week panel`

---

### Task 15: Setup grid — a person's own items

**Files:**
- Modify: `engineers_app/src/lib/teamGrid.ts`, `engineers_app/src/components/report/SetupGrid.tsx`
- Test: `engineers_app/src/lib/teamGrid.test.ts`, `engineers_app/src/components/report/SetupGrid.test.tsx`

**Interfaces:**
- Consumes: Task 14 types.
- Produces: `columnKey` / column derivation exclude `addedBy: 'self'` items — they are always own items (`ownItems` returns them with `addedBy`), never part of a shared column, even when an admin item or another person's own item has the same name. `rowsToSave` sends them back with their id unchanged.

- [ ] **Step 1: Failing tests**

```ts
it('a person\'s own item never joins a column, even with the same name as an admin column', …)
it('own items keep their id in rowsToSave and are not sent when untouched', …)
```
```tsx
it('an own item chip reads "added by <name>" and can still be removed by the admin', …)
```

- [ ] **Step 2: Run, expect failure** — `npx vitest run src/lib/teamGrid.test.ts src/components/report/SetupGrid.test.tsx`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — `npx vitest run && npm run lint`
- [ ] **Step 5: Commit** — `feat: the setup grid shows a person's own items as their chips, never as shared columns`
