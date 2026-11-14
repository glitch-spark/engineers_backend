# Profile rounds card · single list Intv/bid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The profile Metrics tab shows interviews by round type with pass rates (home assessments included as their own
row); the Profiles list shows one Intv/bid rate instead of separate self and bidder rates.

**Architecture:** `app/profile_metrics.py` gains `profile_rounds` (one interview query → round records per profile) and
`rounds_summary` (the Callers rules per round type). `GET /accounts/{id}/metrics` adds `rounds`, `passRate` and
`peers.passRate`. The list endpoint drops `selfRate` / `bidderRate` / `ratesSince`. The frontend adds a Rounds card and
turns the two list rate columns into one.

**Tech Stack:** FastAPI + Beanie, pytest + mongomock-motor; React 18 + SWR + Tailwind (`npm run lint`, `npm run build`).

**Spec:** `docs/superpowers/specs/2026-11-13-profile-rounds-design.md`

Repos: backend `run-backend`, frontend `run-app` (scratchpad worktrees), both on `feat/profiles-applied-by`.
Python: `"/c/Users/Administrator/Downloads/engineer - Copy/engineers_backend/.venv/Scripts/python"` (`$PY`).

## Global Constraints

- Round types, in display order: `intro`, `tech`, `hiring`, `panel`, `final`, then `home_assessment`. Labels: Intro,
  Tech, Hiring, Panel, Final, Home assessment.
- Groups come from `app.interview_rounds._breakdown_group`; home assessment is stage `home_assessment`; AI interviews
  never count.
- Rounds from `merged_rounds(iv)`, dated by the app's local day of `rd.at`, inside [start, end] of the tab's period.
- Result per round: `app.caller_stats.round_result(status)`.
- done = passed + rejected + awaiting; pass rate = passed ÷ (passed + rejected), 4 dp, `None` with no decided rounds
  (use `app.interview_rates.rate(passed + rejected, passed)`).
- Overall `passRate` excludes home assessments. Peers' rate is pooled over the owner's other active profiles' rounds.
- Card footnote copy: `N held with no result yet · N still scheduled`. Each half is left out at 0; the line is left
  out when both are 0. "Still scheduled" replaces the spec's "upcoming": a past round nobody updated is also
  `scheduled`, so "upcoming" would be wrong for it.
- List: one column "Intv/bid" = row `interviewsPerBid`, faded when `lowVolume`; sort key `rate`.
- Push / PR only to the glitch-spark origin; no merge without the user's request.

## Review Focus

