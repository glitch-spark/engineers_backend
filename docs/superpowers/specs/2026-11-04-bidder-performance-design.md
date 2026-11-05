# Bidders → Performance (bids, streaks, interviews per bidder over a period)

Date: 2026-11-04 · Branch: `feat/new-feature-3` (backend from `dev`) · Frontend: `engineers_app`, same branch name
(from `main`)

## Goal

The Bidders page shows one pay week at a time. Owners need to see how each bidder performs over a longer stretch and
who the top performers are:

- each bidder's bids per week, their total and the change against the previous period;
- consistency: a streak of weekdays with 50+ bids, how often the 50 is hit, average bids per weekday;
- interviews on the bidder's profile and interviews per 100 bids;
- the last time the bidder bid, so one who went quiet stands out;
- top performers: most bids, longest streak, most interviews, best conversion.

Out of scope: pay and cost per interview, approval-rate and reject-reason breakdowns, changes to the pay board, the
weekly Slack report, or bid review.

## Who sees it

Same as the pay board (`routers/bids._visible_bidders`): a user sees the bidders they own; an admin sees every
user's, narrowed by the page's owner filter. The page's **Show archived** switch adds archived bidders.

## 1. Definitions

**Period.** Week, Month or Custom, as the Statistics tabs (`interview_rates.window` / `label`, parsed by
`routers/metrics._stats_days`; a custom range is at most 366 days). Default: this month. The current week or month
runs to today.

