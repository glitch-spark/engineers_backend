# Profile Metrics: interviews by round type with pass rates · Profiles list: one Intv/bid

Date: 2026-11-13 · Branch `feat/profiles-applied-by` (follows `2026-11-13-profiles-applied-by-design.md`, not merged yet)

## Why

The owner wants to see, per profile, which kind of round it gets through and which it fails (e.g. passes intros,
fails tech), including take-home assessments, which no count includes today. On the Profiles list, the separate self
and bidder rates turned out to be more than needed: one interviews-per-bid rate is enough.

## What changes (decided with the user)

### Profile → Metrics tab

- A new full-width **Rounds** card below the five tiles (user picked this over cramming it into the Interviews tile).
  One row per round type, in this order: Intro, Tech, Hiring, Panel, Final, then a divider and Home assessment.
  Columns: Round · Done · Passed · Rejected · Pass rate (with a small bar).
  Under the table: "N held with no result yet · N upcoming" (omitted when both are 0).
- Round types are the existing groups (`app/interview_rounds._breakdown_group`): Tech = tech 1/2, live coding, system
  design; Hiring = cultural / hiring manager; Final = final / offer. AI interviews stay out.
- Rules, the same as Statistics → Callers (`app/caller_stats`), so both pages agree:
  - rounds from `merged_rounds` (consecutive entries of one stage are one round), on the local day of `scheduledAt`,
    inside the tab's selected period;
  - **done** = passed + rejected + held with no result yet (`completed`); canceled and still-scheduled rounds aren't
    done;
  - **pass rate** = passed ÷ (passed + rejected); `—` with no decided rounds.
- **Home assessments:** counted on their `scheduledAt` (the due date) in the period; done = completed, passed or
  rejected; same pass rate. They don't join the Interviews total.
- **Interviews tile:** keeps the total (unchanged, so it matches the list and the charts). The line under it becomes
  "N% pass overall" (all round types except home assessments, decided rounds only), with the peer comparison line as
  the other tiles have. The self / caller split moves to the tile's tooltip.

### Profiles list

- One **Intv/bid** column replaces Self intv/bid and Bidder intv/bid: all interviews ÷ all bids over the selected
  date range (the row's existing `interviewsPerBid`), faded under 30 bids (`lowVolume`). No Nov 6 cutoff.
- The Interviews column keeps its `N self · N bidder · N not set` split (by "Applied by").
- Sorting: the rate column sorts by `interviewsPerBid`.

Out of scope: per-round peer comparison, round-number funnel (1st / 2nd / 3rd round), the charts below the tiles.

## Approaches

The build has one sensible way: **extend `GET /accounts/{id}/metrics`** with a `rounds` block computed over the same
period it already uses. Rejected: calling Statistics → Interviews from the tab (a second request with its own period
rules) and computing in the browser (the tab doesn't load interviews).

Layout: A (full-width card) picked by the user over B (inside the tile).

## Backend

- `GET /accounts/{id}/metrics` gains:
  - `rounds`: `{intro|tech|hiring|panel|final|home_assessment: {done, passed, rejected, passRate}}`, plus
    `awaiting` and `upcoming` totals;
  - `passRate` (overall, home assessments excluded) and `peers.passRate` (pooled over the owner's other active
    profiles' decided rounds in the same days — my call: pooled, so a peer with 1 round doesn't weigh like one with 40).
- Reuse `caller_stats.round_result` / `summary` shapes rather than a second copy of the rules.
- `GET /accounts/metrics` (list): `selfRate`, `bidderRate` and `ratesSince` are removed (only the list page used
  them); `APPLIED_BY_SINCE` goes too if nothing else needs it. The applied-by interview split stays.

## Frontend

- `ProfileMetricsPanel.tsx`: the Rounds card and the tile's new line; `endpoints.ts` types.
- `Accounts.tsx`: one Intv/bid column (colSpan back to 10); the "needs the latest server update" guard keys on a field
  the new list response still has.

## Testing

Unit (mongomock): per-type done / passed / rejected / pass rate; canceled and scheduled not done; a held round with no
result counts as done and awaiting; home assessment counted on its due date and kept out of the overall rate; AI
interviews ignored; consecutive same-stage entries are one round; outside the period ignored; peers' pooled rate.
List: one `interviewsPerBid`, no `selfRate` / `bidderRate`. Frontend: lint and build; a live read-only check of one
profile's rounds against a direct count.
