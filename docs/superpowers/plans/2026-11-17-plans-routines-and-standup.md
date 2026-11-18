# Plans: Routines, Weekly Goals and the 6 PM Standup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the duplicated weekly/daily goals with week targets + routines + weekly goals that carry numbers,
derive daily targets, auto-fill bidder bids, post a Goal vs Done standup, and nudge everyone at 6 PM Central with a
Slack form or an app link.

**Architecture:** Extend `WeeklyPlan` / `DailyPlan` (typed `PlanItem`s, per-day `results`), put derivation in
`app/plan_auto.py`, one shared day-save function used by the app router and the Slack form, a bidder write-through
like `self_bids`, a new `app/standup/` package (nudge + modal) wired into the cron tick and the Slack interactions
router, and a reworked Plans page in the app.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor, Slack Block Kit + modals; React + TypeScript + Vite, vitest.

**Spec:** `docs/superpowers/specs/2026-11-17-plans-routines-and-standup-design.md`

## Global Constraints

- Working days Mon–Fri. Derived bid targets: `ceil(week target / 5)` per field, Mon–Fri only; weekend days 0 unless
  overridden. Interviews have no daily target (week figure only).
- Times America/Chicago. Nudge ≥ 18:00, Mon–Fri, once per day, key `standup-nudge:<YYYY-MM-DD>` (`SlackAlertLog` via
  `slack_bot._reminder_claim`), in `SLACK_DAILY_PLAN_CHANNEL_ID`.
- `done.bidsBidder` = Bid Track approved + pending bids of the owner's bidders for the report day (read only); days
  before release keep the typed value. Self bids per profile as today.
- `submittedAt` = the person's own submit (app Save / Slack form). `loggedAt` keeps its meaning. The standup post is
  posted / edited only for days with `submittedAt`.
- Submitted days are frozen: `targetOverride` and each result's `target` are stored on submit.
- Removing an item archives it (`archived: true`); archived items stay for days that have results.
- Carry-over only pre-fills (GET returns a draft built from last week); nothing saved until the user saves.
- Metrics / dashboard / alerts keep reading `WeeklyPlan.goal` and `done.*` — numbers for existing days unchanged.
- Work in the main checkouts on branch `feat/plans-routines-standup` (backend → PR into `dev`, app → PR into `main`).
  Commits single-line, no co-author trailer, only when the user approves committing.
- Release together: the plans API changes shape (Task 4) and the app (Tasks 9–10) must deploy with it; run the
  migration (Task 5, `--apply`) right after the backend deploy and set `plan_bidder.RELEASE_DAY` to the deploy day.
  Dropping the deprecated fields (`goalItems`, `dailyGoal*`, daily `goal`) is a later cleanup, not in this plan.
- Backend tests: `.venv/bin/python -m pytest -q -p no:cacheprovider tests_unit/<file>`. App: `npx vitest run <path>`,
  `npx tsc -b --noEmit`, node tests via `npx tsx --test tests/*.test.mjs`.

## Review Focus

1. A day submitted from Slack, then edited in the app (or the reverse): one post, edited in place (Task 6/8 test).
2. A routine archived mid-week: earlier days still show its result in the post and API (Task 2/4 test).
3. Week target edited on Wednesday: Monday/Tuesday (submitted) keep their targets (Task 4 test).
4. Person with no Slack id clicks Write my update: ephemeral "link your Slack ID in Profile", no modal (Task 8 test).
5. Self bids write-through after submit keeps `results`, `focus`, `notes` (Task 3 test).

---

### Task 1: Models — PlanItem, results, new plan fields

**Files:** Modify `app/models/plan_common.py`, `app/models/weekly_plan.py`, `app/models/daily_plan.py`.
Test: `tests_unit/test_plan_common.py`.

