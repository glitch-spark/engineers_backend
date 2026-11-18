# Statistics Insights — Slice 6 (Earnings + AI usage) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Earnings and AI usage tabs get the you-vs-team frame with tips; the 12-month net chart and the AI cost breakdown stay below as detail.

**Architecture:** Backend `app/stats/earnings.py` and `app/stats/ai_usage.py` return the shared contract; `/stats/{tab}` gains `earnings` and `ai-usage`. App: Earnings = `FramedTab tab="earnings"` + `NetIncomeCard` (viewed person) below; AI usage = `FramedTab tab="ai-usage"` (AI presets incl. Today/Yesterday, no region) + the existing breakdown card below with its Users/Features/Models pickers inside the card.

**Tech Stack:** FastAPI + Beanie (mongomock), React + SWR (vitest).

**Spec:** `engineers_backend/docs/superpowers/specs/2026-11-18-stats-insights-design.md` ("Earnings", "AI usage (visible to everyone)", "The shared contract"). Slice 5 (Resumes) is skipped for now at the user's request.

**Repos / branches:** continue `feat/stats-insights` (backend 7abeeae, app 9a89af2). Main checkouts.

## Global Constraints

- Contract as slices 1–4; new headline format `"usd"` (dollars; app shows `$1,234` for |v| ≥ 10, `$0.42` below).
- Earnings = approved `Transaction.amount` summed per `userId` with `date` in the period (same match as `dashboard.team_metrics`). Team = non-admin, active users with any approved transaction in the period.
- AI usage = `AiUsageEvent` rows with `createdAt` in the period's local days (app tz); cost = `costUsd` (None → 0, counted as unpriced). Team = non-admin users with cost > 0. AI usage stays visible to everyone; viewing another person is still admin-only (same `user` rule).
- notEnoughData / previous-window / admin `user` rules as slices 1–4. Region is ignored on both tabs (Earnings and AI usage have no region); the app hides Region there.
- Commits single line, no co-author / "Generated with" trailers. Never touch `.env*`. Never run against the real DB.
- Tests: backend `tests_unit/` (mongomock), full suite with the usual 3 `--ignore`s (2 known local failures); app `npx tsc --noEmit`, `npx vitest run`.

## Review Focus

1. Negative net (more expenses than income) renders and ranks correctly (Task 1).
2. A person with no transactions: would-rank, "No earnings recorded this period", no own-average tip (Task 1).
3. Unpriced AI calls (`costUsd` None) count as $0 and are disclosed (Task 2).
4. Today/Yesterday on AI usage map to single-day custom ranges for the frame (Task 3).
5. Admin viewing someone on Earnings: frame and the 12-month chart both show that person (Task 3).

---

### Task 1: Earnings view (backend)

**Files:** Create `app/stats/earnings.py`; Modify `app/routers/stats.py` (`VIEWS["earnings"]`, Literal adds it). Test `tests_unit/test_stats_earnings.py` (+ router test).

**Interfaces:** `async def earnings_view(subject, period, day, region, until, today) -> dict` (region ignored). Pure rules: `negative_period(net)` (bad: "You spent more than you earned this period ({net})"); `below_own_average(net, own_avg)` (watch when own_avg > 0 and net < 0.8 × own_avg: "Net is {x}% below your average of the last 3 periods ({avg})"); `above_own_average(net, own_avg)` (good when net ≥ 1.2 × own_avg and own_avg > 0); `no_earnings` (watch when the subject has no approved transactions and notEnoughData is false).

