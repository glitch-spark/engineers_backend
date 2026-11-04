# Profile metrics (bids, interviews and resume scores per profile)

Date: 2026-11-03 · Branch: `feat/new-feature-2` (backend from `dev`) · Frontend: `engineers_app`, same branch name
(from `main`)

## Goal

Each profile (an `accounts` document, "Profiles" in the app) shows its key numbers, so its owner can see how each
profile is doing and compare their profiles with each other:

- bids per week, self + bidder;
- interviews per week, self + caller, and interviews per bid;
- the average tailor score of tailored resumes and the average job-match score of non-tailored resumes.

Self bids are not recorded per profile today (the Daily Plan holds a per-user total split by region), so the Daily
Plan's Done form changes to take self bids per profile.

Out of scope:
- comparing with other users' profiles, or a team benchmark (the Profiles page stays scoped as it is);
- per-profile goals (the morning goal stays per region, from the weekly plan);
- per-profile numbers before launch (self bids per profile exist only from launch on);
- typed bidder bids per profile (bidder bids come from Bid Track).

## Who sees it

Same as the Profiles page: a user sees their own profiles; an admin sees any user's through the existing user filter.
`GET /accounts/{id}/metrics` follows `GET /accounts/{id}` (owner or admin, else 404). Plans keep their rules
(`routers/_plan_access.py`): only the owner writes a plan.

## 1. Daily Plan: self bids per profile

**Stored.** `DailyPlan.done.bidsSelfByProfile: Dict[str, int]` (accountId hex → count, each ≥ 0). `goal` is
unchanged. Plans saved before launch have no `bidsSelfByProfile` and read as before.

**Done form.** The three inputs Self (US) / Self (EU) / Self (LATAM) are replaced by one input per **active profile
the plan's owner created**, labelled `Self · <profile name> (<region>)`. An archived profile is listed only when that
day already holds a number for it. Bidder stays one input. The Self total below shows the region breakdown.

**Regions are derived on save, server-side.** For each entry, the profile's `region`:

| Profile region | Adds to |
|---|---|
| `US` | `bidsSelfUs` |
| `EU` | `bidsSelfEu` |
| `Latam` | `bidsSelfLatam` |
| `Canada`, `Asia`, none | none of the three |

