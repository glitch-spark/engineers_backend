# Statistics Insights — Slice 4 (Callers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Callers tab gets the you-vs-team frame: a caller sees their pass rate vs other callers by round type; a non-caller sees how their own rounds pass vs the caller rounds on their interviews. The round list + PDF export stay below.

**Architecture:** Backend: extract round collection from `app/caller_stats.py` (output unchanged), build `app/stats/callers.py` with two modes (`caller` / `engineer`), serve via `/stats/{tab}`. App: Callers tab = `FramedTab tab="callers"` + the existing "Pass rate by round type" grid and "Rounds held" list/PDF below, with a caller picker replacing the old table click.

**Tech Stack:** FastAPI + Beanie (mongomock), React + SWR (vitest).

**Spec:** `engineers_backend/docs/superpowers/specs/2026-11-18-stats-insights-design.md` ("Callers", "The shared contract", "The app (frame)" — slice extras below the frame).

**Repos / branches:** continue `feat/stats-insights` (backend 5ff8a53, app 69599cb). Main checkouts.

## Global Constraints

- Contract as slices 1–3, plus top-level `mode: "caller" | "engineer"` on the callers view; `everyone.rowKind: "caller"` (rows never switch the viewed person).
- Round result & pass rate exactly as `caller_stats` (`round_result`; pass rate = passed ÷ (passed + rejected); ranked from `MIN_DECIDED` = 5 decided). Caller identity via `_caller_id` (callerId, else typed name, else Unassigned).
- Mode = `caller` when the subject (a platform user) is the caller on ≥ 1 held caller round in the period/region; else `engineer`.
- `/metrics/caller-stats` output must not change (the PDF and extras still use it).
- notEnoughData / today-excluded / admin `user` rules as slices 1–3.
- Commits single line, no co-author / "Generated with" trailers. Never touch `.env*`. Never run against the real DB.
- Tests: backend `tests_unit/` (mongomock), full suite with the usual 3 `--ignore`s (2 known local failures); app `npx tsc --noEmit`, `npx vitest run`.

## Review Focus

1. A user who is both a caller and an engineer: caller mode, and their own interviews don't leak into their caller numbers (Task 1).
2. Caller under 5 decided rounds: listed, rate shown, not ranked, no weak_round_type tip (Task 1).
3. Unassigned caller rounds count in the team but never as a "you" (Task 1).
4. `?caller=` deep link still preselects the rounds list caller (Task 2).
5. Region filter scopes rounds by the interview's profile region in both modes (Task 1).

---

### Task 1: Callers view + rules + route (backend)

**Files:** Modify `app/caller_stats.py` (extract `async def collect_rounds(start, end, region) -> tuple[list[dict], list[dict]]` returning (caller_rounds, self_rounds), every row carrying `result, group, day, engineerId`; caller rows also `callerId, callerName, interviewId, roundId, roundDate, company, stage`; `caller_stats` calls it, output unchanged); Create `app/stats/callers.py`; Modify `app/routers/stats.py` (`VIEWS["callers"]`, Literal adds `"callers"`). Tests: `tests_unit/test_stats_callers.py`, extend `tests_unit/test_stats_router.py`; existing `tests_unit/test_caller_stats*.py` must pass unchanged.

**Interfaces:**
- `async def callers_view(subject, period, day, region, until, today) -> dict` (same signature as the other views).
- Pure rules: `weak_round_type(mine: dict[group,(passed,rejected)], team: dict[group,(passed,rejected)])` (bad; group with ≥ 5 decided on both sides and subject rate < team rate − 0.15; names the group, e.g. "Tech rounds: 40% pass vs 62% for all callers"); `awaiting_backlog(rounds, today)` (watch; > 5 rounds with result `awaiting` held > 7 days before today: "7 rounds are still waiting for a result after a week — update them"); `caller_vs_self` reused from `app/stats/interviews.py` in engineer mode (watch); `no_caller_rounds` (engineer mode, bad-free info: watch "No caller rounds on your interviews this period" only when the subject has ≥ 5 decided self rounds).

