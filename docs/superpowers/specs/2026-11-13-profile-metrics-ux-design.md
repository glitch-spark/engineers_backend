# Profile → Metrics tab: a verdict first, detail below, compared with every owner's profiles

Date: 2026-11-13 · Branches `feat/profile-metrics-ux` (engineers_backend from `dev`, engineers_app from `main`).
Follows `2026-11-13-profile-rounds-design.md` (merged: Rounds card, one Intv/bid on the list).

## Why

An owner opening a profile's Metrics tab should see in seconds whether the profile is doing well and what to improve,
with the detail underneath. Today the tab is five equal tiles of raw numbers (you judge yourself whether 2.1% is
good), compares only with the owner's own other profiles, compares *totals* (unfair on All time: a 2-week-old profile
against 3-month-old ones), hides self vs caller per round, has no conversion trend, spreads resume info over two tiles,
and keeps a six-column day table always open that repeats the charts.

The four insights the owner asked for: **bids** (self & bidder), **interviews** (self & caller, per round),
**bid → interview conversion**, **tailor score with resumes tailored**, each compared with the owner's other profiles
and with every owner's profiles.

## Goals, non-goals, anti-goals

- Goals: a top-of-tab verdict ("Needs attention"); four cards with one comparison each; detail in two tables (Rounds
  by type with self / caller; "How it compares" with three columns); trends in two charts; the day/week numbers on
  demand; the tab stays as fast as today.
- Non-goals: AI-written advice; per-round comparison with other profiles; changes to the Profiles list page or to
  Statistics; per-bidder breakdown on this tab (the list already names bidders).
- Anti-goals: naming other owners or their profiles anywhere (averages only); a tab that waits on the slow
  every-owner averages before showing the profile's own numbers; comparisons that punish a young profile for its age.

## Decisions (with the user)

- **"All profiles"** = every owner's active profiles (this one included, so one average serves every profile),
  shown as an average with no names, **to every owner** (user picked A over admins-only and same-region-only).
- **Build**: a separate request for the "All profiles" numbers, remembered by the server per date range for ~15
  minutes; the tab shows its own numbers first and fills the "All profiles" values in when they arrive (user picked
  this over adding it to the tab's request, measured at +10 s for Nov 1–13 and +16 s for Oct 1–Nov 13 over 57 active
  profiles, mostly loading ~45k resume scores; and over a nightly precompute, which can't serve custom ranges or the
  current week).

## Approaches

1. Add the every-owner averages to `GET /accounts/{id}/metrics` — rejected: every tab open waits 10–16 s more.
2. **Separate `GET /accounts/metrics/benchmark`, cached in process per range for 15 min — picked.** First open of a
   range takes 10–16 s for the "All profiles" values only; any profile in that range after that is instant. Values
   can be up to 15 minutes old, which doesn't matter for an average over ~57 profiles.
3. Nightly precompute stored in Mongo — rejected: no custom ranges or "this week to today", plus a job to keep alive.

## The tab, top to bottom

```
Metrics            [Week | Month | Custom | All time]  ‹ Nov 2026 ›   [Day | Week]
Nov 2026
┌ Needs attention ─────────────────────────────────────────────────┐
│ ▼ Tech rounds pass 20% (Intro 71%)                               │
│ ▼ 1.8% interviews per bid vs 3.6% across all profiles            │
│ ▲ Tailor score 84, above all profiles (74)                       │
└──────────────────────────────────────────────────────────────────┘
┌ Bids ────────┐┌ Interviews ──┐┌ Interviews / bid ┐┌ Tailor score ─┐
│ 412          ││ 14           ││ 3.4%             ││ 81            │
│ 120 self ·   ││ 9 self ·     ││                  ││ 63 tailored   │
│ 292 bidder   ││ 5 caller     ││                  ││               │
│ 34 / week    ││ 46% pass     ││                  ││               │
│ Yours 28 ·   ││ Yours 40% ·  ││ Yours 2.9% ·     ││ Yours 78 ·    │
│ All 66 /wk   ││ All 44%      ││ All 3.6%         ││ All 74        │
└──────────────┘└──────────────┘└──────────────────┘└───────────────┘
Rounds · Nov 2026
Round            Done  Self  Caller  Passed  Rejected  Pass rate
Intro               7     4       3       5         2  ████▌ 71%
Tech · Hiring · Panel · Final
───────────────
Home assessment     2     –       –       1         1  ███ 50%
3 held with no result yet · 2 still scheduled

How it compares · Nov 2026
                           This profile   Your other profiles   All profiles
Bids / week                         34                    28             66
Interviews / week                  1.2                   0.9            2.4
Interviews / bid                  3.4%                  2.9%           3.6%
Pass rate                          46%                   40%            44%
Tailor score                        81                    78             74
Tailored resumes / week             15                    11             22
Match score                         70                    68             66

[chart] Bids per week (self / bidder stacked bars) + interviews per bid (line, right axis)
[chart] Interviews per week by round type (stacked bars: Intro, Tech, Hiring, Panel, Final)
▸ Day-by-day numbers            (collapsed; "Week-by-week" in week view)
```

