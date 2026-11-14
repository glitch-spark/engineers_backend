# Profile Metrics Tab UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Profile → Metrics tab into a verdict first (Needs attention), four cards, a Rounds table with self /
caller, a "How it compares" table against the owner's other profiles and every owner's profiles, two trend charts and
the day table on demand.

**Architecture:** The existing `GET /accounts/{id}/metrics` gains round self / caller and round-type counts, plus a
`compare` block (this profile and the owner's other profiles, measured over the compared window by the spec's fair
rules). A new `GET /accounts/metrics/benchmark` computes every owner's profiles by the same rules, behind an
in-process stale-while-revalidate cache. The browser fetches both and works out the Needs attention lines.

**Tech Stack:** FastAPI + Beanie/Motor (mongomock-motor tests, pytest-asyncio auto mode); React 18 + TypeScript +
SWR + Tailwind + Recharts on Vite; Node 24's built-in test runner for one pure TS module.

**Spec:** `docs/superpowers/specs/2026-11-13-profile-metrics-ux-design.md` (engineers_backend checkout `run-backend`).
Frontend checkout `run-app`. Both on branch `feat/profile-metrics-ux`.

## Global Constraints

- Paths: backend `C:/Users/ADMINI~1/AppData/Local/Temp/2/claude/409b0cc9221f9860/d4b3fc45-0895-4b25-8af0-e23ddd50123e/scratchpad/run-backend`,
  frontend `.../scratchpad/run-app`. Python: `/c/Users/Administrator/Downloads/engineer - Copy/engineers_backend/.venv/Scripts/python`.
- Backend tests: `$PY -m pytest tests_unit -q -p no:cacheprovider --continue-on-collection-errors` (3 collection
  errors and 2 `.env`-only failures — `test_bidder_ext` upload 503, `test_resume_prompts` bad lists — predate this
  work). Frontend: `npm run lint && npm run build`, plus `npm test` once Task 5 adds it.
- The local `.env` points at the live DB: live checks are read-only, never `init_db()`; run the local backend with
  `SLACK_BOT_TOKEN=`.
- **Compared window** = `[max(range start, launch day), range end]`, launch day = `pm.since_date()` (live 2026-11-05;
  None → no clamp). Empty (launch day after range end) → no comparison at all. Tests move the launch day by patching
  `pm.get_settings` with `PROFILE_METRICS_SINCE` (conftest sets `0001-01-01`, i.e. no clamp).
- **A profile in a window**: days `[max(window start, its creation day), window end]`; absent when created after the
  window; weeks = ceil(days / 7). Averages ("Your other profiles", "All profiles") keep only profiles with ≥ 1 bid in
  the window; the "This profile" column keeps it even with 0 bids.
- **CompareStats** (backend dict, frontend type), exactly these keys: `profiles` (int), `bids` (summed window bids),
  `decided` (summed passed + rejected rounds, home assessments out), `bidsPerWeek`,
  `interviewsPerWeek`, `tailoredResumesPerWeek` (per-profile means, 1 decimal), `interviewsPerBid` (pooled, 4
  decimals, None with 0 bids), `passRate` (pooled decided rounds, home assessments out, `interview_rates.rate`),
  `tailorScore`, `matchScore` (`{avg: 1 decimal | None, n}` over every scored resume pooled).
- Existing response fields stay as they are (an older frontend keeps working); `peers` is left untouched — the fair
  "Your other profiles" values live in the new `compare.peers`. *(Plan call: the spec said `peers` changes; a new key
  keeps the deployed frontend correct while Railway and Vercel deploy at different times.)*
