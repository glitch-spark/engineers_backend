# Conversion redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Real bids / interviews with rank at the top of Statistics → Conversion, a ranked Everyone list, key findings, a coverage line and rate bars with ★ / ⚠ in the breakdowns.

**Architecture:** Backend adds `summary` (cards, everyone, coverage) and `findings` + group `mark` to `GET /stats/conversion`; the app rebuilds `ConversionTab` around them.

**Tech Stack:** FastAPI + Beanie (mongomock tests); React + SWR + Tailwind (vitest).

**Spec:** `docs/superpowers/specs/2026-11-19-conversion-redesign-design.md` (binding: definitions, findings rules, copy). Mockup: `engineers_app/.superpowers/brainstorm/162256-1795140102/content/conversion-redesign.html`.

## Global Constraints

- Copy and rules verbatim from the spec. Evidence rule for ★ / ⚠ and findings: ≥ 3 interviews or ≥ 3 expected at the team rate; `few` groups never marked.
- Interviews started = first non-canceled round in the period (per `interview_outcome`), owner = interview `createdBy`, region = profile's current region, archived included.
- No real-DB runs; commits single line, no trailers. Backend full suite with the usual ignores (2 known failures: test_bidder_ext upload_url_without_storage, test_resume_prompts bad_lists[prompts5-new-1]); app `npx vitest run`, `npx tsc --noEmit`.

## Review Focus

1. Whole team vs All my profiles: cards show team totals vs the subject's rank — never a rank in Whole team.
2. A person with bids but 0 interviews, or interviews but < 30 bids: ranked on bids/interviews, not on per 100.
3. Findings never fire on few/chance-level groups (test on sparse 0.5/100 data).
4. Coverage line numbers reconcile: rows ≤ bids, matched ≤ interviews.
5. Region filter applies to bids, interviews, everyone and findings alike.

---

### Task 1: Backend — summary (cards, everyone, coverage)

**Files:** Create `app/stats/conversion_summary.py`; Modify `app/routers/stats.py` (`/stats/conversion` adds `summary`). Tests `tests_unit/test_conversion_summary.py`.

- `summary = {cards: {bids: {value, self, bidder, rank, of, median, vsMedian}, interviews: {value, rounds, rank, of, median, vsMedian}, per100: {value, rank, of, teamRate}}, everyone: [{userId, name, bids, interviews, per100, isSubject, ranks: {bids, interviews, per100}}], coverage: {rows, bids, matched, interviews}}`; in Whole team: cards carry team totals and `rank`/`of` null, `median` = median per person.
- Reuse the Bids frame's people/bids computation (`app/stats/bids.py`, `interview_rates.counts`) — extract a shared helper rather than copy.
- [ ] Failing tests (incl. Review Focus 1, 2, 4, 5) → implement → full suite → commit `feat: Conversion summary with real bids, interviews started and ranks`

### Task 2: Backend — findings and group marks

**Files:** Create `app/stats/conversion_findings.py`; Modify `app/stats/conversion.py` (groups get `mark`), `app/routers/stats.py` (`findings` list). Tests `tests_unit/test_conversion_findings.py`.

- `findings(report, team_rate, scope) -> list[{kind: "best"|"weak"|"gap", text, evidence}]` per the spec rules; `mark_groups(report, team_rate)` sets `mark`.
- [ ] Failing tests: each rule fires at its edge and not below; sparse 0.5/100 data → no chance findings; max 4, ordered; none → the fallback sentence; marks match findings → implement → full suite → commit `feat: Conversion key findings and best/weak marks`

### Task 3: App — redesigned Conversion tab

**Files:** `src/components/stats/ConversionTab.tsx` (+ test), `ConversionTable.tsx` (+ test), `src/api/endpoints.ts` types; new small components as needed (`ConversionCards`, `EveryoneList`, `KeyFindings`). Remove the StatsFrame bids strip and its `getStatsView` call from this tab.

- Layout and copy per the spec and mockup; Everyone switch default Interviews; admin name click → view that person (scope me, as today); ConversionTable: rate bar with team tick, ★ / ⚠ from `mark`, muted few rows; dark mode; phone layout (cards stack, tables scroll).
- [ ] Failing tests: cards (me vs team scope), Everyone switch reorders, findings render, coverage line, bars + marks, order of sections → implement → full vitest + tsc → commit `feat: Conversion tab redesign — real totals, Everyone, key findings, rate bars`
