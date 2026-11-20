# Statistics Insights — Slice 2 (Interviews) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Interviews tab with the shared you-vs-team frame: interviews per 100 bids vs team, rank, how far interviews get, rule-based tips, everyone ranked, trend.

**Architecture:** Backend: `app/stats/interviews.py` builds the same contract as `app/stats/bids.py` (slice 1), from `interview_rates.counts(..., logged_only=False)` for bids/interviews and a new per-round result helper for pass rates; `/stats/{tab}` serves both tabs. App: the Bids tab's wiring becomes a generic framed tab; `StatsFrame`'s header stops assuming Bids' keys.

**Tech Stack:** FastAPI + Beanie (mongomock tests), React + SWR + Recharts (vitest).

**Spec:** `engineers_backend/docs/superpowers/specs/2026-11-18-stats-insights-design.md` ("The shared contract", "Interviews", "Decisions from the builder check"). Slice 1 plan for context: `docs/superpowers/plans/2026-11-18-stats-insights-slice1.md`.

**Repos / branches:** continue on `feat/stats-insights` in both repos (slice 1 heads: backend 7193c4f, app 2ddb1d3). Main checkouts.

## Global Constraints

- Same contract keys as slice 1 (`tab, label, subject, headline[], tips[], everyone{metric,rows[]}, trend{unit,buckets[]}, notEnoughData, definitions[]`); `tab = "interviews"`.
- Bids per person = slice 1's definition (every stored day + Bid Track), so "per 100 bids" uses the same bids the Bids tab shows.
- Interviews = rounds held (`interview_rounds.held_rounds` rules: canceled, AI interviews, home assessments excluded), in the profile's region under a region filter.
- Round result: `caller_stats.round_result(status)` → passed / rejected / awaiting / canceled / scheduled. Pass rate = passed ÷ (passed + rejected).
- Rate team distribution and ranking only over people with ≥ `interview_rates.MIN_BIDS` (30) bids; others listed, not ranked ("would rank" style, like slice 1).
- notEnoughData / today-excluded / previous-window / admin `user` rules exactly as slice 1.
- Commits single line, no co-author / "Generated with" trailers. Never touch `.env*`. Never run against the real DB (backend `.env` is production; dev server uses `DB_FAST_START=1`).
- Tests: backend `tests_unit/` (mongomock); full suite with the usual 3 `--ignore`s (2 known local failures). App: `npx tsc --noEmit`, `npx vitest run`.

## Review Focus

1. A person with interviews but < 30 bids: shown, not ranked, no `low_conversion` tip (Task 1).
2. Zero bids in the team (rate undefined) → no crash, rate "—" (Tasks 1, 3).
3. Stage pass rates with too few decided rounds (< 4) never produce a tip (Task 1).
4. Region filter: interviews and bids both region-scoped (Task 1).
5. Switching tabs keeps the admin's Viewing choice and both tabs honor it (Task 3).

---

### Task 1: Interviews view + rules (backend)

**Files:** Modify `app/interview_rounds.py` (add `held_round_results`); Create `app/stats/interviews.py`; Test `tests_unit/test_stats_interviews.py`.

**Interfaces:**
- Produces `held_round_results(iv, start, end, tz) -> list[tuple[date, str, bool, str]]` → (local day, group, is_caller, result) for the same rounds `held_rounds` keeps, result via `caller_stats.round_result(round_status)`.
- Produces `async def interviews_view(subject, period, day, region, until, today) -> dict` (same signature as `bids_view`).
- Produces pure rules (each → tip dict with `impact`, or None): `low_conversion(my_rate, team_median_rate, my_bids)`, `stalls_at_stage(mine: dict[group, (passed, rejected)], team: dict[group, (passed, rejected)])`, `caller_vs_self(caller: (passed, rejected), self_: (passed, rejected))`, `up_vs_last_period(now, before, window)` (reuse slice 1's wording/helper — import from `app.stats.bids` if it's generic, else move it to `frame.py` and import from both).

