# Callers Statistics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Statistics → Callers shows each caller's interviews done and pass rate: compared across callers, by round
type, against self-taken rounds, and week by week. The pending and older-round lists go.

**Architecture:**
- `app/interview_rounds.py` gains `merged_rounds(iv)`, one round enumeration with entry ids. The Interviews tab and
  the Callers tab both count with it.
- `app/caller_stats.py` is rewritten on top of it. The response gains `team`, `self` and `weekly`, plus per-caller
  `done`, `ranked`, `vsTeam` and `byType`.
- The frontend's `CallersTab.tsx` and `lib/callerStatsPdf.ts` are rebuilt around those fields.

**Tech Stack:** FastAPI + Beanie (pytest + mongomock-motor); React 18 + SWR + Recharts + Tailwind; jsPDF.

**Spec:** `docs/superpowers/specs/2026-11-06-callers-statistics-design.md`

**Worktrees:** backend `.../scratchpad/run-backend`, frontend `.../scratchpad/run-app`, both on
`feat/statistics-cleanup`. `$PY` = `"/c/Users/Administrator/Downloads/engineer - Copy/engineers_backend/.venv/Scripts/python"`.

## Global Constraints

- **Interviews done** = passed + rejected + awaiting. Canceled and scheduled are counted but are not done.
- **Pass rate** = passed ÷ (passed + rejected), rounded to 4 places (`interview_rates.rate`), `null` at 0.
- **`MIN_DECIDED = 5`**: a caller is ranked when passed + rejected ≥ 5.
- **`vsTeam`** = caller pass rate − team caller pass rate, rounded to 4 places; `null` if either is `null`.
- **Round type** = `interview_rounds._breakdown_group(stage)`, one of `intro`, `tech`, `hiring`, `panel`, `final`.
- **Rounds not counted at all:** AI interviews, home assessments, and any stage with no group.
- **Round enumeration** = `merged_rounds`: consecutive entries of one stage are one round, and the latest non-empty
  status, date and caller win.
- **Removed:** the outdated and scheduled sections, `counts.outdated`, the caller cards and the Reject dialog.
  - The reject *endpoint* (`POST /interviews/{id}/stages/{id}/reject`) stays.
- Empty state copy: `No caller rounds in this period.` A `null` rate displays as `—`.
- No test or check writes to the live database.

## Review Focus

1. **A round whose status is still `scheduled` but whose date has passed.** It counts as scheduled, not done, and is
   not in `rounds` (Task 2 test: an Oct 25 scheduled round).
2. **A caller with exactly 5 decided rounds** is ranked; 4 is not (Task 2 test: Pietro at 5).
3. **An interview whose history repeats one stage** ("completed" then "passed") is one passed round (Task 1 and
   Task 2 tests).
4. **A period with caller rounds but zero decided rounds.** Rates are `null`, the frontend shows `—`, and nothing
   divides by zero (Task 2: the EU assertion, where `team.passRate is None`; Task 3: the frontend `pct` handles `null`).
5. **Rounds of removed callers or Unassigned** still appear as rows, as today (Task 2 keeps the existing
   `_caller_rows` ordering test).

---

### Task 1: One round enumeration with entry ids

**Files:**
- Modify: `app/interview_rounds.py` (add `merged_rounds`; `_dated_rounds` delegates to it)
- Test: `tests_unit/test_interview_rounds_merge.py` (create)

**Interfaces:**
- Produces:
  - `@dataclass class MergedRound: stage: str; at: Optional[datetime]; status: str; caller: Any; entry_id: Optional[str]`
  - `def merged_rounds(iv: Interview) -> list[MergedRound]`, in history order. `entry_id` is the id of the last
    history entry merged into the round, or `None` for a round that comes only from the interview-level tip.
  - `_dated_rounds(iv)` keeps its exact output: `[(r.stage, r.at, r.status, r.caller) for r in merged_rounds(iv)]`.

- [ ] **Step 1: Write the failing test**