**Interfaces — Produces:**
- `PlanItem(BaseModel)`: `id: str` (default `uuid4().hex[:12]`), `text: str` (1–200, stripped), `kind:
  Literal["check","count","hours"] = "check"`, `target: Optional[float] = None` (≥ 0; ignored for check),
  `archived: bool = False`.
- `ItemResult(BaseModel)`: `done: bool = False`, `value: Optional[float] = None` (≥ 0), `target: Optional[float]`.
- `WeeklyPlan.routines: list[PlanItem] = []`, `WeeklyPlan.weeklyGoals: list[PlanItem] = []` (old `goalItems`,
  `dailyGoal`, `dailyGoalItems` stay declared until Task 5 ran; marked deprecated in comments).
- `DailyPlan.targetOverride: Optional[Counts] = None`, `results: dict[str, ItemResult] = {}`, `focus: list[str] = []`,
  `submittedAt: Optional[datetime] = None`.

- [ ] Step 1: tests — `test_plan_item_defaults_and_limits` (id generated, text stripped, >200 rejected, negative
  target rejected); `test_daily_plan_new_fields_default_empty`; `test_legacy_docs_still_load` (a raw dict with only
  old fields validates).
- [ ] Step 2: run, expect FAIL. Step 3: implement. Step 4: run, expect PASS (+ `tests_unit/test_plan_*.py`).
- [ ] Step 5: commit `feat(plans): plan items, per-day results and submit marker on plan models`

### Task 2: Derivation — day targets, week items, progress, carry-over draft

**Files:** Modify `app/plan_auto.py`. Test: `tests_unit/test_plan_auto.py`.

**Interfaces — Produces:**
- `day_targets(plan: Optional[DailyPlan], weekly: Optional[WeeklyPlan], day: date) -> Counts` — `plan.targetOverride`
  if set; else Mon–Fri `ceil(weekly.goal.<bid field> / 5)` for every bid field (interviews 0); weekend / no week → 0.
- `day_items(weekly, plan, day) -> list[dict]` — `{item: PlanItem, scope: "routine"|"goal", result: ItemResult|None}`:
  non-archived routines on Mon–Fri, all non-archived weekly goals; plus archived items that have a result on this day.
  A routine's per-day `target` comes from `result.target` when present (frozen), else the item's.
- `goal_progress(weekly, days: list[DailyPlan]) -> dict[str, dict]` — per weekly goal id: `{"value": sum of values,
  "done": any done, "target": item.target}`.
- `async draft_week(uid, monday: date) -> Optional[WeeklyPlan]` — unsaved WeeklyPlan copying the previous week's
  `goal` and non-archived `routines` (same ids), no weekly goals; None when no previous week.
- `day_goal` is replaced by `day_targets`; update its callers in later tasks.

- [ ] Step 1: tests — `test_targets_ceil_divide_mon_to_fri` (goal bidsSelf 601 → 121 Mon, 0 Sat);
  `test_override_wins`; `test_no_week_is_zero`; `test_archived_routine_kept_where_it_has_a_result`;
  `test_frozen_result_target_wins_over_item`; `test_goal_progress_sums_counts_and_any_done`;
  `test_draft_week_copies_goal_and_routines_not_goals`; `test_draft_week_none_without_previous`.
- [ ] Steps 2–4: FAIL → implement → PASS. Step 5: commit `feat(plans): derive day targets, items, goal progress and week draft`

### Task 3: Bidder bids write-through

**Files:** Create `app/plan_bidder.py`. Modify `app/routers/bidder_ext.py` (after `record_screenshot` creates a bid),
`app/routers/bids.py` (`review_bid`, `review_bids` after their recounts), `app/self_bids.py` (keep `results`,
`focus`, `notes` — it already writes only `done.*`; assert in test). Test: `tests_unit/test_plan_bidder.py`.

**Interfaces — Produces:**
- `async bidder_done(owner_id, day: date) -> int` — approved + pending bids (kind bidder) of bidders with
  `createdBy == owner_id`, report day `day` (`profile_metrics.bid_report_day`, `schedule_from_settings()`).
