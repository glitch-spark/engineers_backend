# Statistics "Conversion" Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Statistics Bids and Interviews tabs with one **Conversion** tab: totals + five breakdown tables (Bidder, Job site, Resume score, Job area, Profile age) with a My profiles / One profile / Team total scope, data from Oct 6, and the existing Your profiles / What to change / When to bid cards following the same filters.

**Architecture:** Backend: the applications sync attaches tailored-resume fields to bid rows; a shared scope helper turns (subject, scope, profile, region, period) into the row set and the team row set; a new `GET /stats/conversion` returns totals + breakdowns; `/profiles`, `/changes`, `/timing` take the same scope. App: a new `ConversionTab` replaces BidsTab / InterviewsTab and wires the cards; `JobFitCard`, `LeadersCard` and their endpoints are deleted.

**Tech Stack:** FastAPI + Beanie/Motor (mongomock tests); React + SWR + Tailwind (vitest).

**Spec:** `docs/superpowers/specs/2026-11-19-conversion-tab-design.md` (read all of it, especially "Key definitions", "Data fix", "Endpoint", "Page" and "Decisions from the builder check" 1–9).

**Repos / branches:** backend `/home/eningeer/work/Engineer/engineers_backend`, app `/home/eningeer/work/Engineer/engineers_app`, both `feat/stats-insights`. Read first: `app/applications/sync.py`, `rows.py`, `app/routers/stats.py` (`_scope`, `_bounds`, `_graded_profiles`, the `applications_*` routes), `app/stats/profile_loader.py`, `job_fit.py`, `timing.py`, `changes.py`, `profile_report.py`; app `src/pages/Statistics.tsx`, `src/components/stats/*`, `src/components/statistics/statsFilters.tsx`, `src/lib/dateRange.ts`.

## Global Constraints

- `DATA_START = date(2026, 10, 6)`: every stats period used by this tab starts no earlier; a range entirely before it returns an empty report with `startClamped: true`.
- Rows = `applications` rows, non-rejected, `byInterviewOnly` excluded (already done by the loader). Matured = `appliedAt <= now - matured_days`; rate = matured interviews ÷ matured × 100 (one decimal), None when matured 0; `few` = matured < 30.
- Scope rows: `me` = rows on the subject's **non-archived** profiles whose **current** Account region matches (`all` = any); `profile` = one of those (other users' → 403 like `_subject_id`; admin any); `team` = rows on all non-archived profiles of the region. Team baseline (grades, suggestions, "team" columns) = the `team` row set for the same period.
- Bidder table omitted for `team`. Bidder group labels: `Self` (by == "self") first, then each bidder by bids desc (Bidder.name; missing name or bidderId → `Unnamed bidder`).
- Score groups: `80+`, `65–79`, `50–64`, `Under 50`, `No tailored resume`. Site/area/age groups and labels as `app/stats/job_fit.py` (sites ranked by bids).
- Tooltip copy for `few`: `Only {n} matured bids — this rate can swing a lot.`
- Header caption: `Bids counted from Plans and Bid Track, all profiles.`
- Never run tests or scripts against the real DB (backend `.env` is production). Commits single line, no co-author / "Generated with" trailers.
- Backend tests: `.venv/bin/python -m pytest -q -p no:cacheprovider tests_unit/<file>`; full suite with `--ignore=tests_unit/test_resume_experience_roles.py --ignore=tests_unit/test_style_serializer.py --ignore=tests_unit/test_template_annotate_removals.py` (2 known failures: test_bidder_ext upload_url_without_storage, test_resume_prompts bad_lists[prompts5-new-1]). App: `npx vitest run <file>`, `npx vitest run`, `npx tsc --noEmit`.

## Review Focus

