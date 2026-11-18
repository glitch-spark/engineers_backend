# Plans: routines, weekly goals with numbers, and the 6 PM standup

Date: 2026-11-17. Status: draft for approval. Repos: `engineers_backend` (data, API, Slack) and `engineers_app`
(Weekly / Daily plan screens).

## Why

People don't know what belongs in the weekly goal vs the daily goal. Production data (last 3 weeks, every user) shows:

- Weekly bid numbers are the daily numbers × 5, typed by hand every week. Interview targets are only ever weekly.
- Most goal lines are routines ("Bidder supervision", "Interview prep: 2h", "Newsela work"), typed twice (weekly lines
  and daily lines), identical every day and usually the next week too.
- Real weekly outcomes ("Company Facebook create", "Tessera Labs background check", "Email outreach ×250") are mixed
  in with routines and rarely ticked.
- Many items are counts written as text ("Send 50 emails"); the actual result ends up in notes ("connected 31 UK
  citizens") or as a bare ✓/✗.
- Daily goals are almost never edited; the day copies the weekly default.
- Nothing reminds people to log the day; the standup post appears only when someone saves a follow-up.

## Goals

1. One place to plan the week: **targets** (bids self by region + bidder, interviews), **routines** (recurring
   daily tasks) and **weekly goals** (one-off outcomes for the week). No separate "daily goal" entry.
2. Items carry real numbers: a **check** (✓/✗), a **count** (target, done) or **hours** (target, done).
3. Daily targets are derived (weekly ÷ the week's working days); a day may override them.
4. Weekly goals with numbers add up across the week from each day's entries (e.g. 180 / 250 by Thursday).
5. Routines and targets carry over to the next week until removed.
6. **6:00 PM Central**: one message in the daily standup channel (`SLACK_DAILY_PLAN_CHANNEL_ID`) with **Write my
   update** (Slack form, pre-filled per person) and **Open in app** (the same draft on the Daily plan page).
7. The standup post becomes **Goal vs Done** (layout below), posted or edited in place whichever way it was filled in.

## Non-goals

- No change to how self bids and interviews are counted (tracked self bids, interview records). Bidder bids change
  source, see "Calls made by the designer" 1.
- No DMs. No per-person posts at 6 PM.
- No change to metrics / dashboards that read the done counts (`done.bids*`, `bidsSelfByProfile`).

## Anti-goals (failure even if it works)

- Two standup posts for the same person and day (one from Slack, one from the app).
- A person's pre-filled numbers visible to others before they submit.
- Old weekly / daily plans losing their goal lines in the migration.
- The 6 PM message posted twice, or on days no one works (see Constraints).

## Constraints

- Week = Monday–Sunday (existing `monday_of`). **Working days are Monday–Friday** (user decision): derived targets
  (weekly ÷ 5), routines and the 6 PM nudge appear Monday–Friday only. Saturday / Sunday can still be opened in the
  app and logged as extra work (no targets or routines unless set for that day); saving posts a standup as usual.
- Times are America/Chicago. The 6 PM message runs from the existing cron (keen-spirit, every 5 min), claimed once
  per day like the other Slack posts (`SlackAlertLog`), Monday–Friday.
- Slack form limits: a modal holds at most 100 blocks; inputs are plain text / number inputs and checkboxes.
- The existing standup post code (`slack_bot.daily_plan_text` / `sync_daily_plan_message`) is the single writer of
  the post; the Slack form and the app both save through the daily-plan write path, which calls it.

## Approaches considered

- **A. Extend the existing weekly / daily plans (picked).** WeeklyPlan gets typed `routines` and `weeklyGoals`
  (replacing `goalItems` / `dailyGoalItems`); DailyPlan stores per-item results and optional target overrides;
  daily targets derived. Reuses posting, in-place edits, interview counts and tracked self bids.
- B. Separate "plan items" collection living across weeks with a daily entry log. Cleanest model for "until removed",
  but new APIs, bigger migration, two homes for plan data.
- C. Add numbers to today's goal lines only. Fastest, but keeps the double entry and ×5 arithmetic that cause the
  confusion.

## Design

### 1. Data (backend `app/models`)

`PlanItem` (new, in `plan_common.py`): `id` (stable string), `text` (≤ 200), `kind` = `check | count | hours`,
`target` (number, optional; required for count / hours goals that should show "x / target").

WeeklyPlan:
- `goal: Counts` stays: the week's targets (bids by region + bidder, interviews self / caller).
- `routines: list[PlanItem]` — every working day shows them. Count / hours routines have a **per-day** target.
- `weeklyGoals: list[PlanItem]` — one-off outcomes; count / hours goals have a **per-week** target. A check goal is
  done when any day marks it done.
- `dailyGoal`, `dailyGoalItems`, `goalItems` — removed after migration (kept readable by the legacy fold until then).
- `recapNotes` stays.

DailyPlan:
- `targetOverride: Optional[Counts]` — the day's own targets; absent = derived from the week.
- `results: dict[item id → {done: bool, value: number}]` — per routine / weekly goal; `value` for count / hours.
- `focus: list[item id]` — weekly goals the person worked on today (shown in the post; set by entering a value).
- `done`, `notes`, `loggedAt`, `slackChannel`, `slackTs` stay. `goal` / `goalItems` removed after migration.

Derived (not stored): day targets = `targetOverride` or week `goal` ÷ 5 (bids; rounded), Mon–Fri; weekly goal
progress = sum of the week's daily `results[id].value` (count / hours) or any `done` (check).

### 2. Migration (one script, idempotent, dry run first)

- Weekly: each `dailyGoalItems` line → a routine; each `goalItems` line not matching a routine (case / whitespace
  insensitive) → a weekly goal. Numbers are parsed out where the text has them: `x 250`, `×250`, `250 contacts`
  → count target; `2h`, `30min` → hours target (0.5). Unparsed → check.
- Daily: each `goalItems` line → `results` of the matching routine / goal id (done ✓/✗ kept); unmatched lines →
  appended to that week's weekly goals. Daily `goal` differing from the week's `dailyGoal` → `targetOverride`.
- Legacy fold (`fold_legacy_weekly` / `fold_legacy_daily`) keeps reading unmigrated docs until the script ran.

### 3. Carry-over

Creating a week (first open of the weekly plan, or the 6 PM draft for a week with no plan) copies the previous week's
`goal` and `routines` (new ids not needed: ids are stable per item and reused). Weekly goals are not copied.

### 4. API (backend routers)

- `GET/PUT /weekly-plans` gain `routines`, `weeklyGoals`; lose `dailyGoal*` / `goalItems` (app ships together).
- `GET /daily-plans` returns per day: derived targets, routines + weekly goals with that day's results, weekly goal
  progress, done counts (as today), interviews (as today).
- `PUT /daily-plans/{day}` saves results, focus, override, notes, done; sets `loggedAt`; posts / edits the standup
  post (existing `_sync_slack`).
- Same write path is used by the Slack form submission.

### 5. 6 PM standup nudge (backend, cron)

- New tick step: at ≥ 18:00 Chicago, Monday–Friday, once per day (claim key `standup-nudge:<YYYY-MM-DD>`), post in
  `SLACK_DAILY_PLAN_CHANNEL_ID`:
  ```
  🗒️ Daily standup — Tue 11/17
  Time to log today. Your draft is ready: bids, interviews and your plan are filled in.
  [ Write my update ]   [ Open in app ]
  ```
- **Write my update** (`standup_write`) opens a modal for the clicker (platform user by Slack member id; unlinked →
  ephemeral "link your Slack ID in Profile"). Pre-filled: bids done (tracked self bids, approved bidder bids — read
  only, shown as context with an "adjust" number input for self bids on days the profile isn't tracked), interviews
  (read only), each routine (checkbox, or number input with target), weekly goals (number input / checkbox, optional),
  Extra (multiline). Already logged today → same modal pre-filled with what was saved ("Edit today's update").
- **Open in app** → `/plans?day=<YYYY-MM-DD>` (Daily plan page with the draft).
- Submission → daily-plan write path → post / edit the person's standup post. Errors shown in the modal.

### 6. Standup post: Goal vs Done (`slack_bot.daily_plan_text`)

```
🗒️ @Puma · Daily standup · Tue 11/17
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                    Done / Goal
1️⃣ Bids
   Self            134 / 120   ✅  (US 91/80 · LATAM 43/40)
   Bidder          144 / 150   ⚠️
2️⃣ Interviews
   Self              2   Intro(1) · Tech round 1(1)
   Caller            1   Intro(1)
   Week              3 / 15
3️⃣ Routines
   Bidder review     ✅
   Citizen contact   31 / 50   ⚠️
   Interview prep   1.5h / 2h
4️⃣ Weekly goals (week so far)
   Email outreach   180 / 250
   Company Facebook  ✅ done today
5️⃣ Extra
   Company onboarding: 4h · Discord server: 1.5h
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔗 Open Daily plan
```
✅ = met target, ⚠️ = short; no mark without a target. Weekly goals list only those touched today (in `focus` or with
a result today). Empty sections are left out.

### 7. App screens (`engineers_app/src/components/report/*`)

- **Weekly plan**: three blocks — Targets (bids by region + bidder, interviews), Routines (add / remove; kind; per-day
  target), Weekly goals (add / remove; kind; week target; progress bar). "Copied from last week" note on new weeks.
  `DailyGoalCard` / daily goal inputs removed.
- **Daily plan**: Today's targets (derived; "change for today" opens override), Routines (checkbox or number vs
  target), Weekly goals (optional entry, shows week progress), Done counts (as today), Extra notes, Save = post.

## Calls made by the designer (from the builder check)

1. **Bidder done is automatic (user decision): Bid Track approved + pending** bids of the owner's bidders for that
   report day, read only in the app and the form. It is written through into `done.bidsBidder` whenever a bidder bid
   is uploaded or reviewed (same pattern as `self_bids.write_through`; a rejected bid drops out), so dashboards,
   metrics, reports and the post keep reading `done.bidsBidder`. Days before this ships keep their typed value. Self
   bids: tracked profiles come from the existing write-through; untracked profiles get one number input each,
   pre-filled with the day's typed value, so `bidsSelfByProfile` and the region totals keep working.
2. **Submitted days freeze their targets.** On submit the day stores its targets (`targetOverride` filled with the
   derived values) and each item's target in its result, so later edits to the week never rewrite a submitted day.
   Derived bid targets round **up** (`ceil`, as `alerts.daily_bids_target`). The migration fills `targetOverride`
   from every existing day's stored goal, so no existing day's numbers change. `reports` `itemsDone / itemsTotal`
   are redefined over routines + weekly goals touched that day; migrated days keep their counts.
3. **New `submittedAt`** marks a person's own submission (app Save or Slack form). `loggedAt` keeps its current
   meaning (set by the self-bids write-through too) so existing logging metrics don't change. The form opens as
   "Edit today's update" only when `submittedAt` is set; the standup post is posted on first submit, not by the
   write-through.
4. **"Visible to others"** means the 6 PM channel message carries no per-person numbers. In-app read access is
   unchanged.
5. **Editing items mid-week**: removing a routine or weekly goal archives it (hidden from new days, kept for days
   that have results); target changes apply from the next unsubmitted day (submitted days keep their stored target).
6. **Carry-over fills the draft, it does not save a week.** A week with no plan shows last week's targets and
   routines as a pre-filled draft (app and form) until the person saves; the Monday "add this week's plan" alert and
   dashboard attainment keep working off saved plans.

## Builder checks

- Readers of `goal` / `goalItems` / `dailyGoal*`: `alerts_service`, `plan_auto`, `routers/{daily_plans, dashboard,
  metrics, reports, weekly_plans}`, `slack_bot`, app `report/*` and `api/endpoints.ts`. Each must move to the new
  fields or derived targets; metrics / dashboard numbers must not change for existing days.
- The self-bids write-through (`app/self_bids.py`) writes `done.*` into DailyPlan; results / focus must survive it.

## Left to the builder

- Module layout, endpoint shapes beyond the fields above, modal block layout within Slack limits.
- Exact number parsing rules in the migration beyond the examples (log every line it couldn't parse).
- Tests: derived targets (Mon–Fri, override), weekly goal sums, carry-over, migration on real-shaped samples, nudge
  once per weekday at 18:00 Chicago, modal pre-fill (new / already logged / unlinked), submission → one post edited in
  place, post layout (marks, empty sections, focus filter).
