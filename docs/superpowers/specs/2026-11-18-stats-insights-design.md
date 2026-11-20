# Statistics: "how am I doing, and what should I change" — design

Date: 2026-11-18. Repos: engineers_backend (FastAPI + Beanie) and engineers_app (React + SWR + Recharts).
Mockup (local, approved): `engineers_app/.superpowers/brainstorm/*/content/stats-bids.html`.

## Why

Today's Statistics tabs show totals, per-person tallies and generic charts (one bar per person per day, a bids-vs-
interviews scatter). They show volume but don't tell a person whether they're doing well or what to change, and the
charts are awkward to read. The user wants every tab to answer, for the selected period:

1. **How am I doing?** — my numbers.
2. **Good or bad?** — clearly compared with the rest of the team.
3. **If bad, how do I improve?** — concrete insights.

## Goals

- One shared frame on every tab: **You vs team** header → **What to work on** (tips) → **Everyone** (ranked) →
  **Trend** (you vs team median).
- Tips are **rule-based** (deterministic, each shows the numbers behind it). Phase 2 adds a short **AI summary** on top
  that reads the same tips + numbers.
- Admins can view any teammate ("Viewing: X ▾"); staff always see themselves. The ranked list stays visible to all.
- AI usage stays visible to everyone (user decision).
- Tabs: Bids, Interviews, **Profiles (new)**, Callers, Resumes (was Resume compare), Earnings, AI usage.
- Folds in the accepted review fixes: one shared filter bar on every tab (Earnings included), definitions in a collapsed
  "How these are counted" instead of paragraphs above the data, readable rates ("4.5 per 100 bids", not "0.45%"),
  viewed person / selections in the URL.

## Non-goals

- New data collection — every metric comes from data already stored.
- Changing Home, Plans, Slack posts or pay computation.
- Exports beyond what exists (Callers PDF stays).

## Anti-goals

- A tip that is wrong or flaps on noise. No tip when there isn't enough data (see "Minimum data").
- Two definitions of the same number: a metric on Statistics must match Home / Plans for the same person and days.
- Staff seeing another person's tips or header.

## Approaches considered

1. **Server computes each tab's view (chosen).** `GET /stats/{tab}` returns me / team distribution / rank / everyone /
   trend / tips. Rules in Python per tab, unit-tested; one generic frame in the app. The AI summary reuses the tips.
2. Browser computes from existing endpoints — rejected: rules unreachable by the AI summary, raw team data shipped to
   every browser, metric definitions drift (the Home vs Statistics bids mismatch already happened).
3. Nightly snapshot — rejected: day-old numbers, can't serve custom ranges, more moving parts.

## Build order (each slice ships alone)

1. Frame + endpoint pattern + **Bids**
2. **Interviews**
3. **Profiles** (new)
4. **Callers**
5. **Resumes**
6. **Earnings** + **AI usage**
7. **AI summary** (phase 2)

Old tabs keep working until their slice replaces them.

## The shared contract

`GET /stats/{tab}?period=week|month|custom&date=YYYY-MM-DD&to=YYYY-MM-DD&region=all|Latam|EU|US&user=<id>`
(same period/region semantics as today's `/metrics/interview-rates`). `user` is admin-only; a non-admin sending another
user's id gets 403 (same rule as `dashboard._subject_id`). Response:

```
{
  "tab": "bids", "label": "Nov 1 – 18", "subject": {"userId", "name"},
  "headline": [                       # 3–4 numbers for the header, in order
    {"key": "bids", "label": "Your bids", "value": 519, "format": "count",
     "team": {"median": 646, "p25": 560, "p75": 734, "min": 482, "max": 789, "n": 9},
     "rank": 7, "of": 9, "vsMedian": -0.197, "higherIsBetter": true, "note": "Self 214 · bidder 305"}
  ],
  "tips": [ {"id": "bids.low_self_share", "severity": "bad|watch|good", "text": "...", "detail": "...",
             "evidence": {...numbers...}} ],          # 0–4, ordered bad → watch → good, then by impact
  "everyone": {"metric": "bids", "rows": [{"userId", "name", "value", "vsMedian", "rank", "parts": {"self": 214, "bidder": 305}, "isSubject": true}]},
  "trend": {"unit": "week|day", "buckets": [{"label": "Nov 3", "you": 120, "median": 150}]},
  "notEnoughData": false,
  "definitions": ["Self bids are …", "…"]              # shown in "How these are counted"
}
```

- **Team** = active non-admin users with any activity for the tab's metric in the period (the subject is always
  included). Median / quartiles over that set. Rank: higher value = better unless `higherIsBetter` is false; ties
  share a rank.