- `async write_through_bidder(owner_id, day: date) -> None` — `$set done.bidsBidder` on that owner's DailyPlan for
  `day` (upsert with `$setOnInsert` year/weekNumber/createdAt like `self_bids._write`; never sets `loggedAt`), only
  for days ≥ `RELEASE_DAY` (module constant, set at release). Then, if the plan has `submittedAt` and `slackTs`,
  re-render its post (reuse `self_bids.safe_write_through`'s Slack branch). Never raises (logs).

- [ ] Step 1: tests — `test_counts_approved_and_pending_not_rejected`; `test_report_day_cutoff_respected`;
  `test_creates_plan_without_logged_at`; `test_before_release_day_untouched`; `test_keeps_results_focus_notes`;
  `test_review_and_upload_call_it` (patch `write_through_bidder`, hit both paths).
- [ ] Steps 2–4: FAIL → implement → PASS (+ `tests_unit/test_bids*.py tests_unit/test_bidder_ext*.py`).
- [ ] Step 5: commit `feat(plans): bidder bids done follow Bid Track (approved + pending)`

### Task 4: Plans API — weekly items, day draft, shared save

**Files:** Modify `app/routers/weekly_plans.py`, `app/routers/daily_plans.py`, `app/routers/reports.py`,
`app/alerts_service.py` (daily target via `day_targets`), `app/routers/metrics.py` (`bidsTarget` via `day_targets`).
Create `app/plan_save.py`. Tests: `tests_unit/test_daily_plan_api.py` (new), existing plan / report / metrics tests.

**Interfaces — Produces:**
- `WeekIn`: `goal: CountsIn`, `routines: list[PlanItemIn]`, `weeklyGoals: list[PlanItemIn]`, `recapNotes`.
  `PlanItemIn` = PlanItem fields with optional `id` (server assigns when missing; unknown ids rejected 422).
  Items left out of the PUT that exist → `archived = True` (never deleted).
- `GET /weekly-plans/{week}`: saved plan, or `{"draft": true, ...draft_week}` when none; response carries
  `routines`, `weeklyGoals`, `progress` (from `goal_progress`).
- `GET /daily-plans/{day}` and each day in `GET /daily-plans`: adds `targets` (Counts), `items` (from `day_items`
  as `{id, text, kind, scope, target, result}`), `progress`, `submitted: bool`, `bidsBidderAuto: int`.
- `DayIn`: `results: dict[str, ItemResultIn]`, `focus: list[str]`, `done: {bidsSelfByProfile}` (bidder ignored),
  `notes`, `targetOverride: Optional[CountsIn]`.
- `async plan_save.save_day(user: UserExtra, when: date, body: DayIn) -> DailyPlan` — the body of today's `put_day`
  plus: results only for item ids that exist that day; freeze `targetOverride` (body value or `day_targets`) and each
  result's `target`; `done.bidsBidder = await bidder_done(...)`; set `submittedAt` (first time) and `loggedAt` (if
  unset); then `_sync_slack`. `put_day` calls it. Weekend days accepted (extra work).
- `reports`: `itemsDone` = results done or value ≥ target; `itemsTotal` = items with a result that day; `goal` =
  `day_targets(...)`.

- [ ] Step 1: tests — `test_put_week_assigns_ids_and_archives_missing`; `test_get_week_without_plan_returns_draft`;
  `test_get_day_has_targets_items_progress`; `test_save_day_freezes_targets`; `test_week_target_edit_keeps_submitted_days`;
  `test_save_day_ignores_unknown_item_ids`; `test_save_day_sets_submitted_and_posts_once`;
  `test_bidder_done_is_auto_not_typed`; `test_archived_routine_result_still_returned`; `test_reports_items_counts`;
  update existing tests that send `goalItems` / `dailyGoal*`.