### Cards (quick read)

Four cards, each: the headline number, its split, and **one comparison line** `Yours X · All Y` (Yours = the owner's
other active profiles' average; All = every owner's active profiles). A ▲ / ▼ cue against "All" when it's loaded,
else against "Yours" — except the **Bids card, whose cue always compares with "Yours"** (owners run different numbers
of bidders; the "All" value is still shown). "All" shows a small skeleton while it loads and is left out if it fails.

| Card | Headline | Split / sub-line | Compared on |
|---|---|---|---|
| Bids | total bids | `N self · N bidder`, then `N / week` | bids per week |
| Interviews | total rounds (same count as today) | `N self · N caller`, then `N% pass` (or "No decided rounds") | pass rate |
| Interviews / bid | rate; faded under 30 bids with "Fewer than 30 bids — too few to rank" | — | interviews per bid |
| Tailor score | tailor average (— with none scored) | `N tailored` (resumes generated, scored or not) | tailor score |

The Tailored resumes tile and the Resume score tile are merged into the Tailor score card. Match score moves to the
comparison table.

### Needs attention (the verdict)

Up to three lines, ▼ first, then ▲; the section is hidden when none apply. Rule-based, worked out in the browser (so
lines that need "All" appear when it arrives; until then they compare with "Yours"). Thresholds:

1. **Weakest round** — among Intro…Final with ≥ 3 decided rounds, the lowest pass rate, if it is ≥ 15 points below
   the profile's overall pass rate: `▼ {Type} rounds pass {x}% ({best type with ≥ 3 decided} {y}%)`.
2. **Conversion** — with ≥ 30 bids: ≤ 0.75 × the comparison → `▼ {x}% interviews per bid vs {y}% across all profiles`
   (or "on your other profiles"); ≥ 1.25 × → ▲ with the same wording.
3. **Pass rate** — with ≥ 5 decided rounds: ≥ 15 points from the comparison → ▼ / ▲ `{x}% of rounds passed vs {y}%…`.
4. **Tailor score** — with ≥ 5 scored resumes: ≥ 5 points from the comparison → ▼ / ▲
   `Tailor score {x}, below / above all profiles ({y})`.
