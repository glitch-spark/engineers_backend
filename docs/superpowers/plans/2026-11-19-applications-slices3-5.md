# Applications Insights — Slices 3–5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Below the "Your profiles" card on Statistics → Bids / Interviews: **What kind of job works** (slice 3), **What to change** + **Winning vs weakest profiles** (slice 4), **When to bid** + **Bidder quality** (slice 5).

**Architecture:** Each slice = one backend task (a pure module in `app/stats/` + one endpoint under `/stats/applications/…` reusing slice 2's period/subject handling and loader) and one app task (cards in `src/components/stats/`, rendered by `FramedTab` after `ProfilesCard` on bids and interviews). The period rows are loaded once per (start, end, region) and cached in-process 5 minutes, since four endpoints read the same rows.

**Tech Stack:** FastAPI + Beanie/Motor (mongomock tests); React + SWR + Tailwind (vitest).

**Spec:** `docs/superpowers/specs/2026-11-19-applications-insights-design.md` (Goals 4–7, Page items 3–7, "What to change" table, Decisions). Slices 1–2 (table, window, profile report, grades) are on the same branch: read `app/stats/profile_report.py`, `app/stats/profile_loader.py`, `app/routers/stats.py` (`applications_profiles`) and `src/components/stats/ProfilesCard.tsx` first; follow their patterns.

**Repos / branches:** backend `/home/eningeer/work/Engineer/engineers_backend`, app `/home/eningeer/work/Engineer/engineers_app`, both `feat/stats-insights`.

## Global Constraints

- Same query params and subject rule as `GET /stats/applications/profiles` (period, date, to, region, user; non-admin `user=<other>` → 403). Extract the shared "params → (subject, start/end naive-UTC bounds, label, today, now)" code from `applications_profiles` into one helper used by every endpoint (no copies).
- Cohort, rejected exclusion and matured cutoff exactly as slice 2: rows with `appliedAt` in the period; `bidStatus == "rejected"` excluded from every count except bidder quality's rejected share; matured = `appliedAt <= now - matured_days` (team window); per-100 = interviews among matured ÷ matured × 100, one decimal, None when matured is 0.
- "You" = rows on the subject's profiles (`accountId` in `owner_profiles(subject)` ids), the same set the profiles card uses. "Team" = all rows in the cohort and region.
- `MIN_GROUP = 30` matured rows: a group below it shows "too few" (per100 still returned, `tooFew: true`).
- Expected interviews (for judging, as slice 2): expected = matured × team per100 / 100; negative judgements only when expected ≥ 3.
- Copy is English, short, user-facing; every number shown with its count basis.
- `load_period_rows(start, end, region)` gets a 5-minute in-process cache keyed by its arguments (module dict + monotonic timestamp, `clear_cache()` for tests); it also returns fields later tasks need: `board, scoreBand, score, area, profileAgeDays, bidderId, templateName`.
- Never run tests or scripts against the real DB (backend `.env` is production). Commits single line, no co-author / "Generated with" trailers.
- Backend tests: `.venv/bin/python -m pytest -q -p no:cacheprovider tests_unit/<file>`; full suite with `--ignore=tests_unit/test_resume_experience_roles.py --ignore=tests_unit/test_style_serializer.py --ignore=tests_unit/test_template_annotate_removals.py` (2 known failures: test_bidder_ext upload_url_without_storage, test_resume_prompts bad_lists[prompts5-new-1]). App: `npx vitest run <file>`, then `npx vitest run`, `npx tsc --noEmit`.
- App cards: panel styling like `ProfilesCard`; own SWR key including period, date, to, region, user; loading / error / empty states like `ProfilesCard`; tables in `overflow-x-auto`; dark mode classes like siblings.

## Review Focus

1. Real data is sparse (≈ 0.5 interviews per 100 bids; ~95% of rows unscored; ~70% of rows area Unknown/Other): every breakdown must show its coverage ("score known for N%") and "too few" instead of a misleading rate (Tasks 1, 3, 5 tests on sparse fixtures).
2. A suggestion must never contradict the profile's grade on the profiles card (e.g. "below team" suggestion on a ⏳ profile) — suggestions use the same expected ≥ 3 rule (Task 3 `test_no_negative_suggestion_when_too_few`).
3. Leaderboard with fewer than 6 qualifying profiles → no profile appears in both lists (Task 3 `test_leaders_disjoint_when_few`).
4. Weekday / hour buckets use app-timezone time of `appliedAt` (stored naive UTC) — a 01:00 UTC Tuesday apply is a Monday evening apply in America/New_York (Task 5 `test_buckets_use_app_timezone`).
5. Bidder quality for a subject with no bidders → card hidden (empty list), not an empty table (Task 5/6 tests).

---

### Task 1: Backend — what kind of job works (slice 3)

**Files:** Create `app/stats/job_fit.py`; Modify `app/stats/profile_loader.py` (cache + extra fields), `app/routers/stats.py` (shared helper + `GET /stats/applications/jobs`). Test `tests_unit/test_job_fit.py`, `tests_unit/test_stats_jobs_endpoint.py`.

**Interfaces (produces):**
- `job_fit.breakdown(you: list[dict], team: list[dict], matured_before: datetime) -> dict` → `{"bySite": [...], "byScore": [...], "byArea": [...], "byAge": [...], "known": {"score": pct, "area": pct}}` (known = share of your non-rejected cohort rows with a score / with area not None, whole percent; None when you have no rows). Each group: `{"key", "label", "you": {"bids", "matured", "interviews", "per100", "tooFew"}, "team": {same}}`.
- Groupings (labels verbatim):
  - Site: the team's top 8 `board` values by team matured rows, each labelled via a map (`ashbyhq.com` Ashby, `greenhouse.io` Greenhouse, `myworkdayjobs.com` Workday, `workable.com` Workable, `lever.co` Lever, `bamboohr.com` BambooHR, `rippling.com` Rippling, `applytojob.com` JazzHR, `smartrecruiters.com` SmartRecruiters, `breezy.hr` Breezy, `oraclecloud.com` Oracle, `zohorecruit.com` Zoho Recruit, `icims.com` iCIMS, `teamtailor.com` Teamtailor, `linkedin.com` LinkedIn; otherwise the domain), then "Other sites", then "No job link" (board None). Order: as listed.
  - Score (`scoreBand`): strong "80+", good "65–79", fair "50–64", weak "Under 50", None "No tailored resume". Order: as listed.
  - Area: Backend, Full stack, Frontend, AI / ML, Data, DevOps, QA, Mobile, Other, then None as "Unknown". Groups where you and the team both have 0 rows are omitted.
  - Profile age (`profileAgeDays`): "Under 2 weeks" (< 14), "2–6 weeks" (14–41), "6 weeks – 3 months" (42–90), "3+ months" (≥ 91), None "Unknown".
- `GET /stats/applications/jobs` → `{label, subject, window, ...breakdown}`.

- [ ] **Step 1: Failing tests** — `test_site_groups_top8_other_and_no_link`; `test_site_labels`; `test_score_bands_and_no_resume`; `test_area_unknown_and_empty_groups_omitted`; `test_age_buckets_edges` (13 / 14 / 41 / 42 / 90 / 91 / None); `test_too_few_under_30_matured`; `test_rejected_excluded`; `test_known_percentages_sparse` (100 rows, 5 scored → score 5%); `test_loader_cache_reused_and_clearable`; endpoint: `test_jobs_shape`, `test_jobs_user_param_rules`, `test_jobs_region_scopes_team`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** (shared router helper first; `applications_profiles` switches to it with its tests still green).
- [ ] **Step 4: Run** — PASS; full suite.
- [ ] **Step 5: Commit** `feat: what kind of job works — interviews per 100 by site, score, area and profile age`

---

### Task 2: App — `JobFitCard` (slice 3)

**Files:** Modify `src/api/endpoints.ts` (types + `getJobFit`); Create `src/components/stats/JobFitCard.tsx` + test; Modify `src/components/stats/FramedTab.tsx` (render after `ProfilesCard`, bids + interviews only) + its test.

Layout: heading **What kind of job works**; sub-line `Interviews per 100 matured bids — yours vs the team, in four ways. Groups with fewer than 30 matured bids show "too few".` Four small tables in a 2-column grid (1 column on narrow screens): **By job site**, **By resume score** (caption `Score known for {known.score}% of your bids.`), **By job area** (caption `Area known for {known.area}% of your bids.`), **By profile age (when the bid went out)**. Columns: group label · Your bids (`you.bids`) · You (per100 or `too few`) · Team (per100 or `too few`). Highlight (`font-medium` + green/red text) "You" when you are not tooFew, team not tooFew, and you ≥ 1.5× team (green) or ≤ 0.5× team (red). Empty state when every group has 0 bids: `No job-level bids in this period yet.`

- [ ] **Step 1: Failing tests** — `renders four tables with labels and rates`; `shows too few`; `shows known-percent captions`; `highlights much better / much worse than team`; `empty state`; FramedTab renders JobFitCard on bids/interviews not ai-usage.
- [ ] **Step 2–4:** FAIL → implement → PASS; full vitest + tsc.
- [ ] **Step 5: Commit** `feat: Statistics "What kind of job works" card`

---

### Task 3: Backend — what to change + winning vs weakest (slice 4)

**Files:** Create `app/stats/changes.py`, `app/stats/leaders.py`; Modify `app/routers/stats.py` (`GET /stats/applications/changes`, `GET /stats/applications/leaders`). Tests `tests_unit/test_stats_changes.py`, `tests_unit/test_stats_leaders.py`, endpoint tests in `tests_unit/test_stats_changes_endpoint.py`.

**Interfaces:**
- Consumes: slice 2 `summarize`, `team_baseline`, `grade`; Task 1 site labels + `breakdown` (team best sites); loader (cached) for the current and previous period (`frame.previous_window`-style: the same-length window just before).
- `changes.suggest(profile: dict, summary: dict, grade: str, ctx: dict) -> list[dict]` — each `{"pattern", "text", "evidence"}`; `ctx` carries team per100, team median days, team avg score, team best sites (top 2 by team per100 among sites not tooFew), the profile's rows split (self/bidder, by site, scores), previous-period summary, matured_days, quiet-now flag, bidder names. Patterns (spec table; text verbatim, placeholders filled; at most 3 per profile, ordered as listed):
  1. `dead` (grade dead): `Archive it, or rebuild it with a new base resume / prompt and judge it fresh after {matured_days} days.`
  2. `low_scores` (below team with expected ≥ 3, ≥ 10 scored rows, avg score < team avg − 5): `Low interview rate and low resume scores (avg {avg} vs team {team}) — improve the base resume or the role's prompt; check it on Prompt tests.`
  3. `wrong_sites` (below team with expected ≥ 3, and not low_scores): `Good resumes, wrong places — move bids to {site1} and {site2}, where the team converts best{; drop {worst} if a site of this profile got 0 interviews where the team rate gives ≥ 3 expected (its matured × team per100 / 100), and it is not a named best site}.`
  4. `stuck_intro` (grade reason "intros don't reach tech"): `Intros don't turn into tech rounds — tone the prompt down to real skills and add interview prep.`
  5. `slow` (median days ≥ 2 × team, team median ≥ 1): `Interviews come slowly ({median} days vs team {team}) — sharpen the summary; try a winning profile's template.`
  6. `bidder_gap` (self matured ≥ 30, self matured interviews ≥ 3, bidder matured ≥ 30, self per100 ≥ 2 × bidder per100, bidder expected ≥ 3 at the self rate): `Your own applies convert better than your bidders' ({si} of {sm} vs {bi} of {bm} matured bids) — review {bidder}'s jobs and rejected share.` ({bidder} = the bidder with the most matured rows on this profile.)
  7. `volume_up_rate_down` (matured up ≥ 25% vs previous period and per100 down ≥ 25%, expected ≥ 3 in both): `More bids, fewer interviews per bid — fewer, better-matched applies (score {band label} converts best for the team).`
  8. `quiet_now` (period includes today; the profile has a non-rejected row in the last 7 days; the profile (createdAt = appliedAt − profileAgeDays; first row when unknown) older than matured_days + 21 days; and **no interview round in the last 21 days from any interview on that profile** — query `interviews` by `accountId` with a round `scheduledAt` in the last 21 days, matched or not): `No interview on this profile for 21+ days while it's still bidding — check the base resume and the jobs it's applied to.`
  No negative suggestion (2, 3, 5, 6, 7) on a profile graded `early`.
- `GET /stats/applications/changes` → `{label, subject, profiles: [{accountId, name, grade, suggestions: [...]}]}` (profiles with ≥ 1 suggestion, card order like the profiles card).
- `leaders.pick(team_profiles: list[dict], min_matured: int = 150) -> dict` → `{"best": [...], "weakest": [...], "groups": {"best": agg, "weakest": agg}}`. Qualifying = matured ≥ 150 in the cohort; best = top 3 by per100 (ties: more matured first), weakest = bottom 3 among the rest (disjoint). Each profile: `{accountId, name, owner, region, matured, interviews, per100}`. Group agg: `{matured, interviews, regions: {region: n}, avgScore, scored, areas: [{label, share}] top 3 + areaKnown pct, medianProfileAge, sites: [{label, share}] top 3, medianDays, interviewCount, daysTooFew (interviewCount < 10), selfShare}`.
- `GET /stats/applications/leaders` → `{label, window, best, weakest, groups, note: "These are patterns, not proof."}` — visible to every signed-in user (names included, user decision); region filter applies; ignores `user`.

- [ ] **Step 1: Failing tests** — one per pattern (fires / does not fire at its edge), `test_no_negative_suggestion_when_too_few`, `test_max_three_suggestions_in_order`, `test_quiet_now_counts_unmatched_interviews`, `test_quiet_now_only_when_period_includes_today`; leaders: `test_best_and_weakest_by_per100`, `test_leaders_disjoint_when_few`, `test_min_150_matured`, `test_group_aggregates` (shares, medians, daysTooFew); endpoints: shape, subject rule (changes), leaders visible to staff.
- [ ] **Step 2–4:** FAIL → implement → PASS; full suite.
- [ ] **Step 5: Commit** `feat: what to change suggestions and winning vs weakest profiles`

---

### Task 4: App — `ChangesCard` and `LeadersCard` (slice 4)

**Files:** `src/api/endpoints.ts` (types + `getChanges`, `getLeaders`); Create `src/components/stats/ChangesCard.tsx`, `src/components/stats/LeadersCard.tsx` + tests; Modify `FramedTab.tsx` (order: Profiles, Job fit, Changes, Leaders) + test.

- **ChangesCard** — heading **What to change**; one block per profile: name + grade badge (reuse ProfilesCard's badge component — extract it to a shared file if it's local) and a bullet list of suggestion texts. Empty: `Nothing to change right now — no profile shows a clear problem in this period.`
- **LeadersCard** — heading **Winning vs weakest profiles**; sub-line `The team's 3 best and 3 weakest profiles by interviews per 100 matured bids (150+ matured bids each).` Two columns (stack on narrow): **Winning** / **Weakest**; each: profile list (name · owner · region · matured · interviews · per100), then the group summary rows: Matured bids · interviews; Region mix; Avg resume score (`{avg} ({scored} scored bids)` or `no scores`); Job areas (top 3 shares + `area known for {pct}%`); Profile age when bidding (median, in days/weeks); Job sites (top 3 shares); Median days to interview (`{n} days ({count} interviews)` or `too few`); Self / bidder mix (`{self}% self`). Footer note text from the response. Empty (no qualifying profile): `No profile has 150+ matured bids in this period yet.`

- [ ] **Step 1: Failing tests** — changes: renders profiles and bullets, empty state; leaders: both columns, group rows, too-few days, empty state; FramedTab order.
- [ ] **Step 2–4:** FAIL → implement → PASS; full vitest + tsc.
- [ ] **Step 5: Commit** `feat: Statistics "What to change" and "Winning vs weakest profiles" cards`

---

### Task 5: Backend — when to bid + bidder quality (slice 5)

**Files:** Create `app/stats/timing.py`; Modify `app/routers/stats.py` (`GET /stats/applications/timing`). Tests `tests_unit/test_stats_timing.py` + endpoint tests.

**Interfaces:**
- `timing.by_time(you, team, matured_before, tz) -> {"weekday": [...], "hours": [...]}` — weekday groups Mon–Sun (labels `Mon`…`Sun`), hour blocks of 3 hours labelled `00–03`, `03–06`, … `21–24`, from `appliedAt` converted naive-UTC → `tz` (`app_tz()`); group shape as Task 1 (`you`/`team` with bids, matured, interviews, per100, tooFew).
- `timing.bidders(rows_incl_rejected, bidder_names, matured_before, team_bidder_per100) -> list` — per bidder of the subject (`Bidder.createdBy == subject`, any bidder with rows in the cohort on the subject's profiles): `{bidderId, name, bids (non-rejected), matured, interviews, per100, tooFew, rejectedShare (rejected ÷ all rows incl. rejected, whole percent)}`, sorted by bids desc; `team_bidder_per100` = median per100 over all team bidders with matured ≥ 30 (None otherwise).
- `GET /stats/applications/timing` → `{label, subject, weekday, hours, bidders, teamBidderPer100}`; bidders list empty when the subject has none.

- [ ] **Step 1: Failing tests** — `test_buckets_use_app_timezone`; `test_weekday_and_hour_labels`; `test_too_few`; `test_bidders_rejected_share_and_per100`; `test_team_bidder_baseline`; `test_no_bidders_empty`; endpoint shape + subject rule.
- [ ] **Step 2–4:** FAIL → implement → PASS; full suite.
- [ ] **Step 5: Commit** `feat: when to bid by weekday and hour, and bidder quality`

---

### Task 6: App — `TimingCard` and `BiddersCard` (slice 5)

**Files:** `src/api/endpoints.ts` (types + `getTiming`); Create `src/components/stats/TimingCard.tsx`, `src/components/stats/BiddersCard.tsx` + tests (both fed from one SWR fetch — fetch in a small parent `TimingSection.tsx` or in `FramedTab`; one request); Modify `FramedTab.tsx` (after LeadersCard) + test.

- **TimingCard** — heading **When to bid**; sub-line `Interviews per 100 matured bids by when the bid went out ({timezone label}).` Two tables (weekday, 3-hour blocks) with columns label · Your bids · You · Team, same too-few/highlight rules as JobFitCard (reuse its row/highlight component — extract a shared `RateTable` if JobFitCard has one inline).
- **BiddersCard** — heading **Bidder quality**; sub-line `Your bidders' bids in this period: how many, how they convert, and how many Bid Track rejected.` Table: Bidder · Bids · Matured · Interviews · Per 100 (or too few) · Rejected (`{n}%`, red when ≥ 20%); footer `Team bidder median: {x} per 100.` Hidden entirely when `bidders` is empty.

- [ ] **Step 1: Failing tests** — timing tables render with too few; bidders table + rejected red; bidders hidden when empty; one request for both cards; FramedTab order.
- [ ] **Step 2–4:** FAIL → implement → PASS; full vitest + tsc.
- [ ] **Step 5: Commit** `feat: Statistics "When to bid" and "Bidder quality" cards`