- [ ] Steps 2–4: FAIL → implement → PASS (`tests_unit/test_*plan*.py tests_unit/test_report*.py tests_unit/test_metrics*.py tests_unit/test_alerts*.py tests_unit/test_dashboard*.py`).
- [ ] Step 5: commit `feat(plans): weekly routines and goals API, day draft and shared day save`

### Task 5: Migration script

**Files:** Create `scripts/migrate_plans_routines.py`. Test: `tests_unit/test_migrate_plans_routines.py`.

**Interfaces — Produces:** `parse_item(text) -> PlanItem` (`x 250`/`×250`/`250 <word>` → count 250; `2h`, `1.5h`,
`30min` → hours 2 / 1.5 / 0.5; else check; numbers stay in the text); `async migrate(apply: bool) -> dict` (counts
+ unparsed lines logged). Weekly: `dailyGoalItems` → routines; `goalItems` not matching a routine (casefold, collapsed
spaces) → weeklyGoals. Daily: each line → `results[id]` (done kept) of the matching item, unmatched → appended to that
week's weeklyGoals; `targetOverride` = the day's stored `goal` when it has bids; `submittedAt = loggedAt`.
Idempotent (skips docs that already have `routines`/`results`). Dry run by default; `--apply` writes.

- [ ] Step 1: tests — `test_parse_item_examples` (the spec's examples + "Send 50 emails to citizens in db" → count
  50); `test_week_lines_split_into_routines_and_goals`; `test_day_lines_map_to_results`; `test_unmatched_day_line_becomes_goal`;
  `test_existing_day_numbers_unchanged` (`day_targets` before == after); `test_idempotent`.
- [ ] Steps 2–4. Step 5: commit `feat(plans): migrate goal lines into routines and weekly goals`

### Task 6: Goal vs Done standup post

**Files:** Modify `app/slack_bot.py` (`daily_plan_text`, `sync_daily_plan_message` callers), `app/routers/daily_plans.py`
(`_sync_slack` only when `submittedAt`). Test: `tests_unit/test_slack_daily_plan.py`.

**Interfaces — Consumes:** `day_targets`, `day_items`, `goal_progress`. **Produces:** `daily_plan_text(plan, owner,
*, done, rounds, targets, items, progress, week_interviews)` rendering the spec §6 layout exactly: header
`🗒️ <@owner> · Daily standup · Tue 11/17`, "Done / Goal" column, ✅ met / ⚠️ short / no mark without target,
Interviews with `Week n / target`, Routines, Weekly goals (only touched today: in `focus` or result today) as
"(week so far)", Extra; empty sections left out; numbered keycaps as today.

- [ ] Step 1: tests — `test_goal_vs_done_layout` (spec example verbatim), `test_marks`, `test_hours_format`
  (`1.5h / 2h`), `test_only_touched_goals_listed`, `test_empty_sections_left_out`, `test_no_post_until_submitted`.
- [ ] Steps 2–4. Step 5: commit `feat(standup): Goal vs Done daily standup post`

### Task 7: 6 PM nudge

**Files:** Create `app/standup/__init__.py`, `app/standup/nudge.py`; modify `app/slack_bot.py` (`run_alert_tick`
calls it, isolated try/except, reported as `standup_nudge`). Test: `tests_unit/test_standup_nudge.py`.

**Interfaces — Produces:** `STANDUP_WRITE = "standup_write"`; `nudge_blocks(day: date, origin: str) -> (text, blocks)`
(text "🗒️ Daily standup — Tue 11/17", line "Time to log today. Your draft is ready: bids, interviews and your plan
are filled in.", buttons Write my update (`STANDUP_WRITE`, value day ISO) and Open in app (URL
`{origin}/plans?day=<day>`)); `async run_standup_nudge(now) -> str` → `"off" | "skipped" | "posted" | "failed"`.

