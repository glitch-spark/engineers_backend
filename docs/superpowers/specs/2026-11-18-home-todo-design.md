# Home as a to-do list — design

Date: 2026-11-18. Follows `2026-11-18-home-today-design.md` (shipped). Mockup (local):
`engineers_app/.superpowers/brainstorm/*/content/home-todo.html`. Approved by the user.

## Why

The shipped Home reads like a report: bids/interviews rows, a "Write my update" link, and weekly goals shown as
non-clickable ☐ glyphs. Users want to open Home and tick off today's work — daily routines and weekly goals.
Bids and interviews update automatically, so nothing about them needs editing on Home.

## Goals

- One **to-do list**: **Daily** (routines) and **This week** (all live weekly goals), every row a real checkbox.
- Number items (count / hours) = **checkbox + number** (user picked option B): tick = done; the number is optional
  progress; reaching the target ticks it automatically.
- Weekly goals tick once for the week; a goal finished on an earlier day shows "done Mon" and can be un-ticked.
- Bids and interviews: read-only chips marked automatic. No "Write my update", no bids "edit".
- "This week" and "Metrics" shrink to one-line strips.

## Non-goals / anti-goals

- No change to the 6 PM nudge, Slack post layout, or the Plans page layout (only its rows gain the same checkbox).
- A tick on Home must still never post to Slack for an unsubmitted day (draft save, unchanged).
- Never lose a typed number by ticking, or a tick by typing a number.

## Approach (only sensible one)

Rework the Today card; change the shared done rule; give weekly goal progress the day it was finished. Rejected:
a separate weekly "done" stored on the weekly plan (splits the standup numbers across two places); separate cards
(what the user rejected).

## Decided behavior

### Done rule (backend, `plan_save.result_done` — used by the app save, the draft save and the Slack form)
- check: the sent `done` (unchanged).
- count / hours: **done = sent done OR value reaches the target** (value > 0 when no target).
- When the request did **not send** `done` for an item (the Slack form sends values only): keep the stored done,
  then apply the value rule. *(Without this, submitting the Slack form would un-tick items ticked on Home.)*
- The existing "migrated tick without a value stays done" case keeps working (it is the keep-stored-done case).

### Weekly goal progress (backend, `plan_auto.goal_progress`)
- Adds `doneOn: "YYYY-MM-DD" | null` — the earliest day of the week whose result for the goal is done.
- `done` = any day done **or** (target and summed value ≥ target).
- Additive: reaches the day view's `progress`, `/dashboard/week` `weeklyGoals`, and the Slack post's progress.

### Today card (app)
- Header: `Today · Wed Nov 18` + chips `Bids 66 / 275 · auto` (self + bidder done vs targets) and
  `Interviews 1 · auto` (target shown only when > 0). No status text, no "Write my update", no "edit".
- **Daily** section: routines (archived only with a result, disabled). Header right: `N of M done`.
- **This week** section: every live weekly goal (no longer limited to today's focus). Header right:
  `K days left · N of M done` (Mon–Fri days after today; omit days-left on weekends).
- Row: checkbox + text; number items also get a small number box + `/ target`. For a weekly count goal the box
  is **today's** amount and the label reads `· 140 / 250 this week`. Done rows: text struck through, muted.
- Tick → draft save today `{done: true, value}`; un-tick → `{done: false, value}`; typing a number → draft save
  with the current tick; if the number reaches the target the box ticks itself (client mirrors the server rule).
- Weekly goal done on an earlier day (`doneOn` ≠ today): checkbox shown ticked with `done Mon`; clicking it
  un-ticks by a draft save to **that** day (`putDayProgress(doneOn, {results: {id: {done: false}}})`), then
  refreshes. A past submitted day's Slack post updates in place (existing draft-save behavior).
- Debounce, revert-on-failure + Retry, flush-on-leave, read-only without a saved week: unchanged.

### Plans page rows
- `ItemRow` (shared) gets the same checkbox + number for number items, so Plans and Home agree.

### Strips
- **This week** strip (replaces WeekCard body): `Bids 712 / 1375 ▼113 vs pace` (+ thin bar), `Interviews 3`
  (`/ target` when set), `Routines 4 / 9`. Weekly goals list removed (they live in Today now).
- **Metrics** strip: `Earnings this month` + `Pipeline`, side by side, same data and links as today.

## Constraints

- Mon–Fri, America/Chicago; additive API changes only; draft save semantics unchanged except the done rule.
- Tests: backend `tests_unit/` (mongomock), app vitest; TDD.

## Left to the builder

- Component split inside the Today card; exact chip styling; how the strips wrap on phones (stack is fine).