```python
def test_consecutive_entries_of_one_stage_are_one_round_with_the_latest_id():
    iv = Interview(accountId=PydanticObjectId(), createdBy=PydanticObjectId(), scheduledAt=T2, companyName="Acme",
                   stageHistory=[{"id": "a", "stage": "intro", "at": T1, "scheduledAt": T1, "status": "completed"},
                                 {"id": "b", "stage": "intro", "at": T2, "scheduledAt": T2, "status": "passed"},
                                 {"id": "c", "stage": "tech_round_1", "at": T3, "scheduledAt": T3, "status": "scheduled"}])
    rounds = merged_rounds(iv)
    assert [(r.stage, r.status, r.entry_id) for r in rounds] == [("intro", "passed", "b"), ("tech_round_1", "scheduled", "c")]
    assert rounds[0].at == T2
    assert _dated_rounds(iv) == [(r.stage, r.at, r.status, r.caller) for r in rounds]
```

(`T1`, `T2`, `T3` are naive UTC datetimes on Oct 3, 4 and 10, 2026. Build the `Interview` without inserting it; no DB
fixture is needed.)

- [ ] **Step 2: Run it and see it fail**

Run: `$PY -m pytest tests_unit/test_interview_rounds_merge.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'merged_rounds'`.

- [ ] **Step 3: Implement `merged_rounds` and make `_dated_rounds` delegate to it**

- [ ] **Step 4: Run it and the round-counting tests**

Run: `$PY -m pytest tests_unit/test_interview_rounds_merge.py tests_unit/test_interview_rates.py tests_unit/test_profile_metrics.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/interview_rounds.py tests_unit/test_interview_rounds_merge.py
git commit -m "refactor(interviews): merged_rounds, one round enumeration with entry ids"
```

### Task 2: Caller statistics: done, pass rate, by type, self, weekly

**Files:**
- Modify: `app/caller_stats.py` (rewrite `caller_stats`, `_caller_rows`; drop `placed`; keep `round_result`,
  `typed_caller`, `name_key`, `ALIASES`)
- Modify: `app/routers/metrics.py:744-757` (docstring only)
- Test: `tests_unit/test_caller_stats.py`

**Interfaces:**
- Consumes: Task 1's `merged_rounds`; `interview_rounds._breakdown_group`, `_ROUND_COUNT_EXCLUDED`;
  `interview_rates.window/label/rate/unit/buckets/_profile_regions`.
- Produces: the response from spec section 1.
  - `callers[i]` = `{callerId, name, color, counts: {passed, rejected, awaiting, canceled, scheduled}, done, passRate, ranked, vsTeam, byType}`
  - `byType[group]` = `{done, passed, rejected, passRate}` for each of the five groups, always present.
  - `team` and `self` = `{counts, done, passRate, byType}`.
  - `weekly` = `{unit, buckets: [{label, start, end, all: {done, passRate}, callers: {<callerId or "unassigned">: {done, passRate}}}]}`.
  - `rounds` = rounds held in the period whose result is passed, rejected, awaiting or canceled, with today's fields
    except `section`, `daysWaiting` and `stale`.
  - `MIN_DECIDED = 5` as a module constant.

- [ ] **Step 1: Rewrite the main test and add the new ones (failing)**

Replace `test_each_callers_interviews_by_the_period_they_were_held_in` with tests built on one scenario, `_october()`:
- Ana, US profile; Muhammad (`mu`); Pietro (`pi`).
- `mu`:
  - intro passed Oct 3;
  - tech_round_1 rejected Oct 6;
  - intro completed Oct 20 (awaiting);
  - intro canceled Oct 8;
  - intro scheduled Oct 25 (date passed, still scheduled);
  - home_assessment passed Oct 9 (not counted);
  - intro passed Sep 15 (another period).
- `pi`:
  - intro passed Oct 2, 5 and 13, and final rejected Oct 14;
  - one interview with intro completed then intro passed on Oct 12 (one round). That makes pi's decided rounds 5:
    4 passed, 1 rejected.
- Self rounds (`enabled=False`): intro passed Oct 10, live_coding rejected Oct 11.

