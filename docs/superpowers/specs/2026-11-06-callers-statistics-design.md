# Statistics → Callers: interviews done and pass rate

Date: 2026-11-06 · Branch: `feat/statistics-cleanup` in both repos (on top of the Bids / Interviews tabs)

## Goal

The Callers tab answers two questions at a glance: **how many interviews each caller did** and **how often they
pass**. It compares callers with each other, by round type, with self-taken rounds, and over time. The work lists of
pending and older rounds go away.

Decided with the user:
- Focus: interviews done and pass rate.
- In: a comparison table, pass rate by round type, caller vs self rounds, and a weekly trend.
- Keep the rounds-held list for the picked caller. Remove **Outdated pending** and **Scheduled**.
- No pay numbers.

## Definitions

- **Caller round:** a round with an enabled caller. Its caller is resolved as today: `caller.callerId`, else a typed
  name matching the caller list, else Unassigned.
- **Round:** rounds follow `app/interview_rounds._dated_rounds`. Consecutive history entries of one stage are one
  round, and the latest status wins. Canceled rounds, AI interviews and home assessments are not interviews done.
  - Today `caller_stats` counts each history entry, and counts AI interviews and home assessments too. This changes.
- **Held day:** the round's `scheduledAt` (else `at`) on the app's local day. It must fall in [start, end].
- **Result:** `round_result(status)`, which gives passed, rejected, canceled, awaiting (completed, no result yet) or
  scheduled.
- **Interviews done:** passed + rejected + awaiting. Canceled is shown apart. Scheduled rounds in the period are not
  done; they are counted as `scheduled`.
- **Pass rate:** passed ÷ (passed + rejected); `null` when that is 0.
- **Ranked:** passed + rejected ≥ `MIN_DECIDED = 5`. Unranked callers are listed with their rate, after the ranked ones.
- **vs team:** the caller's pass rate minus the team's caller pass rate, in points; `null` if either is `null`.
- **Round type:** `interview_rounds._breakdown_group(stage)`, one of intro, tech, hiring, panel or final.
- **Self rounds:** rounds with no enabled caller, held in the period and region, with the same done / pass-rate rules.
- **Region:** the interview profile's region, as today.

## 1. Backend: `app/caller_stats.py`, `GET /metrics/caller-stats`

The response keeps `period`, `region`, `start`, `end` and `label`, and changes as follows.

```
callers: [{callerId, name, color,
           counts: {passed, rejected, awaiting, canceled, scheduled}, done, passRate, ranked, vsTeam,
           byType: {intro: {done, passed, rejected, passRate}, tech: {...}, hiring, panel, final}}]
team:   {counts, done, passRate, byType}       # every caller round (Unassigned included)
self:   {counts, done, passRate, byType}       # self-taken rounds
weekly: {unit: "day" | "week", buckets: [{label, start, end,
          all: {done, passRate}, callers: {<callerId or "unassigned">: {done, passRate}}}]}
rounds: [...]                                  # rounds held in the period (section "period" only), fields as today
```

- `outdated` and `scheduled` rounds are no longer listed. `counts.outdated` goes.
- Callers keep today's order and inclusion rules (`_caller_rows`): the caller list, then removed callers that have
  rounds, then Unassigned.
- Weekly buckets reuse `interview_rates.buckets` / `unit`: days for a one-week period, weeks for a month or a custom
  range of up to about 13 weeks, as on the Interviews tab. A bucket's `done` across callers adds up to the caller's
  `done`.
- Loading:
  - one interview query for rounds dated in the window, unchanged where possible;
  - self rounds come from the same documents plus interviews with no caller rounds. The query becomes
    "rounds dated in [start, end]", the same as `load_interviews_for_window`, but for every user.

## 2. Frontend: `src/components/statistics/CallersTab.tsx`

1. **Tiles:**
   - Interviews done (`team.done`);
   - Caller pass rate (`team.passRate`);
   - Self pass rate (`self.passRate`);
   - Awaiting result (`team.counts.awaiting`).
2. **Callers table**, replacing the cards:
   - columns: Caller, Interviews done, Pass rate (with a bar), vs team, Passed, Rejected, Awaiting, Canceled;
   - default sort: pass rate, ranked first; every column sorts;
   - rows: an "All callers" row first, then one per caller. Unranked rates are faded with the title "Under 5 decided rounds";
   - clicking a row picks that caller for sections 4 and 5. The picked row is highlighted;
   - callers with no rounds in the period are hidden.
3. **Pass rate by round type:**
   - rows: callers with rounds, then **All callers**, then **Self**;
   - columns: Intro, Tech, Hiring, Panel, Final;
   - each cell reads `60% · 10` (pass rate · done), or `—` with none;
   - the cell's background is tinted by pass rate, with the number always shown.
4. **Trend:** the picked caller (default All callers), with interviews done as bars per day or week and the pass rate
   as a line on a 0–100% second axis. The team's caller pass rate for the period is a dashed reference line.
5. **Rounds held · {caller} · {period}:** today's table without the reject button, plus the sort control and the PDF export.

Removed: the caller cards, Outdated pending, Scheduled, the Reject dialog and its call, and their copy.

**PDF** (`src/lib/callerStatsPdf.ts`):
- a summary table that mirrors the Callers table (or just the picked caller);
- the by-round-type grid, including Self;
- Rounds held.

The outdated and scheduled sections go.

**Types** (`src/api/endpoints.ts`): `CallerStats` and its caller / round types are updated to the response above.

## 3. Errors

- No caller rounds in the period: the tiles show 0 and `—`, the table shows "No caller rounds in this period", and
  the grid and the trend are hidden.
- A `null` pass rate shows as `—` everywhere.

## 4. Testing

`tests_unit/test_caller_stats.py`:
- done excludes canceled and scheduled;
- AI interviews and home assessments are not counted;
- consecutive same-stage entries are one round;
- pass rate, `ranked` at exactly 5 decided rounds, `vsTeam`;
- `byType` grouping (tech_round_1 and live_coding → tech);
- the `self` totals and `byType`;
- weekly buckets add up to each caller's `done`;
- `rounds` holds only period rounds;
- the region filter.

Existing tests that asserted outdated or scheduled sections are updated or removed.

Frontend: `npm run lint` and `npm run build`.

Live check, read-only: for October 2026 and region all, caller `team.done + team.counts.scheduled` must equal the
Interviews tab's `totals.callerInterviews`, and `self.done + self.counts.scheduled` must equal its
`totals.selfInterviews`. The Interviews tab only counts interviews owned by non-admin users, and the Callers tab counts
every interview, so the check counts admin-owned rounds separately and expects the difference to equal them. Any other
difference is explained before merge.