- Cards' rate figures (Interviews `% pass`, Interviews / bid, Tailor score) come from `compare.this`, so a card and its
  comparison line measure the same days; counts (bids, self / bidder, interviews, self / caller, tailored) and the
  Rounds table use the full range. *(Plan call extending the spec's clamped Interviews / bid to the other two rates.)*
- Copy (exact): range note `Compared from {Mon D}, when self bids started being recorded per profile`; low volume
  `Fewer than 30 bids — too few to rank`; comparison line `Yours {x} · All {y}`; attention strings as in Task 5.
- Never show owner or profile names / ids from the benchmark; it returns only CompareStats plus period fields.

## Review Focus

1. Range entirely before the launch day (e.g. Custom Oct 1–20): no comparison, no attention lines, cards still show
   totals, no crash — pinned in Task 3 and Task 5.
2. Owner with a single active profile: `compare.peers` null, the "Your other profiles" column and "Yours" parts
   disappear, attention falls back to "All" only — pinned in Task 3 and Task 5.
3. Benchmark refresh failing in the background (DB hiccup): the stale value keeps being served, the error is logged,
   the next request retries — pinned in Task 4.
4. Profile created inside the window (or a peer created after it): per-week isn't diluted, a peer created after the
   window isn't averaged in as zeros — pinned in Task 2.
5. Older backend behind the new frontend (Railway deploy waits for approval): no `compare`, no round types in
   daily, no self / caller → the tab renders today's equivalents without errors — pinned in Task 6's verify step.

---

### Task 1: Rounds by who took them, and round types per day (backend)

**Files:**
- Modify: `app/profile_metrics.py` (`_COUNTS`, `interview_rounds`, `profile_rounds`, `rounds_summary`)
- Test: `tests_unit/test_profile_rounds.py` (new tests; update the exact-dict assertions for the two new keys)

**Interfaces:**
- Produces: `profile_rounds(...)` records `{"type", "result", "caller": bool, "day": date}` (`caller` = has an
  enabled caller, the `split_rounds_by_day` rule; `day` = local day held). `rounds_summary` → `byType[t]` gains
  `self`, `caller` (done rounds by who took them; 0 / 0 for `home_assessment`). `_COUNTS` gains
  `"intro", "tech", "hiring", "panel", "final"`, so every `daily` / `weekly` entry carries them and they add up to
  `selfInterviews + callerInterviews`.

- [ ] **Step 1: Write the failing tests** in `tests_unit/test_profile_rounds.py`:

```python
async def test_rounds_split_self_and_caller_per_type(db):
    # helper: _iv_caller(owner, acc, entries, caller_enabled) — like _iv, each history entry gets
    # caller={"name": "Cal", "enabled": caller_enabled}; check how Interview stores a caller and build that shape.
    ...  # intro passed by a caller, intro rejected self, intro scheduled by a caller (not done), home_assessment done
    s = await _summary(a)
    assert (s["byType"]["intro"]["self"], s["byType"]["intro"]["caller"]) == (1, 1)
    assert (s["byType"]["home_assessment"]["self"], s["byType"]["home_assessment"]["caller"]) == (0, 0)

async def test_daily_round_types_add_up_to_interviews(db, today):
    # intro Nov 3 + tech_round_1 Nov 3 + final Nov 4 + home_assessment Nov 4 on profile a
    out = await get_profile_metrics(str(a.id), ana, period="month", date="2026-11-13", to="")
    nov3 = next(d for d in out["daily"] if d["day"] == "2026-11-03")
    assert (nov3["intro"], nov3["tech"], nov3["final"]) == (1, 1, 0)
    for d in out["daily"] + out["weekly"]:
        assert sum(d[k] for k in ("intro", "tech", "hiring", "panel", "final")) == d["selfInterviews"] + d["callerInterviews"]
```

- [ ] **Step 2: Run, expect FAIL** — `$PY -m pytest tests_unit/test_profile_rounds.py -q -p no:cacheprovider`
  Expected: the two new tests fail with `KeyError: 'self'` / `KeyError: 'intro'`.
- [ ] **Step 3: Implement.** `interview_rounds` returns 10 slots (the 5 existing, then intro…final by
  `split_rounds_by_day`'s group); `_days` maps `_COUNTS[2:]` onto them as today. `profile_rounds` adds `caller` and
  `day`. Update the existing exact-dict assertions in this file to include `self` / `caller`.
- [ ] **Step 4: Run, expect PASS** — the same command, then the full backend suite (Global Constraints).
  Expected: all tests in the file pass; suite shows only the known pre-existing failures.
- [ ] **Step 5: Commit** — `feat(profile metrics): rounds by who took them and round types per day`

### Task 2: Window stats and pooling (backend, pure)

**Files:**
- Create: `app/profile_compare.py`
- Test: `tests_unit/test_profile_compare.py`

**Interfaces:**
- Consumes: Task 1 round records; `_days` daily dicts (`_COUNTS` keys); `_score_lists` dicts.
- Produces:
  - `async def compared_window(start: date, end: date) -> Optional[tuple[date, date]]` — Global Constraints rule.
  - `def window_stats(created: date, window: tuple[date, date], daily: dict[date, dict], score_lists: dict, rounds: list[dict]) -> Optional[dict]`
    → `{"weeks", "bids", "interviews", "tailored", "tailorScores": list[int], "matchScores": list[int], "passed", "rejected"}`
    over the profile's days in the window (rounds filtered by `day`, home assessments out of passed / rejected);
    None when created after the window. `score_lists` is already cut to the window by the caller.
  - `def pool(stats: list[dict], *, drop_idle: bool = True) -> Optional[dict]` → CompareStats; None when nothing
    is left after dropping idle (bids 0) profiles.
  - `COMPARE_KEYS: tuple[str, ...]` — the CompareStats keys, in Global Constraints order (tests assert on it).

- [ ] **Step 1: Write the failing tests** (no DB needed except `compared_window`):

```python
def test_week_count_starts_at_creation():          # window Nov 1–28, created Nov 15 → weeks 2, not 4
def test_created_after_window_is_absent():          # created Dec 1 → None
def test_idle_profiles_are_dropped_from_averages(): # A 40 bids/2 wk, B 0 bids → profiles 1, bidsPerWeek 20.0
def test_rates_and_scores_are_pooled():
    # A: 3 bids 1 interview; B: 97 bids 1 interview → interviewsPerBid 0.02 (not mean 0.1718)
    # A tailor [90], B tailor [70, 70, 70] → tailorScore {"avg": 75.0, "n": 4}
    # A passed 1 rejected 0, B passed 0 rejected 3 → passRate 0.25
def test_this_profile_kept_with_no_bids():          # pool([idle], drop_idle=False)["bidsPerWeek"] == 0.0, interviewsPerBid None
def test_pool_of_nothing_is_none():                 # pool([]) is None; pool([idle]) is None
async def test_compared_window_clamps_to_launch(monkeypatch):
    # PROFILE_METRICS_SINCE "2026-11-05": (Oct 1, Nov 13) → (Nov 5, Nov 13); (Oct 1, Oct 20) → None; (Nov 6, Nov 9) unchanged
```

- [ ] **Step 2: Run, expect FAIL** — `$PY -m pytest tests_unit/test_profile_compare.py -q -p no:cacheprovider`
  Expected: `ModuleNotFoundError: app.profile_compare`.
- [ ] **Step 3: Implement** `app/profile_compare.py`. Read the launch day as `await pm.since_date()` through the
  module (`from app import profile_metrics as pm`) so tests can patch it.
- [ ] **Step 4: Run, expect PASS** — same command.
- [ ] **Step 5: Commit** — `feat(profile metrics): fair window stats and pooled comparisons`

### Task 3: `compare` on the profile endpoint (backend)

**Files:**
- Modify: `app/routers/accounts.py` (`get_profile_metrics`), `app/profile_metrics.py` (expose the loader: rename
  `_load` → `load_sources` and `_days` → `days_from`, updating `collect` / `collect_range`)
- Test: `tests_unit/test_profile_compare.py` (endpoint tests)

**Interfaces:**
- Consumes: Task 2 (`compared_window`, `window_stats`, `pool`); Task 1 records.
- Produces, on `GET /accounts/{id}/metrics`: `comparedFrom: str | None` (ISO) and
  `compare: {"this": CompareStats | None, "peers": CompareStats | None}`. Sources load **once** over the full range
  (`load_sources`), then `days_from(key, *window, sources)` per profile gives the windowed score lists;
  `profile_rounds` is called once over the full range and filtered by `day` (this also removes the old double
  interview load when `collect_range` is replaced by `load_sources` + `days_from` here).

- [ ] **Step 1: Write the failing tests**:

```python
async def test_compare_starts_at_launch_day(db, today, monkeypatch):
    # launch Nov 5. Profile a: Oct 20 interview (intro passed) + Nov 6 interview; 40 bidder bids Nov 6–8.
    out = await get_profile_metrics(str(a.id), ana, period="all", date="", to="")
    assert out["comparedFrom"] == "2026-11-05"
    assert out["compare"]["this"]["interviewsPerBid"] == 0.025      # 1 / 40: the Oct 20 round isn't counted
    assert out["interviews"] == 2                                     # totals keep the full range
async def test_compare_peers_leave_out_idle_and_unborn_profiles(db, today):
    # b: 30 bids; c: 0 bids; d created Dec 1 (after the window) → compare.peers.profiles == 1
async def test_single_profile_owner_has_no_peers(db, today):          # compare.peers is None, compare.this present
async def test_range_before_launch_has_no_comparison(db, today, monkeypatch):
    # custom Oct 1–20 with launch Nov 5 → comparedFrom None, compare == {"this": None, "peers": None}, bids still totalled
```
  Bids: reuse the `Bid` / `Bidder` helpers from `tests_unit/test_profile_bidder_breakdown.py` (bid days follow
  `bid_report_day`: put `firstAt` at 15:00 UTC).

- [ ] **Step 2: Run, expect FAIL** — `$PY -m pytest tests_unit/test_profile_compare.py -q -p no:cacheprovider`
  Expected: `KeyError: 'comparedFrom'`.
- [ ] **Step 3: Implement** in `get_profile_metrics`; keep every existing field (incl. `peers`) as it is today.
- [ ] **Step 4: Run, expect PASS**, then the full suite (the renames touch `collect` / `collect_range` callers).
- [ ] **Step 5: Commit** — `feat(profile metrics): compare this profile and the owner's others over the fair window`

### Task 4: Every-owner benchmark endpoint with stale-while-revalidate cache (backend)

**Files:**
- Modify: `app/profile_compare.py` (`benchmark`, `BenchmarkCache`), `app/routers/accounts.py` (route; extract the
  period resolution shared with `get_profile_metrics`)
- Test: `tests_unit/test_profile_benchmark.py`

**Interfaces:**
- Produces:
  - `async def benchmark(window: tuple[date, date]) -> dict` — CompareStats of every owner's active profiles
    (`archive_status_query("active")`, all `createdBy`), `pool(..., drop_idle=True)`; with nobody left, CompareStats
    with `profiles: 0` and every value None (`{avg: None, n: 0}` for scores).
  - `class BenchmarkCache(compute, *, fresh_s=900, keep_s=86400, clock=time.monotonic)` with
    `async def get(self, window) -> dict`: fresh → cached; stale (≤ keep) → cached now + one background refresh
    (`asyncio.create_task`, errors logged, entry kept); missing / older than keep → compute under a per-key
    `asyncio.Lock` (concurrent misses compute once). Module instance `benchmark_cache = BenchmarkCache(benchmark)`.
  - `GET /accounts/metrics/benchmark?period=week|month|custom|all&date=&to=` (declared right after `GET /metrics`):
    any signed-in user; `{start, end, label, comparedFrom, ...CompareStats}`; `period=all` → launch day … today
    (label "All time"); no compared window → `comparedFrom: None` and the empty CompareStats; bad period → 422.

- [ ] **Step 1: Write the failing tests**:

```python
async def test_benchmark_pools_every_owner(db, today):        # owners Ana (a: 40 bids, 2 iv) and Bo (b: 60 bids, 1 iv, c idle)
    # → profiles 2, interviewsPerBid 0.03, response keys == {"start","end","label","comparedFrom", *COMPARE_KEYS}
async def test_benchmark_open_to_any_signed_in_user(db, today): # Cy owns no profile → 200, same numbers
async def test_benchmark_bad_period_is_422(db, today):
async def test_benchmark_all_time_starts_at_launch(db, today, monkeypatch):  # start == comparedFrom == "2026-11-05"
async def test_cache_serves_fresh_entry_without_recomputing():  # fake clock: t=0 compute, t=100 → same object, 1 call
async def test_cache_serves_stale_then_refreshes():             # t=1000 → old value returned, refresh task ran → 2 calls, next get new value
async def test_cache_keeps_stale_value_when_refresh_fails():    # refresh raises → old value still served, next stale get retries
async def test_concurrent_misses_compute_once():                # asyncio.gather(get, get) → 1 call
```

- [ ] **Step 2: Run, expect FAIL** — `$PY -m pytest tests_unit/test_profile_benchmark.py -q -p no:cacheprovider`
  Expected: ImportError on `benchmark` / `BenchmarkCache`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat(profile metrics): every-owner benchmark with a 15-minute stale-while-revalidate cache`

### Task 5: Types and Needs attention rules (frontend, pure)

**Files:**
- Modify: `src/api/endpoints.ts`, `package.json` (`"test": "node --test tests/"`)
- Create: `src/lib/profileAttention.ts` (only `import type` imports, no enums — Node strips the types),
  `tests/profileAttention.test.mjs` (imports `../src/lib/profileAttention.ts`)

**Interfaces:**
- Consumes: Tasks 1, 3, 4 response shapes.
- Produces (`endpoints.ts`): `CompareStats`; `RoundTypeStats` gains optional `self?`, `caller?`; `MetricCounts` gains
  optional `intro? tech? hiring? panel? final?`; `ProfileMetricDetail` gains optional `comparedFrom?: string | null`,
  `compare?: { this: CompareStats | null; peers: CompareStats | null }`;
  `getMetricsBenchmark(params: { period; date?; to? }) => Promise<{ start; end; label; comparedFrom: string | null } & CompareStats>`.
- Produces (`profileAttention.ts`):
  `attentionLines(rounds: RoundsSummary | undefined, compare: {this; peers} | undefined, all: CompareStats | null): { dir: 'up' | 'down'; text: string }[]`.
  Rules (spec "Needs attention"), comparison = `all` value when non-null, else `peers` value; wording suffix
  `across all profiles` / `on your other profiles`. Whole percents for pass rates (`71%`), one decimal for
  interviews per bid (`1.8%`), whole numbers for scores and bids / week (one decimal when < 10):
  1. `{Type} rounds pass {x}% ({Best} {y}%)` — weakest type with ≥ 3 decided, ≥ 15 points under `rounds.passRate`;
     best = highest other type with ≥ 3 decided (parenthetical omitted when none). Down only.
  2. `this.bids` ≥ 30 and `this.interviewsPerBid` ≤ 0.75 × comparison → down / ≥ 1.25 × → up:
     `{x}% interviews per bid vs {y}% {suffix}`.
  3. `this.decided` ≥ 5 and pass rates ≥ 15 points apart: `{x}% of rounds passed vs {y}% {suffix}`.
  4. `this.tailorScore.n` ≥ 5, ≥ 5 points apart: `Tailor score {x}, {below|above} all profiles ({y})` (or
     `your other profiles`).
  5. peers only, `this.bidsPerWeek ≤ 0.5 × peers.bidsPerWeek`: `{x} bids / week vs {y} on your other profiles`. Down only.
  Downs first, then ups, each in rule order; at most 3; `[]` when `compare.this` is null (rule 1 still applies).

- [ ] **Step 1: Write the failing tests** (`node:test` + `node:assert/strict`): each rule just inside and just outside
  its threshold; low volume (29 bids) suppresses rule 2; four triggers → 3 lines, downs first; `all = null` →
  "on your other profiles" wording; `peers = null` and `all = null` → only rule 1; `compare` undefined → only rule 1.
- [ ] **Step 2: Run, expect FAIL** — `npm test` → `Cannot find module .../profileAttention.ts`.
- [ ] **Step 3: Implement**; update `endpoints.ts`.
- [ ] **Step 4: Run, expect PASS** — `npm test && npm run lint`.
- [ ] **Step 5: Commit** (run-app) — `feat(profile metrics): types and Needs attention rules`

### Task 6: The tab (frontend)

**Files:**
- Modify: `src/components/profiles/ProfileMetricsPanel.tsx` (data, layout, charts)
- Create: `src/components/profiles/ProfileMetricsParts.tsx` (`MetricCard`, `AttentionList`, `RoundsTable`,
  `CompareTable` — move `RoundsCard` / `Tile` / `PeerLine` here and reshape them)

**Interfaces:**
- Consumes: Task 5 types, `attentionLines`, `getMetricsBenchmark`; SWR key `['metrics-benchmark', period, date, to]`.

- [ ] **Step 1: Data.** Fetch the benchmark alongside the profile (same params); its failure only hides "All" values.
  On `period === 'all'` the view opens on Week unless the user picks Day in this visit (don't write that default to
  storage).
- [ ] **Step 2: Layout**, in spec order:
  - range label, then the `Compared from …` note when `comparedFrom` is after `start`;
  - `AttentionList` (hidden when empty; ▼ `TrendingDown`, ▲ `TrendingUp`, `role="list"`);
  - 4 cards (`grid-cols-1 min-[480px]:grid-cols-2 lg:grid-cols-4`) per the spec table and Global Constraints
    (Bids cue vs Yours only; "All" part shows a `skeleton` while the benchmark loads);
  - `RoundsTable`: Round · Done · Self · Caller · Passed · Rejected · Pass rate (Self / Caller `–` for home
    assessment and when absent from an older server);
  - `CompareTable`: rows Bids / week, Interviews / week, Interviews / bid, Pass rate, Tailor score, Tailored resumes /
    week, Match score; columns This profile · Your other profiles (omitted when `compare.peers` null) · All profiles;
    hidden when `compare` is absent or `compare.this` null.
- [ ] **Step 3: Charts.** Bids: stacked self / bidder bars + Interviews / bid `Line` on a right `YAxis`
  (`tickFormatter` percent) in week view only, null for weeks with 0 bids. Interviews: stacked bars intro…final with
  five distinct hues for light and dark (picked from the existing chart palette); when the daily entries lack `intro`
  (older server) keep today's Total / Self / Caller lines.
- [ ] **Step 4: Day table** inside `<details>` with `<summary>` "Day-by-day numbers" / "Week-by-week numbers"; add
  an Interviews / bid column (blank with 0 bids).
- [ ] **Step 5: Verify.** `npm test && npm run lint && npm run build` → exit 0. Older-backend check: a scratch edit
  that strips `compare`, `comparedFrom`, round `self` / `caller` and daily round types from `m` before rendering
  still type-checks and the code paths read only optional fields (`?.` / fallbacks) — then revert the scratch edit.
- [ ] **Step 6: Commit** (run-app) — `feat(profile metrics): verdict, cards, compare table and trend charts`

### Task 7: Live read-only check

A scratchpad script (not committed) connects with `init_beanie(..., skip_indexes=True)` — never `init_db()` — and
calls the functions directly; everything it calls only reads.

- [ ] **Step 1:** `benchmark_cache.get((date(2026, 11, 5), date(2026, 11, 13)))` twice. Expected: first ≤ ~20 s,
  second < 0.1 s, same object, keys exactly `COMPARE_KEYS`, `profiles` ≤ 57.
- [ ] **Step 2:** `get_profile_metrics` for one profile with bids and rounds, period month Nov 2026, as its owner.
  Compare `compare.this` `bids`, `decided` and `passRate` with a direct pymongo count over Nov 5–13. Expected: equal.
- [ ] **Step 3:** Restart the local backend on this branch (stop the old background task first; `SLACK_BOT_TOKEN=`,
  port 8080) and open that profile's Metrics tab at `http://localhost:3001`: four cards, attention lines consistent
  with the compare table, the "All profiles" column fills in, no console errors.
- [ ] **Step 4:** No commit. Note results in the ledger.