```python
async def test_callers_done_pass_rate_and_ranking(db):
    mu, pi = await _october()
    out = await cs.caller_stats("month", date(2026, 10, 1), "all", now=NOW)
    rows = {c["name"]: c for c in out["callers"]}
    assert rows["Muhammad"]["counts"] == {"passed": 1, "rejected": 1, "awaiting": 1, "canceled": 1, "scheduled": 1}
    assert (rows["Muhammad"]["done"], rows["Muhammad"]["passRate"], rows["Muhammad"]["ranked"]) == (3, 0.5, False)
    assert (rows["Pietro"]["done"], rows["Pietro"]["passRate"], rows["Pietro"]["ranked"]) == (5, 0.8, True)
    assert out["team"]["passRate"] == 0.7143 and out["team"]["done"] == 8
    assert (rows["Pietro"]["vsTeam"], rows["Muhammad"]["vsTeam"]) == (0.0857, -0.2143)
    assert "outdated" not in rows["Muhammad"]["counts"]


async def test_pass_rate_by_round_type_and_self(db):
    await _october()
    out = await cs.caller_stats("month", date(2026, 10, 1), "all", now=NOW)
    mu = next(c for c in out["callers"] if c["name"] == "Muhammad")
    assert mu["byType"]["tech"] == {"done": 1, "passed": 0, "rejected": 1, "passRate": 0.0}
    assert mu["byType"]["final"] == {"done": 0, "passed": 0, "rejected": 0, "passRate": None}
    assert (out["self"]["done"], out["self"]["passRate"]) == (2, 0.5)
    assert out["self"]["byType"]["tech"]["passRate"] == 0.0 and out["self"]["byType"]["intro"]["passRate"] == 1.0


async def test_weekly_buckets_add_up(db):
    mu, pi = await _october()
    out = await cs.caller_stats("month", date(2026, 10, 1), "all", now=NOW)
    assert out["weekly"]["unit"] == "week"
    assert sum(b["callers"].get(str(mu.id), {"done": 0})["done"] for b in out["weekly"]["buckets"]) == 3
    assert sum(b["all"]["done"] for b in out["weekly"]["buckets"]) == out["team"]["done"]


async def test_rounds_list_only_rounds_held_in_the_period(db):
    mu, _ = await _october()
    out = await cs.caller_stats("month", date(2026, 10, 1), "all", now=NOW)
    mine = [r for r in out["rounds"] if r["callerId"] == str(mu.id)]
    assert sorted(r["result"] for r in mine) == ["awaiting", "canceled", "passed", "rejected"]
    assert all("section" not in r and "stale" not in r for r in out["rounds"])
```

Keep:
- `test_a_typed_caller_name_counts_for_that_caller` (assert on `done` instead of section counts where it used them);
- `test_every_signed_in_user_sees_the_report`;
- `test_mark_rejected_closes_an_awaiting_round`;
- `test_typed_caller_names_link_by_name_and_alias`.

Add a region assertion: under `EU` with no EU rounds, every caller's `done` is 0 and `team.passRate is None`.

- [ ] **Step 2: Run them and see them fail**

Run: `$PY -m pytest tests_unit/test_caller_stats.py -q -p no:cacheprovider`
Expected: the new tests FAIL (`KeyError: 'done'`, or `outdated` still present).

- [ ] **Step 3: Implement**

- One interview query for rounds dated in the window: `stageHistory.scheduledAt` or `scheduledAt` in
  [first, last] of the app-timezone days, for every owner.
- Walk `merged_rounds`. Skip rounds with no group or an excluded stage, and rounds whose held day falls outside the
  window.
- Bucket each round by caller (an enabled caller) or into self (otherwise), by result, by group and by weekly bucket.

- [ ] **Step 4: Run the file and the neighbours**

Run: `$PY -m pytest tests_unit/test_caller_stats.py tests_unit/test_callers.py tests_unit/test_interview_rates.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/caller_stats.py app/routers/metrics.py tests_unit/test_caller_stats.py
git commit -m "feat(statistics): callers by interviews done and pass rate, by round type, vs self, weekly"
```

### Task 3: Callers tab and PDF

**Files:**
- Modify: `src/api/endpoints.ts` (`CallerStatsCaller`, `CallerStatsRound`, `CallerStats`; add `CallerStatsGroup`, `CallerStatsTotals`, `CallerStatsWeekly`)
- Modify: `src/components/statistics/CallersTab.tsx` (rewrite the view; keep the fetch, the FilterBar and the PDF button)
- Modify: `src/lib/callerStatsPdf.ts`