**Days.** A day is a Bid Track day: the daily window ending at `BID_DAILY_REPORT_TIME` in `BID_REPORT_TIMEZONE`
(18:30 America/Chicago by default), as on the pay board (`app/bids/windows.py`; `profile_metrics.bid_report_day` gives
a bid's day). A bid at the cutoff belongs to the next day.

**Weeks.** Monday–Sunday weeks of those days. The weekly columns are every week touching the period; a week cut by
the period's edge holds only its days inside the period and is marked partial.

**Bids.** Bid records (`bids`) of the bidder with `status` approved or pending (rejected bids never count). The total
also reports approved, pending and rejected apart. Only records count: days before `BID_RECORDS_START` were counted
from screenshot folders and are left out; the response names that date and the UI says so when the period reaches
before it.

**50+ day.** A weekday (Monday–Friday) with 50 or more approved + pending bids.

| Metric | Definition |
|---|---|
| Longest streak | Longest run of consecutive 50+ weekdays in the period. Saturdays and Sundays are skipped: they neither count nor break a run |
| Current streak | The run of 50+ weekdays ending at the latest weekday counted (below), counted back within the period |
| Weekdays counted | Weekdays of the period from the latest of: period start, the bidder's creation day, `BID_RECORDS_START`; up to the earliest of: period end, the day the bidder was archived, yesterday. Today is added only once it has 50+ (a day in progress never reads as a miss) |
| Hit rate | 50+ weekdays ÷ weekdays counted; null with 0 weekdays |
| Avg / weekday | Total bids ÷ weekdays counted (weekend bids count in the total); null with 0 weekdays |
| Interviews | Interview rounds held on the bidder's profile (`Bidder.profileId`) in the period, counted as Statistics and the profile metrics (`profile_metrics.interview_rounds`: rounds held, canceled / AI interview / home assessment excluded); null without a profile |
| Shared profile | True when the profile has another active bidder, or self bids typed per profile (`DailyPlan.done.bidsSelfByProfile`) in the period: its interviews come from other bids too |
| Interviews / 100 bids | Interviews ÷ total bids × 100, 2 decimals; null under 100 bids |
| Last bid | Latest `firstAt` of any of the bidder's bids, whatever the period; null with none |
| Change | (total − previous total) ÷ previous total; null when the previous total is 0. The previous period: Week → the week before; Month → the calendar month before, cut to the same number of days while the current month runs to today (Nov 1–4 against Oct 1–4); Custom → the same number of days just before its start |

Rounding: rates (`hitRate`, `change`) 3 decimals; `avgPerWeekday` 1 decimal; `per100` 2 decimals.

## 2. The Performance tab (`engineers_app`)

**Tabs.** The Bidders page (`src/pages/Bidders.tsx`) gets two tabs under its header: **Pay week** (today's board,
unchanged) and **Performance**, kept in the URL as `?view=performance` (`tab` is already the bidder view's History
param). The admin owner filter and Show archived switch apply to both.

**Filter bar.** The Statistics controls (`src/components/statistics/controls.tsx`: `Segmented` periods Week · Month ·
Custom, `PeriodNav`, From/To with `filtersError`). No region filter.

**Top performers.** Four cards, each the top 3 with their value; a name opens the bidder's view:

| Card | Ranked by | Ties |
|---|---|---|
| Most bids | total | approved, then name |
| Longest streak | longestStreak (days) | hitRate, then total |
| Most interviews | interviews | per100, then name |
| Best conversion | per100, bidders with 100+ bids | interviews, then name |

A card with no eligible bidder shows "—".

**Bidders table.** One row per bidder, sorted by total, every metric column sortable (descending first, nulls last):

| Column | Cell |
|---|---|
| Bidder | name; profile (and owner, admins) below |
| One per week | the week's bids; partial weeks headed `Oct 27*` with a tooltip of the days covered |
| Total | total; `↑ 12%` / `↓ 4%` change below; tooltip approved / pending / rejected |
| Streak | longest; `now 3` below |
| Hit rate | `85%`; `17 of 20 weekdays` below |
| Avg / weekday | `53.9` |
| Interviews | rounds; `shared profile` note with tooltip when `sharedProfile` |
| Per 100 bids | `0.56`; `—` with tooltip "Fewer than 100 bids" when null |
| Last bid | relative (`2h ago`); amber when older than one weekday |

A Total row sums bids and interviews and shows the team's hit rate, avg / weekday and per 100 bids over the summed
values. A row opens the bidder's view on the pay week holding the period's last day.

**States.** Skeletons while loading; "Couldn't load performance — Retry" on error (the Pay week tab is unaffected);
"No bidders in this period" when empty; the records-start note above the table when the period reaches before it.
Phones: cards 2 per row; the table scrolls inside its box.

## 3. Backend (`engineers_backend`)

**`app/bids/performance.py`:**

- `day_counts(bidder_ids, start, end, sched) -> {(bidder id, day): (approved, pending, rejected)}` — one aggregation
  over `bids` grouped by bidder, report day and status (the day computed in the query from the cutoff and report
  timezone); only the groups come back. A unit test checks it against `bid_report_day` across the cutoff and a DST
  change.
- `last_bid_at(bidder_ids) -> {bidder id: datetime}` — `$group` max `firstAt` (index `bidderId, firstAt`).
- `streaks(hits: dict[date, bool], first: date, last: date, today: date) -> dict` — pure: longest, current, hitDays,
  weekdays per §1.
- `week_columns(start, end) -> list[dict]` — pure: Mon–Sun weeks touching the range with `partial`.
- `rank(rows) -> dict` — pure: the four top-3 lists per §2.
- `performance(bidders, period, start, end, label, today) -> dict` — loads this and the previous period in one
  `day_counts` call over both, interviews via `profile_metrics.interview_rounds` over the bidders' profiles, shared
  flags via other active bidders on the profile and `profile_metrics.self_bids`, then assembles the response.

**Endpoint** `GET /bids/performance?period=week|month|custom&date=&to=&includeArchived=&ownerId=` in
`app/routers/bids.py`, declared before `/{bid_id}`. Unknown `period` → 422; bad date / range → 400 (`_stats_days`).

**Response:**

```json
{"period": "month", "start": "2026-10-01", "end": "2026-10-31", "label": "Oct 2026", "recordsStart": "2026-09-08",
 "weeks": [{"start": "2026-09-29", "end": "2026-10-05", "partial": true}],
 "bidders": [{"id": "…", "name": "Ana", "ownerName": "Pedro", "profileId": "…", "profileName": "AI Fullstack",
              "status": "active", "weekly": [120, 310, 305, 320, 185], "total": 1240, "approved": 1100,
              "pending": 140, "rejected": 35, "previousTotal": 1105, "change": 0.122, "longestStreak": 10,
              "currentStreak": 3, "hitDays": 17, "weekdays": 23, "hitRate": 0.739, "avgPerWeekday": 53.9,
              "interviews": 7, "sharedProfile": false, "per100": 0.56, "lastBidAt": "2026-11-04T14:02:11Z"}],
 "totals": {"total": 5210, "interviews": 21, "hitRate": 0.62, "avgPerWeekday": 45.3, "per100": 0.4},
 "top": {"bids": ["…"], "streak": ["…"], "interviews": ["…"], "conversion": ["…"]}}
```

`weekly` is aligned with `weeks`. `recordsStart` is null when `BID_RECORDS_START` is unset. Totals: `hitRate` = summed
hitDays ÷ summed weekdays; `avgPerWeekday` = summed total ÷ summed weekdays; `per100` over summed interviews and bids
(each profile's interviews counted once).

## 4. Testing

Backend, `tests_unit/` with mongomock-motor:

- `test_bids_performance.py` — streaks (weekend inside a run; a 49 breaks it; today only at 50+; current vs longest;
  creation-day, archive-day and records-start clipping); report-day boundary at 18:30 Chicago and across DST;
  approved + pending counted, rejected not; week columns with partial edges; change against the previous period;
  last bid across all time; interviews by profile and the shared flag (second bidder; typed self bids); `per100` null
  under 100; top-card ranking and ties, conversion's 100-bid minimum.
- `test_bids_performance_router.py` — response shape, visibility (own bidders; admin with and without owner filter),
  unknown period 422, bad range 400.
- A read-only check against the live database (as the profile-metrics check): totals and 50+ days recomputed with
  separate plain queries; run only with the user's go-ahead.

Frontend: `npm run lint` and `npm run build` pass; manual check in the local app (tabs, periods, cards, sorting,
phone width, dark mode).
