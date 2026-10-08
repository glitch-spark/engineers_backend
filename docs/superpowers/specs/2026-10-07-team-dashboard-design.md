# Team dashboard: admin "All users" view, goal attainment and plan logging

Date: 2026-10-07 · Branch: `feat/cleanup` (backend and frontend)

## Goal

Admins land on a team overview instead of "Pick a user": team totals and one sortable row per person, and can still
open any individual's dashboard. Two discipline metrics — goal attainment and plan logging — join bids, interviews
and earnings, in the admin table and in everyone's "You vs team" card.

## Context

- `/dashboard` (frontend `src/pages/Dashboard.tsx`) shows This week, You vs team, Activity and Net income cards.
  Admins must pick a user first; with none picked they see "Pick a user to see their dashboard."
- `GET /dashboard/compare?range=week|month|30d` (backend `app/routers/dashboard.py`) ranks active non-admin users by
  bids (logged Daily Plan self + bidder), interviews (`interviews_by_day`) and earnings (net of approved
  transactions dated in the period). Periods come from `compare_window`; ranking from `rank_rows`
  (`app/dashboard_stats.py`). Names follow the Leaderboard's `_display_name` (anonymous users stay anonymous).
- Weekly goals: `WeeklyPlan.goal` (`Counts`: `bidsSelf`, `bidsBidder`, `interviewsSelf`, `interviewsCaller`),
  looked up per week by `weekly_plans_for(user_ids, monday)`.
- Streak: `streak(plan_days, today)` — consecutive logged working days, as on the This week card.

## Decisions (user)

- Admin default is **All users**: team totals + team table. Picking a user shows that user's dashboard.
- New metrics: **goal attainment** and **plan logging** only.
- Goal % and days logged also appear in **everyone's** "You vs team" card.

## 1. Metric definitions

All metrics use the selected period `[start, end]` from `compare_window(today, range)` (app timezone).
**Working days** are Mon–Sat.

- **Bids, interviews, earnings** — unchanged from `/dashboard/compare`. Bids also report the `self` / `bidder` split.
- **Goal attainment (bids / interviews)** — `done ÷ goal`, as a whole percent.
  - Goal: for every Mon–Sun week overlapping the period, that week's `WeeklyPlan.goal`
    (bids = `bidsSelf + bidsBidder`; interviews = `interviewsSelf + interviewsCaller`), multiplied by
    `(working days of that week inside [start, end]) ÷ 6`. Summed over the weeks; weeks without a plan add 0.
  - Done: the same bids / interviews totals as above, over `[start, end]`.
  - No goal (sum is 0): `null`, shown "—". `null` ranks after every number.
  - Example: This week with a 300-bid goal and 240 bids done → 80%. Last 30 days covering 4 full weeks of 300 and
    3 working days of a fifth 300-goal week → goal 1,350.
- **Plan logging** — `logged ÷ workingDays`, shown "5 / 6".
  - `logged`: working days in `[start, min(end, today)]` with a logged Daily Plan (same "logged" rule as the other
    cards).
  - `workingDays`: working days in `[start, min(end, today)]`, but **today counts only if it is already logged**,
    so an unlogged morning does not count against anyone.
  - With `workingDays = 0` (e.g. Monday morning before logging): `null`, shown "—".
- **Streak** — current streak from `streak(plan_days, today)`; not tied to the period.

## 2. Backend

`app/dashboard_stats.py` (pure, unit-tested):
- `working_days(start, end) -> list[date]` (Mon–Sat).
- `prorated_goal(week_goals: dict[date, int], start, end) -> int | None` — `week_goals` maps each overlapping
  Monday to that week's goal; returns the rounded pro-rated sum, or `None` when it is 0.
- `percent(done, goal) -> int | None`.
- `logging_ratio(logged_days: set[date], start, end, today) -> tuple[int, int]` — `(logged, workingDays)` per §1.
- `rank_rows` treats `None` values as lowest (after 0), sharing one rank among them.