1. A bid whose job also has an applied resume and a tailored one → applied resume's fields win, gaps filled from the tailored one; the tailored resume never adds a bid (Task 1 `test_applied_resume_wins_tailored_fills_gaps`, `test_tailored_resume_not_counted`).
2. A profile that changed region / was archived → appears only under its current region / not at all, in tables and cards alike (Task 2 `test_scope_uses_current_region_and_drops_archived`).
3. Default "Since Oct 6" and a range starting Sep 1 → both start Oct 6; range Aug 1–Sep 30 → empty report, `startClamped` (Task 2 `test_clamp_to_oct6`).
4. Team scope never exposes bidder names (Task 3 `test_team_scope_has_no_bidder_table`; Task 4 UI test).
5. One profile scope narrows every card on the page (Task 4 `one profile narrows tables and cards`).

---

### Task 1: Sync — tailored resumes enrich bid rows

**Files:** Modify `app/applications/sync.py`, `app/applications/rows.py`, `app/models/application.py` (field `tailoredResumeId: Optional[str] = None`, add to the sync's field list so `$unset` clears it). Test `tests_unit/test_applications_rows.py`, `tests_unit/test_applications_sync.py`.

**Interfaces:** `build_rows(bids, resumes, accounts, tailored: Optional[list[dict]] = None)` — `tailored` = unapplied (`countsAsBid: false`) resumes not otherwise included as row sources; for each row with a `bid:` source, the earliest tailored resume with the same `(accountId, jobKey)` fills `score/scoreBand/promptId/promptName/templateName` and the job title (first line of its `jobDescription`) where the row has none from an applied resume, sets `tailoredResumeId`, and recomputes `area` from the final title. The sync passes unapplied resumes that are not interview-proven as `tailored`; `resume_row` maps each tailored resume id to the bid row's key so an interview linked to it matches that row.

- [ ] **Step 1: Failing tests** — `test_tailored_resume_enriches_bid_row` (score, band, title, area, tailoredResumeId); `test_tailored_resume_not_counted` (no extra row; `sources` unchanged; `byInterviewOnly` unaffected); `test_applied_resume_wins_tailored_fills_gaps`; `test_earliest_tailored_wins`; `test_interview_linked_to_tailored_resume_matches_bid_row`; sync: `test_full_rebuild_enriches_and_unsets_when_tailored_removed`.
- [ ] **Step 2–4:** FAIL → implement → PASS; full suite.
- [ ] **Step 5: Commit** `feat: bid rows carry their tailored resume's score, title and area`

---

### Task 2: Backend — conversion scope for the tab's endpoints

**Files:** Create `app/stats/conversion_scope.py`; Modify `app/routers/stats.py` (`/stats/applications/profiles`, `/changes`, `/timing` take `scope`, `profile`; use the helper), `app/stats/changes.py` (`volume_up_rate_down` off when the previous window starts before `DATA_START`). Tests `tests_unit/test_conversion_scope.py` + existing endpoint test files updated.

**Interfaces (produces):**
- `DATA_START: date`.
- `async def conversion_scope(user, period, date, to, region, user_id, scope: Literal["me","profile","team"] = "me", profile: Optional[str] = None) -> dict` → `{subject, start, end, label, today, now, startClamped, accounts: {id: {name, region, archived, ownerId}}, rows, teamRows}` where `rows` / `teamRows` follow the Global Constraints (loader rows for the clamped period, filtered by account ids). `scope="me"` default keeps today's callers working.
- Endpoints `/profiles`, `/changes`, `/timing` accept `scope` (default `me`) and `profile`; their team baselines use `teamRows`; under `profile` they report only that profile; under `team` `/profiles` and `/changes` return `profiles: []` and `/timing` returns team groups with `bidders: []`.

- [ ] **Step 1: Failing tests** — `test_clamp_to_oct6`; `test_scope_uses_current_region_and_drops_archived`; `test_profile_scope_other_users_profile_403`; `test_team_rows_are_the_baseline` (profiles' `team.per100` equals the team rate of `teamRows`); `test_volume_suggestion_off_before_data_start`; endpoint tests: `/profiles?scope=profile` returns one profile, `/timing?scope=team` has no bidders.
- [ ] **Step 2–4:** FAIL → implement → PASS; full suite.
- [ ] **Step 5: Commit** `feat: Conversion scope (me / profile / team, active profiles, from Oct 6) for the stats cards`

---

### Task 3: Backend — `GET /stats/conversion` and removal of jobs / leaders

**Files:** Create `app/stats/conversion.py`; Modify `app/routers/stats.py` (new route; delete `/stats/applications/jobs`, `/stats/applications/leaders`); Delete `app/stats/leaders.py`, `tests_unit/test_stats_leaders.py`, `tests_unit/test_stats_jobs_endpoint.py` (move any still-relevant grouping assertions into the conversion tests); keep `job_fit.py` helpers used by conversion/timing (drop its unused `breakdown` if nothing else uses it). Tests `tests_unit/test_stats_conversion.py`.

**Interfaces:**
- `conversion.report(rows, matured_before, bidder_names: dict, include_bidders: bool) -> dict` → `{totals: {bids, matured, interviews, interviewsAll, inPlay, rate}, byBidder? , bySite, byScore, byArea, byAge}`; group `{key, label, bids, matured, interviews, rate, few}`. `interviewsAll` = rows with an interview among all period rows; `inPlay` = rows not matured.
- `GET /stats/conversion` (same params as Task 2) → `{label, startClamped, window, scope, profile?, ...report}`.

- [ ] **Step 1: Failing tests** — totals math; `few` at 29 / 30 matured; score bands incl. `No tailored resume`; bidder table `Self` first then bids desc, `Unnamed bidder`; `test_team_scope_has_no_bidder_table`; `test_profile_scope_narrows`; `test_empty_before_data_start`.
- [ ] **Step 2–4:** FAIL → implement → PASS; full suite (no reference to the deleted routes/modules remains: grep).
- [ ] **Step 5: Commit** `feat: /stats/conversion breakdowns; drop jobs and leaders endpoints`

---

### Task 4: App — the Conversion tab

**Files:** Create `src/components/stats/ConversionTab.tsx`, `ConversionTable.tsx` (+ tests); Modify `src/pages/Statistics.tsx` (tabs: **Conversion**, Resume compare, AI usage — Bids and Interviews removed; old `?tab=bids|interviews` URLs open Conversion), `src/lib/dateRange.ts` + `statsFilters.tsx` (preset `since_oct6` = custom Oct 6 → today, labelled `Since Oct 6`, default on the Conversion tab only), `src/api/endpoints.ts` (`getConversion`, scope/profile params on `getProfileReport`, `getChanges`, `getTiming`; remove `getJobFit`, `getLeaders` + types), `ProfilesCard.tsx`, `ChangesCard.tsx`, `TimingSection.tsx` (accept `scope`, `profile`); Delete `JobFitCard.tsx`, `LeadersCard.tsx` (+ tests), `src/components/statistics/BidsTab.tsx`, `InterviewsTab.tsx` and FramedTab code/tests only they used.

Layout (spec "Page"): header strip (StatsFrame header + Everyone, `extras=false`) with the caption; filter row adds the scope switch `My profiles | Profile ▾ | Team total` (URL keys `scope`, `profile`; profile picker lists the subject's active profiles of the region as `name · region`); totals line `{label} · {bids} bids · {matured} matured · {interviews} interviews · {rate}% · {inPlay} in play`; five `ConversionTable`s in a 2-column grid (1 on phones) with columns `Group · Bids · Matured · Interviews · Rate`, `few` rates with `⚠` + tooltip (also shown on focus/tap); then Your profiles, What to change, When to bid (+ Bidder quality). Team total hides the Bidder table, Your profiles, What to change and Bidder quality. When `startClamped` and the report is empty: `No data before Oct 6, 2026 — pick a later period.`

- [ ] **Step 1: Failing tests** — `renders totals and five tables`; `few rate shows warning tooltip`; `team total hides bidder table and per-person cards`; `one profile narrows tables and cards` (every request carries `scope=profile&profile=…`); `Since Oct 6 is the default and sends custom 2026-10-06..today`; `old bids/interviews tab URLs open Conversion`; `empty clamped report message`.
- [ ] **Step 2–4:** FAIL → implement → PASS; full vitest + tsc; grep shows no import of deleted files.
- [ ] **Step 5: Commit** `feat: Statistics Conversion tab replaces Bids and Interviews`
