# Statistics cleanup: Bids and Interviews tabs

Date: 2026-11-06 · Branch: `feat/statistics-cleanup` (backend from `dev`, frontend `engineers_app` from `main`)

## Goal

Statistics reads clearly. Bids and interviews get a tab each instead of sharing "Interview rate". Bids split into self vs
bidder, and interviews into self-taken vs caller-taken. Callers keeps its own tab. Bidder bids come from Bid Track
records, so Statistics agrees with Profiles metrics and Bidders → Performance.

Decided with the user:
- Tabs: **Bids · Interviews · Callers · Resume compare · AI usage**. "Interview rate" is removed.
- Interviews are split by **who took the round**: self vs caller. They are not split by "Applied by".
- Bidder bids come from **Bid Track records only** (approved + pending). Before Oct 6, 2026 (the first record) they are
  0. The bidder count typed in the Daily Plan is no longer used on Statistics.
- Interviews per bid, the bids-vs-interviews chart and the ranking move to the **Interviews** tab.

Out of scope:
- Callers, Resume compare and AI usage: unchanged.
- The Daily Plan's typed bidder count: still saved and shown on the Daily Plan, the week card and Slack.
- Any interview split by "Applied by".

## 1. Numbers (`app/interview_rates.py`)

Everything else stays as today:
- the same period and region filters;
- every non-admin user ranked, with an admin viewer shown but not ranked;
- `MIN_BIDS = 30`.

| Number | Definition |
|---|---|
| Self bids | `done.bidsSelf` on logged days. A region uses that region's field (`bidsSelfUs` / `Eu` / `Latam`). Unchanged. |
| Bidder bids | Bid Track `Bid`s with status approved or pending, counted to the bidder's owner (`Bidder.createdBy`, as Bidders → Performance; archived bidders included). The day is the Bid Track report day from `firstAt` (daily window ending 18:30 in the report timezone, as Bids → Pay week). A region counts the bids whose bidder (`Bidder.profileId`) is on a profile of that region. A bidder with no profile, or a profile with no region, counts under All only. |
| Bids | self + bidder |
| Interviews | rounds held (`interview_rounds`: canceled rounds, AI interviews and home assessments excluded), in the region of the interview's profile, counted to the interview's owner. Unchanged. |
| Self / caller interviews | each round held, split by `split_rounds_by_day`'s `is_caller` (an enabled caller on the round). |
| Interviews per bid | all rounds ÷ all bids; `lowVolume` under 30 bids. Unchanged. |

**What changes from today:**
- Bidder bids came from the typed `done.bidsBidder` and counted under All and US only (`BIDDER_REGIONS`). Now they come
  from records, in the bidder's profile region.
- `bidderSplit` goes. Every region can now have bidder bids.

**Where it lives:**
- `counts()` keeps self bids and interviews.
- A new `bidder_bids(user_ids, start, end, region, regions)` returns `{(day, ownerId): n}`. It uses one aggregation
  over `bids` for the period, reusing `app/bids/performance.day_counts` / the Bid Track day windows. Bidders are
  loaded once, to map bidder → owner and profile.
- The per-day map grows to `[self bids, bidder bids, self interviews, caller interviews]`.

**Response** (`GET /metrics/interview-rates`). Same shape, with these changes:
- Each row gains `selfInterviews` and `callerInterviews`.
- `totals` gains the same two fields.
- `interviewsChart` gets two parts, `self` and `caller` (it had `interviews`).
- `bidsRecordsStart`: the first Bid Track record's local day (`2026-10-06`), or `null` when there are no records.
  The Bids tab uses it for its note.
- `bidderSplit` is removed.

## 2. Frontend (`engineers_app`)

`src/pages/Statistics.tsx`:
- Tabs **Bids** (default), **Interviews**, **Callers**, **Resume compare**, **AI usage**.
- The filters stay shared by the first four tabs.
- Both new tabs read the same SWR key, so switching between them doesn't refetch.

`InterviewRateTab.tsx` is split by job. The period and region controls, the per-user bar chart and the color slots move
into `src/components/statistics/rateShared.tsx`.

**`BidsTab.tsx`:**
- Tiles: Bids, Self, Bidder, Bidder share (bidder ÷ bids).
- "Bids per user" bars by day, week or month, stacked self / bidder.
- When the period starts before `bidsRecordsStart`, a note under the chart: "Bidder bids are counted from Bid Track,
  which starts Oct 6, 2026."
- A table per user: Self, Bidder, Total, Bidder share. Sortable, You highlighted.

**`InterviewsTab.tsx`:**
- Tiles: Interviews, Self-taken, Caller-taken, Interviews per bid.
- "Interviews per user" bars stacked self / caller.
- The bids-vs-interviews chart and the ranking, as today. The ranking table gains Self and Caller columns.

**API types** (`src/api/endpoints.ts`): `InterviewRateRow` and the totals gain `selfInterviews` and
`callerInterviews`; `InterviewRates` gains `bidsRecordsStart` and loses `bidderSplit`.

## 3. Errors

- No bids or interviews in a period: the existing empty states.
- A rate with 0 bids: `null`, shown as `—`.
- Bidders whose owner is an admin: their bids are not on the board, the same as today's users.

## 4. Testing

`tests_unit/test_interview_rates.py` (mongomock), new cases:
- bidder bids come from records: approved + pending count, rejected doesn't;
- they are counted to the bidder's owner, archived bidders included;
- a bid at 18:29 vs 18:30 lands on different days;
- the region comes from the bidder's profile, and a bidder without a profile counts under All only;
- the typed `bidsBidder` is ignored;
- the self / caller interview split adds up to the interviews;
- `bidsRecordsStart`.

Existing cases are updated where they relied on typed bidder bids or `bidderSplit`.

Frontend: `npm run lint` and `npm run build`.

Live check, read-only: for one month, compare team bidder bids with Bidders → Performance (all bidders, archived
included) and with the Profiles list's bidder bids. Interviews must equal the self + caller split.