**Caller mode** (subject is a caller): headline — `passRate` ("Your pass rate as a caller", format "pct", value = subject's caller pass rate, team = distribution over ranked callers (≥ 5 decided, Unassigned excluded), rank among them, note `"{passed} passed · {rejected} rejected"`); `rank`; `done` ("Rounds done", format "count", team = distribution of callers' done, note `"{awaiting} awaiting a result"`); `weakest` ("Weakest round type", format "pct", value = lowest group pass rate with ≥ 5 decided, note = group label, team none → `team: null`). Tips: weak_round_type, awaiting_backlog (subject's caller rounds), up_vs_last_period on rounds done (frame helper). Trend: rounds done per bucket, you vs median caller.
**Engineer mode**: headline — `selfRate` ("Pass rate when you take the round", format "pct", team = distribution over engineers with ≥ 5 decided self rounds, rank among them); `rank`; `callerRate` ("Caller rounds on your interviews", format "pct", note `"{n} rounds"`, team = distribution of each engineer's caller-round pass rate); `awaiting` ("Awaiting a result", format "count", higherIsBetter false). Tips: caller_vs_self, awaiting_backlog (rounds on the subject's interviews), no_caller_rounds. Trend: `null`.
**Both modes** — Everyone: `rowKind: "caller"`, `metric: "passRate"`, one row per caller with rounds in the period (Unassigned included, external callers included), `userId` = caller id (or `"unassigned"`), `value` = pass rate, `rank` (null when < 5 decided or Unassigned), `vsMedian` vs ranked callers' median, `parts: {passed, rejected}`, `isSubject` = caller id == subject id. Definitions: 3 lines (what a held caller round is; pass rate; ranking threshold).

- [ ] **Step 1: Failing tests** — `test_collect_rounds_unchanged_caller_stats` (caller_stats output identical before/after on a fixture); `test_mode_caller_when_subject_called`; `test_mode_engineer_otherwise`; `test_caller_and_engineer_uses_caller_numbers_only`; `test_under_5_decided_listed_not_ranked_no_weak_tip`; `test_weak_round_type_fires`; `test_awaiting_backlog_over_5_after_7_days`; `test_unassigned_in_team_never_subject`; `test_region_scopes_both_modes`; `test_engineer_mode_trend_null`; router `test_callers_tab_served`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** new file + existing caller_stats tests + `test_stats_*` + full suite.
- [ ] **Step 5: Commit** `feat: statistics callers view (caller and engineer modes) with round-type and backlog tips`

---

### Task 2: Callers tab (app)

**Files:** Modify `src/components/statistics/CallersTab.tsx` (render `<FramedTab tab="callers" />` then the extras: keep `TypeGrid` and `RoundsHeld` + PDF export fed by today's `/metrics/caller-stats` SWR; remove the old `StatTiles`, `CallersTable` and `CallerTrend`; replace the table-click selection with a "Caller" `Select` (All callers + each active caller) above Rounds held, initialised from `initialCaller` / `?caller=`), `src/api/endpoints.ts` (tab union adds `'callers'`; `StatsView.mode?: 'caller' | 'engineer'`; `rowKind` adds `'caller'`), `src/components/stats/StatsFrame.tsx` (`rowKind 'caller'` rows not clickable; METRIC_LABEL `passRate` → "pass rate"; pct formatting for Everyone when metric is `passRate`). Tests: `CallersTab` test (new or update), `StatsFrame.test.tsx`.

- [ ] **Step 1: Failing tests** — `callers tab renders the frame from /stats/callers`; `extras still render (round types grid, rounds held, export button)`; `caller select filters rounds and ?caller= preselects`; `caller rows are not clickable`; `everyone shows pass rate as %`.
- [ ] **Step 2–4:** FAIL → implement → `npx tsc --noEmit && npx vitest run` PASS.
- [ ] **Step 5: Commit** `feat: Callers tab shows you vs other callers with tips; round list kept below`