Headline (in order): `per100` ("Interviews per 100 bids", value = interviews ÷ bids × 100 rounded to 1 decimal, `format: "per100"`, team = distribution of per-100 over people with ≥ 30 bids, `higherIsBetter` true, note `"{interviews} interviews from {bids} bids"`); `rank` (same shape as slice 1's rank entry, ranked by per-100 among ≥ 30-bid people); `interviews` ("Interviews held", `format: "count"`, team = distribution of interview counts over the team, note `"Self {s} · caller {c}"`); `reach` ("Reached tech", `format: "pct"`, value = share of the subject's interviews (processes with a held round in the period) that reached a tech-or-later group, team = distribution of that share, note `"final {x}%"`).
Team set = non-admin users with interviews > 0 or bids ≥ 30 in the period/region. Everyone: metric `per100`, rows sorted by per-100 desc (people under 30 bids last, rank null), `parts: {self, caller}` (interviews). Trend: interviews held per bucket, you vs team median. Definitions: 3 lines (what counts as an interview; per 100 bids uses the Bids tab's bids; pass rate = passed ÷ (passed + rejected)).

Thresholds (constants): `LOW_CONVERSION = 0.6` (bad, only when the subject has ≥ 30 bids); `STALL_RATIO = 0.6` with `MIN_DECIDED = 4` per group for both subject and team (watch, names the group: "Tech rounds: you pass 20% vs team 45%"); `CALLER_GAP = 0.15` with ≥ 4 decided on each side (watch: "Caller-taken rounds pass 62% vs 40% when you take them — consider a caller for tech rounds"); up_vs_last_period as slice 1 (good) on interviews held; `no_interviews` bad tip when the subject has 0 interviews and notEnoughData is false.

- [ ] **Step 1: Failing tests** — rules: positive + negative each (`test_low_conversion_needs_30_bids`, `test_stall_needs_four_decided`, `test_stall_names_group`, `test_caller_vs_self_gap`, `test_no_interviews_tip`). View: `test_contract_keys_and_headline_order`; `test_under_30_bids_listed_not_ranked`; `test_zero_bids_rate_is_none_no_crash`; `test_region_scopes_interviews_and_bids`; `test_reach_counts_processes_reaching_tech`; `test_held_round_results_matches_held_rounds` (same rounds, results mapped).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** Reuse `frame.py`; if `bids.py` and `interviews.py` would copy the same team/rows/trend assembly, extract it into `frame.py` now (two tabs + Profiles next).
- [ ] **Step 4: Run** the file + `tests_unit/test_stats_bids.py tests_unit/test_stats_frame.py` + full suite.
- [ ] **Step 5: Commit** `feat: statistics interviews view with conversion, stage and caller tips`

---

### Task 2: `/stats/{tab}` route (backend)

**Files:** Modify `app/routers/stats.py`; Test `tests_unit/test_stats_router.py`.

**Interfaces:** Replace `GET /stats/bids` with `GET /stats/{tab}` where `tab: Literal["bids", "interviews"]` → `bids_view` / `interviews_view` (same params, same auth). Unknown tab → 422 (FastAPI literal validation).

- [ ] **Step 1: Failing tests** — `test_interviews_tab_served`; `test_unknown_tab_422`; existing bids/auth tests keep passing via the new path.
- [ ] **Step 2–4:** FAIL → implement → run file + full suite.
- [ ] **Step 5: Commit** `feat: GET /stats/{tab} serves bids and interviews`

---

### Task 3: Generic framed tab + Interviews tab (app)

**Files:** Modify `src/components/stats/StatsFrame.tsx` (header generic), `src/components/statistics/BidsTab.tsx` → extract `src/components/stats/FramedTab.tsx`, `src/components/statistics/InterviewsTab.tsx` (replace body), `src/components/statistics/statsFilters.tsx` (Viewing picker on any framed tab), `src/api/endpoints.ts` (`getStatsView` tab union `'bids' | 'interviews'`; `format` union adds `'per100' | 'pct'`). Tests: `StatsFrame.test.tsx`, `src/components/stats/FramedTab.test.tsx` (move/adapt BidsTab tests), `InterviewsTab.test.tsx` (new).

**Interfaces:** `FramedTab({ tab }: { tab: 'bids' | 'interviews' })` = today's BidsTab body (filters + Viewing picker for admins + SWR `['stats', tab, …]` + loading/error/invalid + "Viewing {name} · {label}" line + `StatsFrame`). `BidsTab` = `<FramedTab tab="bids" />`, `InterviewsTab` = `<FramedTab tab="interviews" />`. Header: first entry = main cell (+ RangeBar), then "Team median" cell from the main entry's team, then the `rank` entry if present, then every remaining entry as its own cell (no key-specific code). Formats: `count` → whole number; `per100` → one decimal + muted "per 100 bids"; `pct` → `Math.round(v*100)%`; `rank` as today; null value → "—". The old Interviews scatter, per-user bars and ranking table are removed (Everyone replaces the ranking); `rateShared.tsx` keeps only what other tabs still import.

- [ ] **Step 1: Failing tests** — `header renders any extra entries generically`; `per100 and pct formats`; `null value shows —`; `interviews tab renders the frame from /stats/interviews`; `viewing choice carries from Bids to Interviews (same user param)`.
- [ ] **Step 2–4:** FAIL → implement → `npx tsc --noEmit && npx vitest run` PASS.
- [ ] **Step 5: Commit** `feat: Interviews tab shows you vs team with tips; shared framed tab`