1. Same-stage history entries (an intro rescheduled twice) must count as one round, not three. Pinned in Task 1.
2. A home assessment with no `scheduledAt` (only the interview's `scheduledAt`) must still be dated through
   `merged_rounds`' fallback, not dropped or crash. Pinned in Task 1.
3. Older backend + newer frontend: the Metrics tab without `rounds` must render the tiles without the card, not
   crash; the list guard must still detect an old list response once `ratesSince` is gone. Pinned in Tasks 3 and 4.
4. A profile with rounds but zero decided ones: every pass rate shows `—`, never `0%` or `NaN%`. Pinned in Tasks 1
   and 3.
5. A round on the period's edge day in Chicago time (e.g. 23:30 local on the last day) counts in the period. Pinned
   in Task 1.

---

### Task 1: Rounds by type on the profile Metrics endpoint

**Files:**
- Modify: `app/profile_metrics.py` (add `ROUND_TYPES`, `profile_rounds`, `rounds_summary`)
- Modify: `app/routers/accounts.py` (`get_profile_metrics`, around line 461)
- Test: `tests_unit/test_profile_rounds.py` (new; copy the env header, `pytestmark`, `db` fixture and
  `_owner_profile` from `tests_unit/test_profile_metrics_applied.py`)

**Interfaces:**
- Produces: `ROUND_TYPES = ("intro", "tech", "hiring", "panel", "final", "home_assessment")`.
- Produces: `async def profile_rounds(account_ids: list[PydanticObjectId], start: date, end: date) -> dict[str, list[dict]]`.
  - Keyed by account id string; each record is `{"type": <ROUND_TYPES member>, "result": <round_result>}`.
  - Uses the same interview query as `interview_rounds`.
- Produces: `def rounds_summary(rounds: list[dict]) -> dict`, returning
  `{"byType": {t: {"done", "passed", "rejected", "passRate"} for t in ROUND_TYPES}, "awaiting": int, "scheduled": int, "passRate": float | None}`.
  - `awaiting` and `scheduled` count every type, home assessments included.
  - `passRate` excludes `home_assessment`.
- Produces in the response: `row["rounds"] = rounds_summary(mine)`, `row["passRate"] = row["rounds"]["passRate"]`,
  `row["peers"]["passRate"]` = `rounds_summary(<all other profiles' records concatenated>)["passRate"]`. When
  `peers` is `None` (no other profiles), leave it `None`.

- [ ] **Step 1: Write the failing tests** in `tests_unit/test_profile_rounds.py`. Helper
  `_iv(owner, acc, entries)` inserts an Interview whose `stageHistory` is
  `[{"stage": s, "at": w, "scheduledAt": w, "status": st} for s, w, st in entries]`, with
  `scheduledAt=entries[-1][1]`.

```python
async def test_rounds_by_type_with_pass_rates(db):
    ana, a = await _owner_profile()
    await _iv(ana, a, [("intro", at(11, 3), "passed"), ("tech_round_1", at(11, 5), "rejected")])
    await _iv(ana, a, [("intro", at(11, 4), "rejected")])
    await _iv(ana, a, [("intro", at(11, 6), "completed"), ("live_coding", at(11, 7), "passed")])
    await _iv(ana, a, [("cultural", at(11, 8), "scheduled")])
    await _iv(ana, a, [("final", at(11, 9), "canceled")])
    await _iv(ana, a, [("ai_interview", at(11, 9), "passed")])
    await _iv(ana, a, [("home_assessment", at(11, 10), "passed"), ("intro", at(10, 1), "passed")])  # Oct 1: outside
    s = pm.rounds_summary((await pm.profile_rounds([a.id], date(2026, 11, 1), date(2026, 11, 13)))[str(a.id)])
    assert s["byType"]["intro"] == {"done": 3, "passed": 1, "rejected": 1, "passRate": 0.5}
    assert s["byType"]["tech"] == {"done": 2, "passed": 1, "rejected": 1, "passRate": 0.5}
    assert s["byType"]["hiring"] == {"done": 0, "passed": 0, "rejected": 0, "passRate": None}
    assert s["byType"]["final"]["done"] == 0 and s["byType"]["panel"]["passRate"] is None
    assert s["byType"]["home_assessment"] == {"done": 1, "passed": 1, "rejected": 0, "passRate": 1.0}
    assert (s["awaiting"], s["scheduled"]) == (1, 1)
    assert s["passRate"] == 0.5          # 2 passed / 4 decided, home assessment left out


async def test_same_stage_entries_are_one_round(db):
    ana, a = await _owner_profile()
    await _iv(ana, a, [("intro", at(11, 3), "scheduled"), ("intro", at(11, 4), "scheduled"), ("intro", at(11, 5), "passed")])
    s = pm.rounds_summary((await pm.profile_rounds([a.id], date(2026, 11, 1), date(2026, 11, 13)))[str(a.id)])
    assert s["byType"]["intro"] == {"done": 1, "passed": 1, "rejected": 0, "passRate": 1.0}


async def test_home_assessment_dated_by_interview_when_entry_has_no_date(db):
    ana, a = await _owner_profile()
    await Interview(accountId=a.id, createdBy=ana.id, companyName="Acme", scheduledAt=at(11, 6),
                    stage="home_assessment", status="completed").insert()
    s = pm.rounds_summary((await pm.profile_rounds([a.id], date(2026, 11, 1), date(2026, 11, 13)))[str(a.id)])
    assert s["byType"]["home_assessment"]["done"] == 1


async def test_edge_day_in_local_time(db, monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "America/Chicago")  # use the tz setting app_tz() reads; check the name
    ana, a = await _owner_profile()
    await _iv(ana, a, [("intro", datetime(2026, 11, 14, 5, 30), "passed")])  # Nov 13 23:30 Chicago
    s = pm.rounds_summary((await pm.profile_rounds([a.id], date(2026, 11, 1), date(2026, 11, 13)))[str(a.id)])
    assert s["byType"]["intro"]["done"] == 1


async def test_profile_metrics_endpoint_returns_rounds_and_peer_rate(db, today):
    ana, a = await _owner_profile()
    b = await Account(name="B", region="US", createdBy=ana.id, createdAt=datetime(2026, 1, 5, 15)).insert()
    await _iv(ana, a, [("intro", at(11, 3), "passed")])
    await _iv(ana, b, [("intro", at(11, 3), "passed"), ("tech_round_1", at(11, 4), "rejected"),
                       ("final", at(11, 5), "rejected")])
    from app.routers.accounts import get_profile_metrics
    out = await get_profile_metrics(str(a.id), ana, period="month", date="2026-11-13", to="")
    assert out["rounds"]["byType"]["intro"]["passRate"] == 1.0
    assert out["passRate"] == 1.0
    assert out["peers"]["passRate"] == 0.3333
```

  - Copy the `today` fixture from the applied test file.
  - If `app_tz()` reads a settings field rather than an env var, patch that instead. The point is a fixed
    Chicago zone.
  - If mongomock's `Interview` rejects the tip-only interview's fields, use the model's real field names
    (`stage`, `status`, `scheduledAt`).

- [ ] **Step 2: Run them; they fail.**
  Run: `$PY -m pytest tests_unit/test_profile_rounds.py -q -p no:cacheprovider`
  Expected: FAIL, `AttributeError: module 'app.profile_metrics' has no attribute 'profile_rounds'` (and `rounds`
  KeyError for the endpoint test).

- [ ] **Step 3: Implement** `ROUND_TYPES`, `profile_rounds` and `rounds_summary` in `app/profile_metrics.py`.
  - The type is `"home_assessment"` when `rd.stage == "home_assessment"`, else `_breakdown_group(rd.stage)`.
  - Skip records with no type or no `rd.at`.
  - Then wire them into `get_profile_metrics`: one `profile_rounds([a.id for a in accounts], start, end)` call after
    `collect_range`.

- [ ] **Step 4: Run them; they pass,** then the related suites.
  Run: `$PY -m pytest tests_unit/test_profile_rounds.py tests_unit/test_profile_metrics.py tests_unit/test_profile_metrics_router.py tests_unit/test_profile_metrics_applied.py -q -p no:cacheprovider`
  Expected: all pass.

- [ ] **Step 5: Commit** `feat(profile metrics): interviews by round type with pass rates, home assessments included`

### Task 2: One interviews-per-bid rate on the Profiles list (backend)

**Files:**
- Modify: `app/routers/accounts.py` (`list_profile_metrics`, lines ~405–455; the import on lines 16–17)
- Modify: `app/profile_metrics.py` (remove `APPLIED_BY_SINCE`, `_source_rate`, `source_rates`)
- Modify: `tests_unit/test_profile_metrics_applied.py`, `tests_unit/test_profile_metrics_router.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: the list response `{"start", "end", "weeks", "rows"}`. Rows keep `interviewsPerBid`, `lowVolume`,
  the applied-by interview counts, `firstApply` and `lastApply`. Rows lose `selfRate` / `bidderRate`; the response
  loses `ratesSince`.

- [ ] **Step 1: Change the tests first.**
  - `test_list_shape`: `set(out) == {"start", "end", "weeks", "rows"}`.
  - In `test_profile_metrics_applied.py`:
    - drop `test_list_range_before_nov_6_has_no_rates` and `test_list_all_time_rates_since_nov_6`;
    - rename `test_list_date_range_and_rates_from_nov_6` to `test_list_date_range`, keeping its range and count
      assertions;
    - replace its rate assertions with `row["interviewsPerBid"] == <its interviews ÷ bids over the whole range>`
      and `"selfRate" not in row and "ratesSince" not in out`.
- [ ] **Step 2: Run them; they fail.** Run: `$PY -m pytest tests_unit/test_profile_metrics_applied.py tests_unit/test_profile_metrics_router.py -q -p no:cacheprovider`
  Expected: FAIL on the shape (`ratesSince` still present) and on `selfRate` in the row.
- [ ] **Step 3: Remove the second `collect_range`, `source_rates` and `ratesSince`** from `list_profile_metrics`, along
  with the now-unused names in `profile_metrics.py`, and update its docstring.
- [ ] **Step 4: Run the whole backend suite.** Run: `$PY -m pytest tests_unit -q -p no:cacheprovider > "$WS/suite.txt" 2>&1; tail -3 "$WS/suite.txt"`
  Expected: everything passes except the 2 known `.env`-only failures.
- [ ] **Step 5: Commit** `feat(profiles): one interviews-per-bid rate on the list, no self/bidder split`

### Task 3: Rounds card on the Metrics tab (frontend)

**Files:**
- Modify: `src/api/endpoints.ts` (the profile-metrics response type used by `ProfileMetricsPanel`)
- Modify: `src/components/profiles/ProfileMetricsPanel.tsx`

**Interfaces:**
- Consumes (Task 1): `rounds?: RoundsSummary`, `passRate?: number | null`, `peers.passRate?: number | null`.
- Produces:
  - `export type RoundTypeStats = { done: number; passed: number; rejected: number; passRate: number | null }`;
  - `export type RoundsSummary = { byType: Record<'intro'|'tech'|'hiring'|'panel'|'final'|'home_assessment', RoundTypeStats>; awaiting: number; scheduled: number; passRate: number | null }`.
  - All three new fields are optional, so an older backend still type-checks.

- [ ] **Step 1: Types** in `endpoints.ts` as above.
- [ ] **Step 2: Interviews tile.**
  - The `split(...)` line becomes `` `${fmtPct(m.passRate)} pass overall` ``, or `No decided rounds` when it's null.
  - Add `<PeerLine mine={m.passRate} peer={peers?.passRate} fmt={fmtPct} />` beside the existing count PeerLine.
  - The tile's `title` shows `N self · N caller`.
  - Use the page's existing percent formatter if there is one; otherwise add
    `fmtPct = (r) => r == null ? '—' : \`${Math.round(r * 100)}%\`` in `lib/profileMetrics.ts`.
  - Without `m.passRate` in the response (older backend), keep today's self/caller line.
- [ ] **Step 3: Rounds card** after the tiles grid, rendered only when `m.rounds`.
  - A `<section className="card">` titled "Rounds" with a `<table>`.
  - One row per type in Global Constraints order, with a top border on the Home assessment row.
  - Columns Round · Done · Passed · Rejected · Pass rate:
    - Pass rate is `fmtPct` plus a 64px bar (`bg-surface-muted` track, fill width = rate × 100%) when not null.
    - Passed and Rejected show `–` when done is 0.
  - Footnote copy from Global Constraints.
- [ ] **Step 4: Verify.** Run in run-app: `npm run lint && npm run build`
  Expected: both exit 0.
  - In the browser (local backend on 8080, frontend on 3001), open a profile with interviews → Metrics.
  - The card shows six rows, and the overall pass rate matches the rows (decided only, without home assessment).
- [ ] **Step 5: Commit** (run-app) `feat(profile metrics): Rounds card with pass rate per round type`

### Task 4: One Intv/bid column on the Profiles list (frontend)

**Files:**
- Modify: `src/pages/Accounts.tsx`, `src/api/endpoints.ts` (`ProfileMetricsRow`, `getProfileMetrics` response)

**Interfaces:**
- Consumes (Task 2): rows without `selfRate` / `bidderRate`; response without `ratesSince`.

- [ ] **Step 1: Types.** Remove `selfRate`, `bidderRate`, `SourceRate` and `ratesSince` from `endpoints.ts`.
- [ ] **Step 2: Page.**
  - `SortKey` swaps `selfRate | bidderRate` for `rate` (`m.interviewsPerBid`).
  - Remove `rateCell` and `ratesSinceLabel`.
  - Add one Intv/bid `<td>`: `fmtRate(m.interviewsPerBid)`, `text-muted` and the title
    `` `${fmtCount(m.interviews)} interviews ÷ ${fmtCount(m.bids)} bids · fewer than 30 bids, too few to rank` ``
    when `lowVolume`, `—` when null.
  - `METRIC_COLS` 5, header list `['rate', 'Intv/bid']`, `colSpan={10}` (both places).
  - The old-server guard becomes `metricsData.rows.length > 0 && !('lastApply' in metricsData.rows[0])`. An old
    backend's rows lack `lastApply`, and an empty list needs no guard.
- [ ] **Step 3: Verify.** Run: `npm run lint && npm run build`
  Expected: both exit 0.
  - In the browser: the Profiles list has one Intv/bid column, sorts by it, and All time shows numbers, not dashes.
- [ ] **Step 4: Commit** (run-app) `feat(profiles): one Intv/bid column on the list`
