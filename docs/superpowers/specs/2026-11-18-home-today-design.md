# Home: today's to-dos, this week, a few metrics — design

Date: 2026-11-18. Repos: engineers_backend (FastAPI) + engineers_app (React/Vite). Builds on
`2026-11-17-plans-routines-and-standup-design.md` (routines, weekly goals, derived targets, 6 PM standup).

## Why

Home (`/dashboard`, the landing page after sign-in) is a metrics page few people use. Daily work — routines, weekly
goals, today's targets — lives only on the Daily plan page. Home should be where a teammate starts the day and keeps
ticking things off: today's to-dos, the week's to-dos with progress, and only the metrics that matter.

Success: people tick routines and type progress on Home during the day; the 6 PM update then has little left to fill.

## Goals

- Today: targets, routines and focused weekly goals, **tickable / editable in place**, saved without posting to Slack.
- This week: bids and interviews against **pace** (where you should be by today), routines done so far, each weekly
  goal's progress.
- A short metrics block: earnings this month vs last month, interview pipeline.
- Net income chart and "You vs team" move to a new **Earnings** tab on Statistics.

## Non-goals

- Daily plan page / team table changes (parked by the user).
- New metrics beyond the two listed.
- Changing how the 6 PM nudge, Slack form or standup post look.

## Anti-goals

- Ticking on Home must **never** post or create a Slack standup message for an unsubmitted day.
- Home must not become a second copy of the Daily plan form (notes, per-profile bid typing, Post button flow).
- No second copy of target / pace math that can drift from the Daily plan's.

## Approaches considered

1. **Reuse existing endpoints + a "save without submitting" option (chosen).** One backend change to the day save; Home
   composes the day view, dashboard week, and net-monthly endpoints. Smallest backend change; ~4 calls on load.
2. One new `/home` aggregate endpoint. One call, but re-implements day / week / compare logic → drift risk. Rejected.
3. Embed the existing Daily plan `DayPanel` on Home. Fastest, but it's a full form; Home would feel like the Daily plan
   page. Rejected.

User picked 1. Layout: user picked **A — "Today first", single column** (mockup:
`engineers_app/.superpowers/brainstorm/*/content/home-layout.html`, local only).

## Layout (A)

```
┌ Today · Wed Nov 18 ─────────────── Update not posted · [Write my update] ┐
│ Self bids                          52 / 80   (read-only here, "edit" →)   │
│ Bidder bids (Bid Track)            86 / 120  (read-only)                  │
│ Interviews  2:00 PM Acme · 4:30 PM Globex      1 / 2                      │
│ ☑ Bidder Management                                   ✓                   │
│ ☐ Email outreach                               [31] / 50                  │
│ ☐ Interview Prep                              [1.5] / 2 h                 │
│ ☆ Focus: Ship portfolio site (weekly goal)            ☐                   │
└───────────────────────────────────────────────────────────────────────────┘
┌ This week · Nov 16 – 20 ───────────────────────────── day 3 of 5 ┐
│ Bids 734 / 2000          ▼66 vs pace   [bar with pace tick]      │
│ Interviews 5 / 10        ▲1 vs pace    [bar with pace tick]      │
│ Routines 11 / 15                                                 │
│ Weekly goals: • Ship portfolio site ☐  • 250 contacts 140/250    │
│               • Mock interview x 3  3/3 ✓                        │
└──────────────────────────────────────────────────────────────────┘
┌ Metrics ─────────────────────────────────────────────────────────┐
│ Earnings this month   $4,820  ▲12% vs last month   → Statistics  │
│ Pipeline              3 tech · 2 final · 1 offer   → Interviews  │
└──────────────────────────────────────────────────────────────────┘
```

## Decided behavior