- [ ] Step 1: tests — `test_posts_once_at_six_pm_chicago`, `test_not_before_six`, `test_not_on_weekends`,
  `test_off_without_channel_or_token`, `test_failed_post_releases_claim`, `test_tick_reports_it`.
- [ ] Steps 2–4. Step 5: commit `feat(standup): 6 PM nudge in the daily standup channel`

### Task 8: Slack form — Write my update

**Files:** Create `app/standup/modal.py`; modify `app/routers/slack_bot.py` (route `STANDUP_WRITE` clicks and
`STANDUP_VIEW` submissions). Test: `tests_unit/test_standup_modal.py`.

**Interfaces — Consumes:** `plan_save.save_day`, `day_targets`, `day_items`, `bidder_done`, `owner_profiles`,
`actions.slack_platform_user`, `actions.open_modal`. **Produces:** `STANDUP_VIEW = "standup_update"`;
`async build_view(user, day) -> dict` (≤ 100 blocks; title "Daily standup", submit "Post update"; context lines for
bids and interviews; one number input per untracked profile; per item a checkbox (check) or number input with
"/ target" hint; Extra multiline prefilled with notes; private_metadata `{"day": ISO}`; "Edit today's update" title
when submitted); `read_submission(view) -> DayIn`; `async process_standup_click(slack_user_id, trigger_id, day,
response_url)`; `async process_standup_submission(view, slack_user_id)`; `submission_errors(view) -> dict` (numbers
≥ 0, ≤ 100000).

- [ ] Step 1: tests — `test_view_prefilled_from_draft`, `test_view_for_submitted_day_says_edit`,
  `test_unlinked_user_gets_ephemeral`, `test_submission_saves_and_posts_once`, `test_slack_then_app_edits_same_post`,
  `test_bad_number_returns_field_error`, `test_view_block_limit` (40 items → ≤ 100 blocks, overflow note).
- [ ] Steps 2–4 (+ `tests_unit/test_slack_interactions.py`). Step 5: commit `feat(standup): Slack form to write the daily update`

### Task 9: App — Weekly plan screen

**Files (app):** Modify `src/api/endpoints.ts` (types `PlanItem`, `ItemResult`, week/day shapes from Task 4),
replace `src/components/report/WeekGoalCard.tsx` with Targets / Routines / Weekly goals blocks, delete
`DailyGoalCard.tsx`, update `WeekPanel.tsx`, `ChecklistEditor.tsx` (item kind + target). Tests next to components.

**Interfaces:** `<PlanItemsEditor items scope onChange />` (add / edit / archive; kind select; target input shown for
count / hours; "per day" vs "this week" label by scope); draft banner "Copied from last week — save to keep".

- [ ] Step 1: tests — editor adds/archives/sets kind+target; draft banner shown for `draft: true`; save sends
  routines/weeklyGoals without client ids for new items; goal progress bar `180 / 250`.
- [ ] Steps 2–4 (`npx vitest run src/components/report`, `npx tsc -b --noEmit`). Step 5: commit `feat(plans): weekly targets, routines and weekly goals`

### Task 10: App — Daily plan screen

**Files (app):** Modify `src/components/report/DayPanel.tsx`, `DayRow.tsx`, `GoalDone.tsx`, `GoalDoneFields.tsx`,
`src/pages/Report.tsx` (open `?day=YYYY-MM-DD`). Tests next to components.

**Interfaces:** targets (with "change for today" override), bidder bids read-only (`bidsBidderAuto`), self bids per
untracked profile, routine / goal rows (checkbox or number vs target, week progress for goals), Extra, Save =
"Post update" (`submitted` → "Update post").

- [ ] Step 1: tests — renders targets/items from API shape; number vs target shows 31 / 50; bidder field not
  editable; `?day=` opens that day; save body has results/focus/notes only for shown items.
- [ ] Steps 2–4 (+ full `npx vitest run`, node tests via tsx). Step 5: commit `feat(plans): daily plan with routines, goals and auto counts`