Headline: `net` ("Net this period", format "usd", team distribution of members' nets, rank, vsMedian, note `"income {i} · expenses {o}"`); `rank`; `ownAvg` ("Your 3-period average", format "usd", team null, note `"vs this period {±x%}"`). Own average = mean net of the 3 previous full periods of the same type (month → previous 3 calendar months; week → previous 3 weeks; custom → 3 equal-length windows before). Everyone: `rowKind "person"`, metric `"net"`, rows = team (+ subject if outside), value net, parts `{income, outcome}`. Trend: net per bucket, you vs team median. Definitions: 2 lines (approved transactions only; net = income − expenses).

- [ ] **Step 1: Failing tests** — `test_net_income_outcome_from_approved_only`; `test_negative_net_ranks_and_tips`; `test_no_transactions_would_rank_and_no_earnings_tip`; `test_own_average_three_previous_months`; `test_below_and_above_own_average`; `test_contract_keys`; router `test_earnings_tab_served`.
- [ ] **Step 2–4:** FAIL → implement → run file + `test_stats_*` + full suite.
- [ ] **Step 5: Commit** `feat: statistics earnings view (net vs team and own average)`

---

### Task 2: AI usage view (backend)

**Files:** Create `app/stats/ai_usage.py`; Modify `app/routers/stats.py` (`VIEWS["ai-usage"]`, Literal adds it). Test `tests_unit/test_stats_ai_usage.py` (+ router test).

**Interfaces:** `async def ai_usage_view(subject, period, day, region, until, today) -> dict` (region ignored). Per user over the period: `cost`, `calls`, `unpriced` (calls with costUsd None), `resumes` (distinct `refId` of ok `feature == "resume"` events), `resumeCost` (cost of `feature == "resume"` events), `byModel` (resume cost and resumes per model). `costPerResume = resumeCost / resumes` (None when 0). `costPerInterview = cost / interviews held` (interviews from `interview_rates.counts(..., logged_only=False)`; None when 0). Pure rule `expensive_model(my_cpr, team_median_cpr, my_top_model, team_top_models)` (watch when my_cpr ≥ 2 × team median and the team's most-used resume model differs from mine and is cheaper per resume: "Your resumes cost {a} each vs {b} for the team — most of the team uses {model}"); `no_ai_usage` none (no tip — zero cost is fine).

Headline: `cost` ("Your AI cost", format "usd", team distribution, rank with **higherIsBetter false**, note `"{calls} calls"` + `" · {n} unpriced"` when any); `rank`; `perResume` ("Cost per resume", format "usd", team distribution, higherIsBetter false, note `"{resumes} resumes"`); `perInterview` ("AI cost per interview", format "usd", team distribution, higherIsBetter false, note `"{interviews} interviews"`). Everyone: `rowKind "person"`, metric `"cost"`, rows = team (+ subject if outside) sorted by cost ascending (cheapest first = rank 1), parts `{resume: resumeCost, other: cost − resumeCost}`. Trend: cost per bucket, you vs team median. Definitions: 3 lines (what's counted; unpriced calls count as $0; per-resume / per-interview definitions).

- [ ] **Step 1: Failing tests** — `test_cost_and_unpriced`; `test_cost_per_resume_distinct_refids_ok_only`; `test_cost_per_interview_none_without_interviews`; `test_rank_cheapest_first`; `test_expensive_model_tip`; `test_contract_keys`; router `test_ai_usage_tab_served`.
- [ ] **Step 2–4:** FAIL → implement → run file + `test_stats_*` + full suite.
- [ ] **Step 5: Commit** `feat: statistics AI usage view (cost per resume / interview vs team)`

---

### Task 3: Earnings + AI usage tabs (app)

**Files:** Modify `src/components/statistics/EarningsTab.tsx` (→ `<FramedTab tab="earnings" />` then `NetIncomeCard` for the viewed person: `userId` = the `user` URL value for admins, else self; remove `CompareCard` and the old admin `<select>`), `src/components/statistics/AiUsageCard.tsx` (add `embedded` mode: no `StatsFilterBar`; Users/Features/Models multiselects rendered in the card's header row; range from `useStatsFilters('ai')`), `src/pages/Statistics.tsx` (AI usage tab = `<FramedTab tab="ai-usage" />` + `<AiUsageCard embedded />`), `src/components/stats/FramedTab.tsx` + `statsFilters.tsx` (tab union adds `'earnings' | 'ai-usage'`; for these tabs hide Region; for `ai-usage` use AI presets and map `today` / `yesterday` to a single-day custom range for the frame request), `src/components/stats/StatsFrame.tsx` (format `usd`; METRIC_LABEL `net` → "net", `cost` → "AI cost"), `src/api/endpoints.ts` (unions). Tests: `EarningsTab.test.tsx` (update), AI usage tab test, `StatsFrame.test.tsx`, `statsFilters.test.tsx`.

- [ ] **Step 1: Failing tests** — `earnings tab renders frame + net income chart for the viewed person`; `admin ?user= drives both`; `ai usage tab renders frame + embedded breakdown without a second filter bar`; `today maps to a single-day custom range`; `region hidden on earnings and ai usage`; `usd format`.
- [ ] **Step 2–4:** FAIL → implement → `npx tsc --noEmit && npx vitest run` PASS.
- [ ] **Step 5: Commit** `feat: Earnings and AI usage tabs show you vs team with tips`
