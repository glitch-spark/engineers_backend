# Statistics Insights — Slice 3 (Profiles) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new Profiles tab: which of the person's profiles turn bids into interviews, how that compares with the team, and which profiles to fix.

**Architecture:** Backend `app/stats/profiles.py` returns the shared contract with the Everyone list being the subject's **profiles** (`everyone.rowKind = "profile"`); `/stats/{tab}` gains `profiles`. App adds a Profiles tab on `FramedTab`; profile rows aren't clickable; a null trend hides the Trend card.

**Tech Stack:** FastAPI + Beanie (mongomock tests), React + SWR (vitest).

**Spec:** `engineers_backend/docs/superpowers/specs/2026-11-18-stats-insights-design.md` ("Profiles (new)", "The shared contract", "Decisions from the builder check").

**Repos / branches:** continue `feat/stats-insights` (backend 47d101d, app 5153884). Main checkouts.

## Global Constraints

- Contract as slices 1–2, plus `everyone.rowKind: "person" | "profile"` (bids/interviews send `"person"`) and `trend` may be `null`.
- Per-profile bids = self bids typed for that profile (`DailyPlan.done.bidsSelfByProfile`, every stored day) + Bid Track bids of bidders on that profile (`Bidder.profileId`, approved + pending, by Bid Track day). Per-profile interviews = rounds held on interviews with `accountId` = the profile (same held-round rules as the Interviews tab).
- Active profile = not archived and ≥ 1 bid in the period. Region filter: only profiles of that region (no-region profiles only under All).
- Per-100 for a profile only when it has ≥ 30 bids (`interview_rates.MIN_BIDS`); otherwise value null, "needs 30 bids".
- notEnoughData / today-excluded / admin `user` rules exactly as slices 1–2.
- Commits single line, no co-author / "Generated with" trailers. Never touch `.env*`. Never run against the real DB.
- Tests: backend `tests_unit/` (mongomock), full suite with the usual 3 `--ignore`s (2 known local failures); app `npx tsc --noEmit`, `npx vitest run`.

## Review Focus

1. A profile with bids but < 30: listed, per-100 "—", never "dead" (Task 1).
2. Archived profiles never appear as idle/dead; archived profiles with activity in the period still listed (Task 1).
3. A subject with no profiles: header renders, "No profiles yet" empty state, no crash (Tasks 1, 2).
4. Profile rows can't switch the viewed person (Task 2).
5. Region filter hides other regions' profiles and their interviews (Task 1).

---

### Task 1: Profiles view + rules + route (backend)

**Files:** Create `app/stats/profiles.py`; Modify `app/stats/bids.py` (move `idle_profiles` + `_profile_last_bids` into `app/stats/frame.py` or a shared `app/stats/profile_data.py`, imported by both), `app/routers/stats.py` (`VIEWS["profiles"]`, Literal adds `"profiles"`), `app/stats/bids.py` / `interviews.py` (add `"rowKind": "person"` to `everyone`). Test: `tests_unit/test_stats_profiles.py`, extend `tests_unit/test_stats_router.py`.

**Interfaces:**
- `async def profiles_view(subject, period, day, region, until, today) -> dict` (same signature as the other views).
- `async def profile_counts(uid, start, end, region) -> dict[str, dict]` → `{profileId: {"name", "region", "archived", "bids", "selfBids", "bidderBids", "interviews"}}` for the subject's profiles in the region (bids/interviews over [start, end]).
- Pure rules: `dead_profile(rows_28d: dict)` (≥ 150 bids and 0 interviews over the 28 days ending yesterday → bad, names up to 3); `region_gap(by_region: dict[str, (bids, interviews)])` (regions with ≥ 30 bids each; best per-100 ≥ 2× worst and best > 0 → watch: "EU profiles convert 2.4× US (4.8 vs 2.0 per 100)"); `idle_profiles` reused (watch).

Headline (in order): `best` ("Best profile", format "per100", value = highest per-100 among the subject's active profiles with ≥ 30 bids, note = that profile's name, team = distribution of each team member's best per-100, higherIsBetter true); `rank` (subject's rank by best per-100 among team members who have a ≥ 30-bid profile — same shape as other tabs); `zero` ("Profiles with 0 interviews", format "count", value = active profiles with ≥ 30 bids and 0 interviews, team = distribution of that count, higherIsBetter **false**); `active` ("Active profiles", format "count", team distribution of active-profile counts, note `"{n} in total"` incl. inactive non-archived).
Team set = non-admin users with ≥ 1 active profile in the period/region. Everyone: `rowKind: "profile"`, `metric: "per100"`, one row per subject profile with bids or interviews in the period (archived included when active), `userId` = profile id, `name` = profile name, `value` = per-100 or null, `rank` among the subject's ≥ 30-bid profiles, `vsMedian` = vs the subject's own profile median (null when < 2 ranked profiles), `parts: {self, bidder}` = bids, `isSubject` false. Trend: `null`. Definitions: 3 lines (per-profile bids; per-profile interviews; 30-bid threshold).
Tips: `dead_profile`, `region_gap`, `idle_profiles`, and `no_profiles` (bad: "No active profiles this period") when the subject has 0 active profiles and notEnoughData is false. No up_vs_last_period on this tab.

- [ ] **Step 1: Failing tests** — `test_profile_counts_self_bidder_interviews`; `test_under_30_bids_profile_value_none`; `test_dead_profile_fires_at_150_bids_zero_interviews` / `…_not_below_150`; `test_region_gap_needs_30_bids_each_and_2x`; `test_archived_not_idle_or_dead`; `test_region_filter_hides_other_regions`; `test_no_profiles_empty_and_tip`; `test_contract_keys_rowkind_profile_trend_null`; `test_zero_profile_rank_lower_is_better`; router `test_profiles_tab_served`; bids/interviews views include `rowKind: "person"`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new file + `test_stats_bids.py test_stats_interviews.py test_stats_frame.py test_stats_router.py` + full suite.
- [ ] **Step 5: Commit** `feat: statistics profiles view (per-profile conversion, dead / idle / region tips)`

---

### Task 2: Profiles tab (app)

**Files:** Create `src/components/statistics/ProfilesTab.tsx` (`<FramedTab tab="profiles" />`); Modify `src/pages/Statistics.tsx` (tab `{key: 'profiles', label: 'Profiles'}` after Interviews; `TAB_KEYS`), `src/api/endpoints.ts` (tab union adds `'profiles'`; `StatsView.trend: StatsTrend | null`; `everyone.rowKind: 'person' | 'profile'`), `src/components/stats/StatsFrame.tsx` (profile rows not clickable even for admins; show each row's region tag when present — backend adds `region` to profile rows; Trend card hidden when `trend` is null; empty Everyone for profiles reads "No profiles yet."; metric label for profiles "interviews per 100 bids by profile"). Tests: `StatsFrame.test.tsx`, `src/components/statistics/ProfilesTab.test.tsx`.

- [ ] **Step 1: Failing tests** — `profile rows are not clickable for admins`; `null trend hides the trend card`; `profiles empty state`; `profiles tab renders from /stats/profiles`; `tab order includes Profiles after Interviews`.
- [ ] **Step 2–4:** FAIL → implement → `npx tsc --noEmit && npx vitest run` PASS.
- [ ] **Step 5: Commit** `feat: Profiles tab — which profiles turn bids into interviews`