`bidsSelf` = the sum of all `bidsSelfByProfile` values. `_SelfTotal.self_total_from_regions` (which sets
`bidsSelf` to the regions' sum) must not run over a by-profile save, or Canada / Asia / no-region bids would be
dropped from the total: a by-profile body sets the total from the profiles instead. Region-only bodies (no
`bidsSelfByProfile`) keep today's behaviour.

Everything reading the region fields keeps working unchanged: Statistics (`interview_rates.py`), the dashboard, the
Slack post (`slack_bot.py` `_REGION_LABELS`) and the week card.

**Validation.** Every key must be the id of a profile the plan's owner created (archived allowed), else 422 naming
the id. Admins still get 403 on writes.

**Older days.** A day with region values and no `bidsSelfByProfile` shows its region numbers read-only in the row.
Editing it opens the per-profile inputs at 0, with the old region numbers shown as a hint; saving replaces them with
the derived ones.

**API.** `DayIn.done` accepts `bidsSelfByProfile`. The day and week responses return it in `done`, plus
`profiles: [{id, name, region, archived}]` (the owner's profiles for the form), so the form needs no second request.

## 2. Metrics and their window

**Window.** Last **4, 8 or 12** weeks (default 4). Weeks are Monday–Sunday on app-local days
(`dashboard_stats.app_tz`), as the weekly plans. Only **completed** weeks count: the current week is left out.

**Launch clamp.** The span starts at the latest of: the window's first Monday; the first Monday on or after
the launch day — `PROFILE_METRICS_SINCE` when set, otherwise the day after this release first started on the
deployed backend (recorded once in `app_settings`; nothing is measured before that); the Monday of the week holding the
profile's `createdAt` (local day). All of a profile's metrics use this one span, so
bids and interviews always cover the same days. **Per week** = total ÷ weeks in the span. A span of 0 weeks returns
`null` metrics (UI: "Not enough history yet").

| Metric | Definition |
|---|---|
| Self bids | Sum of `done.bidsSelfByProfile[profile]` over the owner's logged days (`loggedAt` set) in the span |
| Bidder bids | `Bid`s with `status` in {approved, pending} whose bidder (`Bidder.profileId` = profile; archived bidders included) is assigned to the profile, on the bid's local report day (from `firstAt`, as Bid Track counts days) |
| Bids | self + bidder |
| Self / caller interviews | Rounds held (`interview_rounds`) of interviews with `accountId` = profile, split self / caller by `split_rounds_by_day` |
| Interviews per bid | all rounds ÷ all bids in the span; `null` at 0 bids; `lowVolume` when bids < 30 (`interview_rates.MIN_BIDS`) |
| Tailor score | mean `matchScore.total` of every tailored resume generated for the profile in the span (Resume Generator and Job Applies, applied with or not): completed `resumeapplications` with this `accountId`, no `fixedFrom`, by `createdAt`. `tailoredResumes` counts them all, scored or not |
| Job-match score | mean score of applies with a non-tailored resume in the span: log entries with `resumeId` and a `score`, plus log entries whose `tailoredJobId` record has `fixedFrom` (a fixed-resume fork) and a `matchScore` |

Both scores are returned with `n` (how many entries the mean covers) and shown whenever `n > 0`: a profile can use
both kinds. Unscored entries are skipped. A bidder moved to another profile takes their bid history with them (known,
accepted).

## 3. Backend

**`app/profile_metrics.py`** (like `app/interview_rates.py`):

- `span(weeks, today, since, created) -> (start, end, n_weeks)`: pure; the window and clamp above.
- `self_bids(owner_id, account_ids, start, end) -> {(accountId, monday): n}`.
- `bidder_bids(account_ids, start, end) -> {(accountId, monday): n}`.
- `interview_rounds(account_ids, start, end) -> {(accountId, monday): [self, caller]}` (reuses
  `app/interview_rounds.py`).
- `scores(account_ids, start, end) -> {accountId: {"tailored": [...], "match": [...]}}`.
- `summarize(...)`: one row per profile; per-week averages, ratio, score means with `n`, `lowVolume`.

Each loader runs one query per collection for all the owner's profiles; nothing loops per profile. Each profile's
span differs only by its creation date, so loaders read the widest span and `summarize` clips per profile.

**Endpoints** (`app/routers/accounts.py`, declared before `/{account_id}`):

- `GET /accounts/metrics?weeks=4&userId=` → every active profile of the caller, or of `userId` (admins only; a
  non-admin passing another user gets 403):

  ```json
  {"start": "2026-10-06", "end": "2026-11-01", "weeks": 4,
   "rows": [{"accountId": "...", "spanWeeks": 4,
             "bidsPerWeek": 42.0, "selfBidsPerWeek": 30.0, "bidderBidsPerWeek": 12.0,
             "interviewsPerWeek": 2.5, "selfInterviewsPerWeek": 2.0, "callerInterviewsPerWeek": 0.5,
             "interviewsPerBid": 0.0595, "lowVolume": false,
             "tailorScore": {"avg": 81.2, "n": 37}, "matchScore": {"avg": 74.0, "n": 12}}]}
  ```

- `GET /accounts/{id}/metrics?weeks=4` → that profile's row, plus
  `weekly: [{"week": "2026-10-06", "selfBids", "bidderBids", "selfInterviews", "callerInterviews"}]` and
  `peers`: the same row's fields averaged over the owner's **other** active profiles (`null` with no other profile).

`weeks` outside {4, 8, 12} → 422.

**Config.** `PROFILE_METRICS_SINCE` in `app/config.py` and `.env.example`.

## 4. Frontend (`engineers_app`)

**API** (`src/api/endpoints.ts`): `getProfileMetrics({weeks, userId})`, `getProfileMetric(id, {weeks})`, types
`ProfileMetricsRow` and `ProfileMetricDetail`; Daily Plan `Counts` gains `bidsSelfByProfile?: Record<string, number>`.

**Profiles list** (`src/pages/Accounts.tsx`):
- a Period picker (4 / 8 / 12 weeks) in the toolbar, kept in `localStorage` (wrapped in try/catch);
- new columns after Region: **Bids/wk** (`42`, `30 self · 12 bidder` below), **Interviews/wk** (`2.5`,
  `2 self · 0.5 caller` below), **Intv/bid** (`6.0%`, "low volume" tooltip under 30 bids), **Score**
  (`T 81 · M 74`, tooltip with each `n`). Country folds into the Region cell (flag) to keep the table's width;
- clicking a metric header sorts the visible rows;
- metrics load through their own SWR key `['account-metrics', weeks, userId]`; cells show a skeleton while loading,
  `—` and one inline error if the call fails; the list never waits on them.

**Profile page** (`src/pages/AccountEdit.tsx`, existing profiles only): `ProfileMetricsPanel`
(`src/components/profiles/ProfileMetricsPanel.tsx`) as the first `panel` section: the same Period picker; four tiles
(Bids/wk, Interviews/wk, Interviews/bid, Avg scores), each with its split and a "vs. your other profiles" line from
`peers` with an up/down cue (hidden when `peers` is null); a Recharts weekly chart below (stacked self / bidder bid
bars, interview-rounds line).

**Daily Plan** (`src/components/report/GoalDoneFields.tsx`, `DayRow.tsx`, `src/lib/reportWeek.ts`): Done renders one
input per profile from the day response's `profiles`; the Self total shows the derived region split through a
`regionOfProfile` helper matching the server's table; `DayRow` shows per-profile values when present and the region
display for older days. Goal inputs unchanged.

## 5. Errors

- Missing scores, 0 bids, 0-week span → `null`; the UI shows `—` with a tooltip. No division by zero.
- A deleted profile's key in `bidsSelfByProfile` is ignored by metrics.
- A metrics failure never blocks the Profiles list or the profile form.

## 6. Testing

Backend, `tests_unit/` with mongomock-motor (as `test_interview_rates.py`):

- `test_profile_metrics.py`: `span()` clamps (launch date, creation mid-week, completed weeks only, 0 weeks); self
  bids from logged days only; bidder bids approved + pending, not rejected, archived bidders included; rounds held
  split self / caller; interviews per bid with `null` at 0 bids and `lowVolume`; tailor vs job-match scores with
  fixed forks as job-match and unscored entries skipped; peers exclude the profile, `null` for a single profile.
- `test_profile_metrics_router.py`: response shape, `weeks` validation, 403 for another user's `userId` (non-admin),
  404 for another user's profile.
- `test_daily_plan_by_profile.py`: derived region fields; Canada / Asia / no region count in `bidsSelf` only;
  `_SelfTotal` keeps them; 422 for a profile that isn't the owner's; region-only plans still read and save as
  before; day response includes `profiles`.

Frontend: `npm run lint` and `npm run build` pass; manual check in the local app with seeded data of the Profiles
list, the profile page and the Daily Plan Done form.
