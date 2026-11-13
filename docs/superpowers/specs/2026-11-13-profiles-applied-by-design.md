# Profiles list: interviews by who applied, rates per source, first / last apply

Date: 2026-11-13 · Branch `feat/profiles-applied-by` (backend from `dev`, `engineers_app` from `main`)

## Why

On the Profiles list the owner should see where each profile's interviews come from and which kind of bid turns
into interviews more often. Today the Interviews column splits by who *took* the round (self / caller), and Intv/bid
mixes every bid with every interview. "Applied by" (self / bidder) has been on every new interview since Nov 6, so
the split by source is now possible. The owner also wants to see when each profile last applied, to spot idle ones.

## What changes (decided with the user)

- **Interviews column:** the total, with `N self · N bidder · N not set` underneath (by "Applied by"; "not set" =
  interviews from before Nov 6, or saved without it). The self / caller split moves to the tooltip.
- **Two rates instead of Intv/bid:**
  - **Self intv/bid** = self-applied interviews ÷ self bids;
  - **Bidder intv/bid** = bidder-applied interviews ÷ bidder bids.
  - Both count only from **Nov 6, 2026** (when "Applied by" started), whatever the period: the window is
    [max(period start, Nov 6), period end].
  - "Not set" interviews count toward neither rate.
  - A period ending before Nov 6 shows `—`.
  - Under 30 bids in the window, a rate is faded with "fewer than 30 bids" (as today's `lowVolume`).
- **Applies column (new):**
  - the profile's **first** apply date, with "last: N days ago" under it and both exact dates on hover;
  - lifetime, not affected by the Period picker;
  - sources, only these two: **bidder bids** (Bid Track, approved + pending, the Bid Track day of `firstAt`, the
    profile's bidders including archived ones) and **self bids typed per profile** in the Daily Plan (logged days
    with `bidsSelfByProfile[profile] > 0`);
  - `—` when the profile has neither.
- Sorting: Applies sorts by last apply. Each rate column sorts on its own.
- Out of scope:
  - the profile's Metrics tab (it keeps self / caller and its own Intv/bid);
  - retiring the Job Applies "Applied" tag, which is a separate design, next.

## Approaches

1. **Extend `GET /accounts/metrics` (picked).** It already loads every profile's bids and rounds in a few queries, so
   the list stays one request with one period. The rates come from one extra pass with the existing
   `collect_range` over the window from Nov 6. That's needed because the All-time path keeps lifetime totals only,
   not days.
2. A separate endpoint for the rates and dates: rejected. It means a second request on every list load and a second
   copy of "the period" to keep in step.
3. Computing the rates in the browser from daily data: rejected. The list doesn't receive daily counts, and sending
   them for every profile would be large.

## Backend (`app/profile_metrics.py`, `app/routers/accounts.py`)

- `APPLIED_BY_SINCE = date(2026, 11, 6)`.
- `interview_rounds` also counts each round by its interview's `appliedBy` (self / bidder / not set). The per-day
  counts gain `selfAppliedInterviews`, `bidderAppliedInterviews` and `unsetAppliedInterviews`, which add up to the
  interviews. `summarize` returns their totals, and `lifetime` too.
- Each list row gains:
  - `selfRate` / `bidderRate` = `{interviews, bids, rate, lowVolume}` over the window (`rate` null at 0 bids);
  - `firstApply` / `lastApply` (ISO dates, or null).
- The response gains `ratesSince`: the window's first day, or null when the period ends before Nov 6.
- `apply_dates(accounts) -> {accountId: (first, last)}`: one aggregation over bids (min / max `firstAt` per bidder,
  mapped to its profile and to the Bid Track day), plus one scan of logged daily plans that have per-profile self
  bids.
- Existing fields stay, because the Metrics tab and its peers use them.

## Frontend (`src/pages/Accounts.tsx`, `src/api/endpoints.ts`)

Columns: Name · Region · **Applies** · Bids · Interviews · **Self intv/bid** · **Bidder intv/bid** · Resume score.
The rate tooltips say "since Nov 6: N interviews ÷ N bids".

## Testing

Unit tests (mongomock):
- the applied-by split adds up to the interviews;
- both rates count only from Nov 6 (a self-applied round and self bids on Nov 5 don't count; on Nov 6 they do);
- "not set" is in neither rate;
- `—` for a period ending before Nov 6;
- `lowVolume` under 30 bids;
- first / last apply from bids only, from self bids only, and from both, with archived bidders included and rejected
  bids left out;
- All-time vs 4-week periods.

Frontend: lint and build. Live read-only check of one profile's numbers against a direct count.