### Today card
- Data: `GET /daily-plans/{today}` (targets, items with results, focus, progress, `bidsBidderAuto`, submitted, the
  day's interviews).
- **Routines** and **focused weekly goals** are editable: check items toggle; count / hours items take a number.
  Each change saves (debounced, ~800 ms) through the draft save below. Optimistic UI; on failure the row reverts and
  shows an inline error with retry.
- **Self bids**: shown read-only (the day's total, typed + tracked as the Daily plan shows it), with an "edit" link to
  the Daily plan page for that day. *(My call: per-profile typing stays on the Daily plan page — anti-goal: no second
  form.)*
- **Bidder bids**: read-only (Bid Track, approved + pending).
- **Interviews**: done / today's target, and today's interview times (self + caller) as one line.
- **Header status**: "Update not posted yet" (amber after 18:00 America/Chicago) with **Write my update** → opens the
  Daily plan page on today (`/report?day=YYYY-MM-DD`), where notes and Post live. Once submitted: "✓ Posted 6:12 PM"
  and the button reads **Edit update**.
- Weekends: show the same card (days can be followed up any day), no amber warning.
- No **saved** weekly plan for the week (the day view shows the carry-over draft): card is **read-only**, shows "No plan
  for this week yet" + link to set one (`/plans`). *(My call: draft-week item ids aren't stored yet, so results saved
  against them could be orphaned.)*
- Admins: the Today card is always the signed-in user's own day; the admin user picker affects only TeamOverview.

### Draft save (backend)
- `PUT /daily-plans/{day}` gains query param `submit` (default `true`, existing behavior unchanged).
- `submit=false` is a **partial merge**, not a full day save *(my call: Home never holds `done` / `notes`, and a
  Daily plan tab may be open at the same time — a full-body save would wipe typed self bids and notes)*:
  - Body: `{ results?: {itemId: {done, value}}, focus?: string[] }`. Only the item ids sent are written (`$set` per
    `results.<id>`); `focus` replaced only when sent. `done`, `notes`, `targetOverride` are never touched.
  - Item ids must belong to the day's items (`day_items`), else 422 — same rule as a submit.
  - Results are stored **without** a frozen `target`; freezing happens at submit, as today. *(My call: a Monday tick
    must not lock routine targets before the week is finished being edited.)*
  - `done` derivation for count / hours (value ≥ target) uses the item's current target, same helper as `save_day`.
  - **Does not** set `submittedAt`, does not set `loggedAt`, does not post to Slack, does not freeze day targets.
  - Creates the day document if missing (as the bidder sync already does; `loggedAt: None`).
  - Nothing reads "a day document exists" as "submitted": readers use `submittedAt` / `loggedAt` (checked: alerts,
    dashboard, reports, standup). The bidder sync already creates unsubmitted documents, so drafts add no new case.
  - If the day is **already submitted**: behaves like `submit=true` for Slack (edits the existing post in place) but
    keeps the original `submittedAt`. *(My call: matches the app's existing "Update post" behavior; a submitted day's
    post always shows current numbers.)*
- Same validation, auth (`require_writer`), and "no future days" rule as today.
- The Slack "Write my update" form already prefills from the stored plan, so Home's draft ticks appear there.

### This week card
- Data: `GET /dashboard/week` (bids, interviews, stages).
- **Counts every stored day of the week, drafts included** — bids (`done` + Bid Track bidder count), interviews,
  routines. *(My call: "how far done by today" is the user's framing; today `_logged_plans` skips unsubmitted days,
  which already hides the bidder auto-count on unsubmitted days.)* Streak, logging ratio and team percentile stay
  logged-only (they measure follow-up discipline). The streak is no longer shown on Home; the field may stay.
- `/dashboard/week` has one consumer (`ThisWeekCard`, i.e. Home), so its numbers change in place; new fields are
  additive (`routines: {done, expected}`, `weeklyGoals: [{id, text, kind, value, target, done}]`). The pace dict
  already has `expected`, `behindBy`, `perDayNeeded`, `onTrack`; keep those keys.
- **Pace changes**: `dashboard_stats.pace` today assumes Mon–Sat ÷6. It must use the plan model: expected-by-today =
  sum of the derived **day targets** (`plan_auto.day_targets`, including per-day overrides) for Mon–Fri days up to and
  including today; weekends add nothing; past weeks = full week target. `perDayNeeded` divides the remainder over the
  remaining Mon–Fri days. *(My call: one source of truth with the Daily plan's ÷5 targets — anti-goal: no drift.)*
- Shows "▼N vs pace" / "▲N vs pace"; no target → just the count, no arrow, no bar.
- **Routines**: routine check-offs done so far vs routines × Mon–Fri days elapsed (today included). Source: the new
  `routines` field on `/dashboard/week`, computed with existing `plan_auto` helpers over every stored day.
- **Weekly goals** source: the new `weeklyGoals` field (`goal_progress` over every stored day).
- **Weekly goals**: each goal with its progress (`goal_progress`: summed value vs target, or done flag for check
  goals). Read-only here.
- The old streak and stage tiles leave this card (pipeline moves to Metrics).

### Metrics card
- **Earnings this month** vs last month: from `GET /dashboard/net-monthly` (last two months); ▲/▼ percentage; link →
  Statistics › Earnings.
- **Pipeline**: the interview stage counts from `/dashboard/week` `stages`, as one line ("3 tech · 2 final · 1 offer"),
  zero stages hidden; link → Interviews.

### Statistics › Earnings tab (new)
- New tab key `earnings` in `Statistics.tsx`, placed after Interviews.
- Contains the existing `NetIncomeCard` (12-month chart) and `CompareCard` ("You vs team"), unchanged in behavior.
- Uses the tab's existing user filter if one exists for this purpose; otherwise the cards keep their own props
  (`userId` from the filter's single selected user, else self). *(Builder's call on wiring.)*

### Admins
- Admins see the same personal Home. The admin **TeamOverview** table stays on Home **below** the personal sections,
  with its user picker, until the Daily plan team view work resumes (then it moves there).

## Constraints

- Mon–Fri working days, America/Chicago for "after 6 PM", dates as `YYYY-MM-DD`.
- Existing endpoints' response shapes stay backward compatible (additive fields only); `submit` defaults to `true`.
- Tests: backend unit tests in `tests_unit/` (mongomock); app tests with vitest. TDD per task.
- Backend + app deploy together only if a response shape changes; the `submit` param is additive.

## Left to the builder

- Component split for Home (e.g. `TodayCard`, `WeekCard`, `MetricsCard`) and reuse of `DayPanel` row pieces.
- Debounce details, optimistic update mechanics (SWR `mutate`).
- Where the routines-done count is computed (day view vs week endpoint), as long as it uses existing plan logic.
- Removing now-unused dashboard code (`ThisWeekCard` pieces, streak) once nothing references it.