5. **Bid volume** — bids per week ≤ 0.5 × the owner's other profiles' → `▼ {x} bids / week vs {y} on your other
   profiles` (volume compares with the owner's own profiles only: other owners run different numbers of bidders).

### Tables (detail)

- **Rounds** (the existing card, extended): columns Round · Done · Self · Caller · Passed · Rejected · Pass rate.
  Self + Caller = Done. A round is a caller round when it has an enabled caller (the rule `split_rounds_by_day`
  already uses); home assessments show `–` for Self / Caller. Footnote unchanged.
- **How it compares** (new): rows Bids / week, Interviews / week, Interviews / bid, Pass rate, Tailor score, Tailored
  resumes / week, Match score; columns This profile · Your other profiles · All profiles. "Your other profiles"
  column and its card lines are omitted when the owner has no other active profile (`peers` null).

### Charts (trend)

- **Bids per {day|week}**: the existing stacked self / bidder bars, plus an **Interviews / bid line on a right axis
  in week view only** (a daily rate is noise). Weeks with 0 bids have no point.
- **Interviews per {day|week} by round type**: stacked bars Intro / Tech / Hiring / Panel / Final (home assessments
  stay out, so a day's bars add up to the Interviews count). Replaces the Total / Self / Caller lines; self vs caller
  lives in the cards and the Rounds table.
- On **All time** the view opens on Week (the saved choice still applies to the other periods).

### Day-by-day numbers

The existing table, inside a collapsed `<details>` ("Day-by-day numbers" / "Week-by-week numbers"), gains an
Interviews / bid column (blank with 0 bids).

## Fair comparisons (settled after the builder check; my calls, measured on live data 2026-11-13)

- **Compared window starts no earlier than the launch day** (`since_date()`, live: **2026-11-05**). Before it, self
  bids weren't recorded per profile (they read 0 while interviews still count), and tailor scores shifted ~10 points
  on Nov 4–5 (Luna extraction switch + tailor-score scope change). So when the range starts earlier, every "Yours" /
  "All" value, every Needs attention line and this profile's **Interviews / bid card** use `max(range start,
  launch day)` → range end, with the note "Compared from Nov 5, when self bids started being recorded per profile"
  under the range label. Totals on the cards (bids, interviews, tailored) and the Rounds table keep the full range.
  On All time this means comparisons cover Nov 5 → today; `lifetime()` isn't needed for the benchmark.
- **Per week over the weeks a profile existed** in the compared window (from `max(window start, its creation day)`,
  at least one week), not the window's full length — today `collect_range` divides every profile by the whole range,
  so a profile created mid-month is diluted.
- **Who's averaged**: active profiles with **at least one bid in the compared window**, every owner (admin and
  interview-manager owners included: user picked "every owner"). Idle profiles (live: 7 of 57 had no bids Nov 1–13)
  would pull per-week averages toward zero. The same rule applies to "Your other profiles". `profiles: N` counts them.
- **Rates and scores are pooled, volumes are per-profile means.** Interviews / bid = all interviews ÷ all bids of the
  averaged profiles; pass rate pooled (as today); tailor and match score = mean of every scored resume. Live Nov 1–13:
  pooled 0.54% vs per-profile mean 1.24% — tiny profiles skew a mean. Bids / week, interviews / week, tailored / week
  are means over the averaged profiles. This replaces `peer_average`'s means for `peers` too.
- The profile's own cards keep showing totals.

## Backend

- `GET /accounts/{id}/metrics` (existing) gains:
  - `rounds.byType[t]`: `self`, `caller` (done rounds by who took them; home assessments 0 / 0);
  - each `daily` / `weekly` entry: `intro`, `tech`, `hiring`, `panel`, `final` round counts (they add up to
    selfInterviews + callerInterviews);
  - `tailoredResumesPerWeek` on the row and on `peers`;
  - `comparedFrom` (ISO day: the compared window's first day, after the launch-day clamp) and the clamped
    `interviewsPerBid` for the card;
  - `peers` computed by the rules in "Fair comparisons" (pooled rates and scores, per-week means over bidding
    profiles).
- New `GET /accounts/metrics/benchmark?period=&date=&to=` (same period rules and validation as the profile endpoint;
  the compared window per "Fair comparisons"; `period=all` = launch day → today): `{start, end, label, profiles: N,
  bidsPerWeek, interviewsPerWeek, interviewsPerBid, passRate, tailorScore, matchScore, tailoredResumesPerWeek}`. One
  shared helper computes both this and `peers`, so the two columns follow the same rules. Any signed-in user may call
  it; it never returns names or ids. Route declared before `/{account_id}`.
- Cache: in-process, keyed by `(start, end)` of the compared window, **stale-while-revalidate**: an entry younger than
  15 minutes is served as is; an older one (kept up to 24 h) is served at once and refreshed in the background; with
  none, the request computes it (live: ~9 s for Nov 1–13, ~17 s for a full lifetime — the clamp keeps All time near
  the former). One `asyncio.Lock` per key so concurrent requests compute once. So "All" is slow only on the first open
  of a range after a deploy. Each Railway service keeps its own cache (fine).
- The profile endpoint loads interviews twice today (`collect_range` → `interview_rounds`, then `profile_rounds`);
  the builder may share one load if it's simple — not required.

## Frontend

- `ProfileMetricsPanel.tsx`: Needs attention, four cards, Rounds Self / Caller columns, How it compares, the two
  charts, collapsed day table. Benchmark via its own SWR key `['metrics-benchmark', period, date, to]`, shared by every
  profile's tab. The insight rules live in a pure function in `src/lib/profileMetrics.ts`
  (`attentionLines(m, benchmark | null) -> {dir: 'up' | 'down', text}[]`).
- `endpoints.ts`: `getMetricsBenchmark`, the new fields (optional, so an older server still renders: no Self / Caller
  columns, no round-type chart — falls back to today's interviews lines, no "All" values).

## Testing

- Backend (mongomock): rounds self / caller split per type; daily round-type counts add up to interviews; per-week
  over each profile's own weeks (a profile created mid-range isn't diluted); a range starting before the launch day
  compares from it (`comparedFrom`, clamped Interviews / bid) while totals keep the full range; idle profiles left out
  of both averages; pooled interviews / bid, pass rate and scores; benchmark has no names / ids and `period=all`
  starts at the launch day; cache: fresh entry served as is, stale entry served then refreshed, concurrent misses
  compute once; non-owner may call benchmark; bad period → 422.
- Frontend: `attentionLines` cases (each rule's threshold on both sides, low volume suppressed, max three, ▼ first,
  falls back to "Yours" without benchmark); lint and build; a read-only live check of one profile's numbers and the
  benchmark against a direct count.

## Left to the builder

Exact copy beyond the strings above, card / table spacing, chart colors for the five round types (from the existing
chart palette, distinct in light and dark), skeleton styling, mobile stacking (cards 2-up under `lg`, tables scroll
sideways).