`app/routers/dashboard.py`:
- `async def team_metrics(members: list[UserExtra], start, end, today) -> dict[userId, dict]` — one pass computing,
  per user: `bids`, `bidsSelf`, `bidsBidder`, `interviews`, `earnings`, `goalBids`, `goalBidsPct`,
  `goalInterviews`, `goalInterviewsPct`, `logged`, `workingDays`, `streak`. Used by both endpoints, so their numbers
  always agree. Weekly plans are fetched once per overlapping Monday for all members.
- `GET /dashboard/compare` keeps its shape and adds three metrics:
  `goalBids`, `goalInterviews` (`value` = percent or `null`) and `logged` (`value` = ratio rounded to 4 places or
  `null`; rows also carry `logged` and `workingDays`).
- `GET /dashboard/team?range=week|month|30d&today=` — **admins only** (403 otherwise; 422 on a bad range):

```json
{
  "range": "week", "from": "2026-10-05", "to": "2026-10-11",
  "totals": {"bids": 0, "bidsSelf": 0, "bidsBidder": 0, "interviews": 0, "earnings": 0.0,
             "goalBidsPct": null, "goalInterviewsPct": null, "logged": 0, "workingDays": 0},
  "users": [{"userId": "…", "name": "…", "image": null, "bids": 0, "bidsSelf": 0, "bidsBidder": 0,
             "interviews": 0, "earnings": 0.0, "goalBidsPct": null, "goalInterviewsPct": null,
             "logged": 0, "workingDays": 0, "streak": 0}]
}
```

  Team goal % = sum of done ÷ sum of goals over users who have a goal (`null` when nobody has one). Users are
  active non-admins, ordered by bids (high first), then name.

## 3. Frontend

- `src/pages/Dashboard.tsx`: the admin picker's first option becomes **All users** (the default) instead of
  "Pick a user". All users → `<TeamOverview onPick={setSelectedUserId} />`; a picked user → the existing cards.
- `src/components/dashboard/TeamOverview.tsx`:
  - The This week / This month / Last 30 days switch (`Segmented`), applying to the whole view.
  - Totals strip: Bids, Interviews, Earnings, Bids goal %, Interviews goal %, Days logged.
  - Table: User, Bids (tooltip "self X · bidder Y"), Interviews, Earnings, Bids goal %, Interviews goal %,
    Days logged ("5 / 6"), Streak. Every column header sorts (click again to reverse); default Bids, high first;
    `null` values sort last either way. Clicking a user's name calls `onPick(userId)`.
  - Amber highlight on a goal % under 50 and on days logged under half of working days.
  - Horizontal scroll inside the table on narrow screens; loading skeleton and retry state like the other cards.
- `src/components/dashboard/CompareCard.tsx`: six ranked boards — Bids, Interviews, Earnings, Bids goal %,
  Interviews goal %, Days logged — in a 3-column grid (stacked on phones). Goal boards show "92%" or "—"; the
  logging board shows "5 / 6" or "—".
- `src/api/endpoints.ts`: `DashboardCompare` gains the three metrics; new `DashboardTeam` type and
  `getDashboardTeam`.

## 4. Error handling

- Non-admin calling `/dashboard/team` → 403. Bad `range` → 422 on both endpoints.
- A user with no plans, goals or transactions shows zeros / "—", never an error.

## 5. Testing

- Unit (`tests_unit/test_dashboard_stats.py`): `working_days` (Sunday excluded, month edges), `prorated_goal`
  (full week, partial first/last week, no plan → `None`), `percent`, `logging_ratio` (today logged vs not, Sunday,
  period ending before today), `rank_rows` with `None`.
- Endpoints (`tests_unit/test_dashboard_compare.py`, `tests_unit/test_dashboard_team.py`, mongomock with
  `interviews_by_day` faked): new compare metrics; team endpoint admin-only, totals, every column, pro-rated goal
  for month, inactive users and admins excluded, anonymous names.
- Frontend: `tsc -b` (2 known `InterviewPrepLibrary.tsx` errors) and `vite build`; the UI is checked by the user.