- **Trend**: weekly buckets when the period spans ≥ 14 days, else daily; each bucket has the subject's value and the
  team median for that bucket.
- **Minimum data**: `notEnoughData = true` and no tips when the team has < 3 people with activity, or the period has
  < 3 working days (Mon–Fri) so far. Headline and ranking still show.
- Tip text is rendered server-side (plain English with the numbers); `evidence` carries the raw numbers for the UI and
  the AI summary.

## Per-tab metrics and rules

Thresholds are starting values (constants in one place per tab, easy to tune).

### Bids
- Headline: total bids; team median; rank; per working day vs the plan's daily target (sum of `day_targets`).
- **Self bids count every stored day** (drafts included, same as Home's This week) — fixes today's mismatch.
  Bidder bids = Bid Track approved + pending.
- Everyone: total bids, split self / bidder on hover.
- Rules: `low_self_share` (self share < team median share − 15 pts, bad); `weak_weekdays` (a weekday's avg < 60% of
  the person's other weekdays, ≥ 2 weeks of data, watch); `idle_profiles` (an active profile with 0 bids in the last
  7 days, watch); `behind_target` (per-day < 85% of daily target, bad); `up_vs_last_period` (≥ +15%, good).

### Interviews
- Headline: interviews held; **interviews per 100 bids** vs team; rank; stage reach (share reaching tech / final).
- Rules: `low_conversion` (per-100 < 60% of team median, bad); `stalls_at_stage` (pass-through from a stage < 60% of
  team's for that stage, ≥ 4 rounds, watch); `caller_vs_self` (caller-taken rounds pass ≥ 15 pts more than self-taken,
  watch: "consider a caller for X rounds"); `up_vs_last_period` (good).

### Profiles (new)
- Per profile of the subject: bids → interviews → per 100 bids; region.
- Headline: best profile's per-100, worst active profile's, profiles with 0 interviews.
- Rules: `dead_profile` (≥ 150 bids and 0 interviews in 4 weeks, bad); `region_gap` (a region converts ≥ 2× another
  for this person, watch); `idle_profile` (as Bids).

### Callers
- For callers: their pass rate vs team caller median, by round type. For a non-caller subject: pass rate of their own
  rounds vs caller rounds on their interviews.
- Rules: `weak_round_type` (a round type's pass rate < team's − 15 pts, ≥ 5 decided, bad); `awaiting_backlog`
  (> 5 rounds awaiting a result > 7 days, watch).

### Resumes
- Headline: avg resume score on jobs that got an interview vs jobs that didn't; team avg.
- Rules: `score_gap` (interviewed avg − not-interviewed avg ≥ 10: "higher scores win; aim ≥ X", watch);
  `missing_keywords` (top 3 keywords most often missing in the subject's resumes, watch); `low_avg_score` (< team −
  10, bad).

### Earnings
- Headline: net this period vs team median and vs the subject's last 3 periods average.
- Rules: `below_own_average` (< 80% of own 3-period average, watch); `negative_month` (bad).

### AI usage (visible to everyone)
- Headline: the subject's cost; cost per resume; cost per interview; team medians.
- Rules: `expensive_model` (cost per resume ≥ 2× team median and a cheaper model in the team's top 3, watch).

## The app (frame)

- One `StatsFrame` renders any tab's contract: filter bar (Viewing ▾ for admins, Region, Period) → header (headline
  numbers + "where you sit" range bar: min–max track, p25–p75 shaded, median tick, subject dot) → What to work on (tips
  with severity dot, text, detail; "Not enough data yet" state) → Everyone (horizontal ranked bars, subject highlighted,
  vsMedian %, admins click a name to view them) → Trend (two lines: subject solid, median dashed) → "How these are
  counted" (collapsed).
- `user`, `tab`, `region`, `range` live in the URL. Staff: no Viewing picker.
- Phones: header numbers wrap 2×2; ranked list stays one column.
- Slice-specific extras can sit below the frame (e.g. Resumes keeps a slimmer job × person table; Callers keeps its
  round list + PDF).

## Phase 2: AI summary

- `GET /stats/{tab}/summary?…same params` → 2–3 sentences from the tab's tips + headline evidence, via
  `app.llm_router.complete` (new purpose `stats_summary`, logged to AI usage), cheapest capable model in the catalog.
- Cached per (subject, tab, period, region, day); no call when `notEnoughData` or no tips. Shown under the tips as
  "Summary"; the frame renders fine without it.

## Constraints

- Period presets, region values and the "today" convention as today's Statistics.
- Admin check like `dashboard._subject_id`.
- Additive: old endpoints stay until their tab is replaced.
- Tests: backend `tests_unit/` (mongomock) per rule and per endpoint; app vitest per frame part.

## Decisions from the builder check (my calls — check these)

1. **Self bids** = `done.bidsSelf` on every stored DailyPlan in the period, `loggedAt` set or not (Home's rule). One
   shared counts loader serves every `/stats` tab (Bids and Interviews use the same bids). The old
   `/metrics/interview-rates` keeps its logged-only rule until its tabs are replaced. (Draft saves never write `done`;
   tracked self bids write it with `loggedAt` — so in practice this only adds days typed then left unposted.)
2. **Working days** = Mon–Fri, as in plans and pace. A Mon–Fri day with a 0 target still counts as a working day.
3. **`behind_target`**: target = Σ `day_targets(plan, weekly, d)` (bids) over Mon–Fri days in the period up to today;
   actual = bids on those days. Skipped when that target is 0 (no weekly plan).
4. **Region**: same semantics as today's `interview_rates.counts` (per-region self field, bidder bids by profile
   region; profiles without a region are excluded under a region filter). Team set and every rule work within the
   chosen region.
5. **Team set** = users with `role != "admin"` and activity for the tab's metric in the period/region. The subject is
   always shown; when the subject is outside that set (e.g. an admin, or zero activity), their rank reads
   "would rank N of M" and they're not added to the median/quartiles.
6. **Previous period** (`up_vs_last_period`) = the same number of elapsed days immediately before, aligned to the
   period type: this month so far (Nov 1–18) vs Oct 1–18; this week so far vs the same weekdays last week; custom
   range vs the equal-length window right before it. Earnings' "own average" = the 3 previous full periods of the same
   type.
7. **Computed live**, no cache in the slices (one team loader per request, like `interview_rates` today). Phase 2's AI
   summary is the only cache.
8. **URL**: keep today's keys (`tab`, `region`, `range`, `from`, `to`) plus `user`; the app maps them to
   `period/date/to` as it does today. Old `tab=resume-compare` maps to `resumes`; `?caller=` keeps working.
9. **Weekday rule** uses total bids per report day: self bids by plan date, bidder bids by `bid_report_day` (the day
   basis Bid Track pay and plans already share).

## Left to the builder

- Module layout on the server (e.g. `app/stats/frame.py` + `app/stats/<tab>.py`), chart library choices inside the
  existing Recharts setup, exact tip wording within the stated meaning, how slice extras sit below the frame.