**Interfaces:**
- Consumes: Task 2's response; `StatTiles` and `pct` from `rateShared.tsx`.
- Produces, in `CallersTab.tsx`: `CallersTable`, `TypeGrid`, `CallerTrend` and the existing `RoundsTable`.
  - The picked caller key is `'all' | callerId | 'unassigned'`.
- PDF: `CallerStatsExport` becomes `{ name, isAll, periodLabel, regionLabel, callers, team, self, rows: CallerStatsRound[], callerName }`.

- [ ] **Step 1: Types**, as spec section 1.

- [ ] **Step 2: The view**, in spec section 2's order:
  1. Tiles: `Interviews done`, `Caller pass rate`, `Self pass rate`, `Awaiting result`.
  2. Callers table: an All callers row, then callers with `done + counts.canceled > 0`.
     - Columns: Caller, Interviews done, Pass rate (with a bar scaled to the best ranked rate), vs team
       (`+8.6 pts` / `−21.4 pts`), Passed, Rejected, Awaiting, Canceled.
     - Default sort: ranked first, then pass rate descending. Unranked rates are faded, with the title
       `Under 5 decided rounds`.
     - The row is a button that picks the caller.
  3. `TypeGrid`: rows are callers with done > 0, then All callers, then Self.
     - Columns: Intro · Tech · Hiring · Panel · Final.
     - Each cell reads `60% · 10`, or `—` when done is 0.
     - The cell background is emerald at an opacity of `passRate × 0.35`; the text always shows.
  4. `CallerTrend`: a Recharts `ComposedChart` for the picked caller (or `all`).
     - `done` as bars on the left axis; `passRate × 100` as a line on a right axis from 0 to 100;
     - a dashed `ReferenceLine` at `team.passRate × 100` on the right axis;
     - title: `{name} · interviews done and pass rate by {unit}`.
  5. `Rounds held · {name} · {period}`: the existing table without the reject column, plus the sort and the PDF button.
  - Delete `CallerCard`, `RejectDialog`, the outdated and scheduled sections, and their imports.
  - Empty state: when `team.done + team.counts.canceled === 0`, show the tiles and `No caller rounds in this period.` instead of sections 2–4.

- [ ] **Step 3: PDF**
  - Summary table columns: Caller · Interviews done · Pass rate · vs team · Passed · Rejected · Awaiting · Canceled.
    It shows all callers plus a team row, or only the picked caller.
  - Then a "Pass rate by round type" table with the same rows as `TypeGrid`.
  - Then "Rounds held" with the existing badges.
  - Delete the outdated and scheduled sections.

- [ ] **Step 4: Verify**

Run: `npm run lint && npm run build` in `run-app`.
Expected: no type errors, and the build succeeds.
Run: `grep -rn "outdated\|RejectDialog\|CallerCard" src/components/statistics src/lib/callerStatsPdf.ts`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add src/api/endpoints.ts src/components/statistics/CallersTab.tsx src/lib/callerStatsPdf.ts
git commit -m "feat(statistics): Callers tab by interviews done and pass rate"
```

### Task 4: Verification against live data (read-only)

- [ ] **Step 1: Full backend unit suite.** Same command as the Statistics plan's Task 5. Expected: only the 2 known
  `.env` failures.
- [ ] **Step 2: Live comparison.** A scratchpad script with `init_beanie(..., skip_indexes=True)` and no writes, for
  October 2026, region `all`.
  - Expected:
    - `team.done + team.counts.scheduled` equals the Interviews tab's `totals.callerInterviews`, plus the rounds on
      interviews whose owner is an admin;
    - the same for `self` against `totals.selfInterviews`.
  - Print both sides and the admin-owned count, and explain any other difference.
- [ ] **Step 3: PDF smoke test.** Build a PDF from a sample `CallerStatsExport` with the Node harness approach used
  for the interview brief (esbuild bundle, `jsPDF.API.save` patched to write a file). Open it and check the three
  sections render.
