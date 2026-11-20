# Conversion — complete interview counts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Conversion counts every interview the user expects: archived profiles, interview-proven applications, and interviews not tied to a bid (totals); interviews without a scored resume get a fit score from a one-off script.

**Architecture:** Backend scope/loader changes (archived in, `byInterviewOnly` in), a scoped `untied` count, a fit-score script + sync hook; app shows the new totals line and archived profiles in the picker.

**Tech Stack:** FastAPI + Beanie (mongomock tests); React + SWR (vitest).

**Spec:** `docs/superpowers/specs/2026-11-19-conversion-tab-design.md` — section "Change 2026-11-19" (binding) plus the rest for context.

## Global Constraints

- Archived profiles count in every scope; region = profile's current region.
- `byInterviewOnly` rows count everywhere (loader, response window, grades, suggestions, conversion tables).
- `untied` = interviews on the scope's profiles (accountId in the scope's account ids) whose first non-canceled round is in the period's naive-UTC bounds, countable per `app.applications.rows.interview_outcome`, and whose `_id` no `applications` row carries.
- Totals line copy: `{label} · {bids} bids · {matured} matured · {interviews} interviews from matured bids ({interviewsAll} in all) · {rate}% · {inPlay} in play · {untied} more interviews not tied to a bid` (last part only when untied > 0); second line: `Includes applications known only from their interview (no tracked bid), so rates can read a little high.`
- Fit score script never runs in the cron; dry run by default; `--apply` writes `fitScore: {total, band}` on the interview. No real-DB runs in tests; the controller runs the script only with user approval.
- Commits single line, no co-author / "Generated with" trailers. Backend full suite with the usual ignores (2 known failures); app `npx vitest run`, `npx tsc --noEmit`.

## Review Focus

1. An archived profile's rows and interviews appear in My profiles / Team total and in the picker (Task 1 test, Task 3 test).
2. An interview-proven row counts in bids and interviews of the conversion totals and its groups (Task 1 test).
3. An interview counted in `untied` is never also counted in `interviewsAll` (Task 1 test).
4. The fit-score script skips interviews that already have a tailored-resume score or no job description, and is idempotent (Task 2 tests).

---

### Task 1: Backend — archived in, interview-proven in, untied count

**Files:** `app/stats/conversion_scope.py`, `app/stats/profile_loader.py`, `app/applications/window.py`, `app/routers/stats.py` (`/stats/conversion` totals gets `untied`; response `notes` not needed — the app owns the copy). Tests: `tests_unit/test_conversion_scope.py`, `tests_unit/test_stats_conversion.py`, `tests_unit/test_applications_window.py`, `tests_unit/test_job_fit.py` (loader).

- [ ] Failing tests: `test_archived_profiles_count_in_every_scope`; `test_interview_only_rows_count_in_conversion`; `test_window_includes_interview_only_rows`; `test_untied_counts_interviews_no_row_carries` (+ not double counted, + scoped to the scope's profiles, + by first round in period).
- [ ] Implement; full suite.
- [ ] Commit `feat: Conversion counts archived profiles, interview-proven applications and untied interviews`

### Task 2: Backend — fit score for interviews without a scored resume

**Files:** Create `scripts/score_interview_fit.py`; Modify `app/models/interview.py` (`fitScore: Optional[dict] = None` on Interview), `app/applications/sync.py` / `rows.py` (matched row without score takes the interview's `fitScore`). Tests: `tests_unit/test_score_interview_fit.py`, `tests_unit/test_applications_sync.py`.

- `async def score_interview_fit(apply: bool, since: date = DATA_START) -> dict` — interviews since `since` with `resumeLink.kind` in fixed / uploaded / none or no resumeLink, a non-empty `resumeLink.jobDescription`, and no `fitScore`; base resume text = the profile's (Account) resume text (find the field the Resume Generator / scoring uses for a profile's base resume; if none, skip and count `noResume`); score with `score_manual(jd, text, owner)`; store `{total, band}`; returns counts `{candidates, scored, noResume, unscorable}`. Dry run computes counts without scoring calls? — No: dry run lists candidates and counts only (no scoring, no LLM cost).
- [ ] Failing tests: candidates selection (skips tailored, skips no-JD, skips already scored); apply stores fitScore (patch `score_manual`); idempotent second run; sync uses fitScore when the row has no score and keeps a tailored score when present.
- [ ] Implement; full suite.
- [ ] Commit `feat: fit score for interviews without a scored resume (one-off script) feeds the score bands`

### Task 3: App — totals line, note, archived in picker

**Files:** `src/components/stats/ConversionTab.tsx` (+ test), `src/api/endpoints.ts` (`untied`), profile picker source (include archived, label `{name} · {region} · archived`).

- [ ] Failing tests: totals line with and without `untied`; the "known only from their interview" note; archived profile listed and selectable.
- [ ] Implement; full vitest + tsc.
- [ ] Commit `feat: Conversion totals show untied interviews; archived profiles in the picker`
