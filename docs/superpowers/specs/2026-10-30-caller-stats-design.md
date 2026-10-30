# Statistics → Callers (caller pass-rate statement)

Date: 2026-10-30 · Branch: `feat/caller-stats` (on `feat/bids-per-user-chart`: needs its shared filter bar and
custom range) · Frontend: `engineers_app`, same branch name

## Goal

Callers are paid by how many of their interview rounds pass. A Callers tab on Statistics shows each caller's
statement for a period: the rounds whose result came in during it, and each round's result. That way paying them
doesn't mean digging through the Interviews page. A paid statement doesn't change afterwards, and rounds still
waiting for a result are listed apart until they get one. The layout is the statement that a later PDF export will
send the caller.

Out of scope:
- pay amounts and pay status;
- the PDF export itself (its future shape is noted below so the view is built for it);
- any automatic "no response" timeout. A pending round is rejected by hand (see "Mark rejected").

## Who sees it

Admins and interview managers (`routers/callers.is_caller_manager`), as it is about paying callers. Others don't get
the tab, and both endpoints return 403 to them.

## Round results

A caller round is a `stageHistory` entry with `caller.enabled`. Its caller is `caller.callerId`; rounds without one
are **Unassigned**. Its round date is `scheduledAt` (falling back to `at`).

| Round status | Shown as | Section |
|---|---|---|
| passed | ✓ Passed | Statement, by result date |
| rejected | ✗ Rejected | Statement, by result date |
| canceled | Canceled | Statement, by result date |
| completed | Awaiting result | Pending |
| scheduled (or none) | Scheduled | Scheduled |

A round already turns passed when the next round is added (`routers/interviews.mark_previous_passed`). So
"Awaiting" means held, with no next round and no result entered.

### Result date (new)

`InterviewStageEvent.resultAt: Optional[datetime]` records when a round got its result:

- It is set to now when a round's status becomes passed, rejected or canceled and `resultAt` is empty.
- It is cleared when the status goes back to scheduled or completed.
- A change between passed, rejected and canceled keeps the first `resultAt`.

It is set in one place, `routers/interviews._save_interview`, so every write path gets it: create, edit, round
edit, add round and Mark rejected. Rounds decided before this change have no `resultAt`; they use their round date.

## The statement for a period

All days are app-local days (`dashboard_stats.app_tz`). The region is the interview profile's (`Account.region`),
as on the Interview rate tab; All means every region.

- **Statement**: decided rounds (passed, rejected, canceled) whose result date falls within the period. A result
  that comes in late lands in the period it came in, so a past statement doesn't change.
- **Pending**: rounds awaiting a result now, held on or before the period's last day. Each shows how many days ago
  it was held, and each has a **Mark rejected** action.
- **Scheduled**: rounds still scheduled now, dated on or before the period's last day. Rounds whose date has passed
  are flagged "date passed, status not updated".

Pending and Scheduled show the rounds as they are now, not as they were at the end of the period.

**Pass rate** = passed ÷ (passed + rejected) in the statement. It is `null` (shown "–") when there are none. Canceled,
pending and scheduled rounds don't change it.

## Layout (frontend)

The tab is the third on Statistics, **Callers**, after Interview rate and Resume compare. It uses the shared sticky
`FilterBar` (region, Week / Month / Custom), so the period carries over between tabs.

1. **Caller cards.** There is one per caller in the caller list (`/callers`, in the caller's colour), plus
   **Unassigned** when it has rounds.
   - Each card shows ✓ passed · ✗ rejected · canceled, the pass rate, and the pending and scheduled counts.
   - Clicking a card selects it. The first caller with rounds starts selected.
   - Callers with nothing in any section are dimmed.
2. **Statement** of the selected caller.
   - Title: "‹Caller› · ‹period label›".
   - A **Sort: Date | Company** toggle. Date sorts by result date, oldest first; Company sorts A–Z, then by date.
   - Columns: Result date · Company · Round (stage) · Round date · Engineer · Result.
   - A totals line: Passed · Rejected · Canceled · Pass rate.
3. **Pending.**
   - Columns: Round date · Company · Round · Engineer · Days waiting · [Mark rejected].
   - Mark rejected asks for confirmation ("Mark Acme · Tech 1 as rejected?"). The round then moves into the current
     period's statement.
4. **Scheduled.** Columns: Round date · Company · Round · Engineer, with the stale flag.

Pending and Scheduled use the same sort toggle. Their date column is the round date.

### Later: PDF export

A per-caller **Export PDF** button with an **Include scheduled rounds** checkbox. The PDF is the statement, plus
the Scheduled section when the box is ticked. The endpoint already returns both, so the export does no counting of
its own.

## Backend

- `app/caller_stats.py` holds the counting and no routes.
- `app/routers/metrics.py` holds the report route.
- `app/routers/callers.py` holds Mark rejected, next to the other caller-manager routes.

### `GET /metrics/caller-stats?period=week|month|custom&date=YYYY-MM-DD&to=YYYY-MM-DD&region=all|Latam|EU|US`

- Dates are checked by the existing `_stats_days`; a bad custom range returns 400.
- It returns all callers at once, so switching cards needs no new request.
- Sorting is done in the browser.

```json
{
  "period": "month", "region": "all", "start": "2026-10-01", "end": "2026-10-31", "label": "Oct 2026",
  "callers": [
    {"callerId": "…", "name": "Muhammad", "color": "sky",
     "counts": {"passed": 5, "rejected": 5, "canceled": 2, "pending": 7, "scheduled": 4}, "passRate": 0.5}
  ],
  "rounds": [
    {"interviewId": "…", "roundId": "…", "callerId": "…", "section": "statement",
     "roundDate": "2026-10-03T15:00:00Z", "resultDate": "2026-10-06T09:12:00Z",
     "company": "Acme", "stage": "tech_round_1", "engineer": "Puma", "result": "passed"}
  ]
}
```

- `section` is one of `statement | pending | scheduled`.
- `result` is one of `passed | rejected | canceled | awaiting | scheduled`.
- `resultDate` is `null` outside the statement, and the round date for rounds decided before `resultAt` existed.
- `callerId` is `null` for Unassigned. Its `callers` entry has `callerId: null` and the name "Unassigned".
- Callers come in caller-list order, with Unassigned last.

### `POST /callers/rounds/{interview_id}/{round_id}/reject`

- For caller managers only, on any user's interview: interview managers can't edit other users' interviews through
  the interview routes.
- It only applies to a round that is awaiting a result (status `completed`). Any other status returns 409, and an
  unknown interview or round returns 404.
- It sets the status to rejected and saves through `_save_interview`, so `resultAt` is set.
- It then runs the same #caller sync as a round edit (`_sync_caller_posts`), so the Slack thread gets its usual
  "ended" reply.

## Old rounds with a typed caller name

Rounds saved before the caller list existed have only `callerName` (for example 7 "Pietro", 6 "Muhammad" and 7
"Mohammad"), so they would land in Unassigned. `scripts/link_typed_callers.py` sets their `callerId` once:

- It matches `callerName` to a caller's name ignoring case, plus an alias list (`mohammad` → Muhammad).
- It leaves "TBD", "Anyone" and unknown names (for example "David") as they are.
- By default it is a dry run: it prints each change (interview, round, typed name → caller) and the left-over
  names. It writes only with `--apply`, which is run after the printout is reviewed.

## Testing

Unit tests use mongomock, like `tests_unit/test_interview_rates.py`:

- `resultAt`:
  - set when a round is decided;
  - kept across passed ↔ rejected;
  - cleared when the round goes back to completed;
  - set on the previous round by add round.
- Statement:
  - grouped by result date, so a late result lands in the later period;
  - older rounds fall back to their round date;
  - region filter;
  - Unassigned rounds.
- Pass rate:
  - per caller;
  - `null` when nothing is decided;
  - canceled, pending and scheduled rounds leave it unchanged.
- Pending and scheduled:
  - current state, held on or before the period end;
  - stale scheduled rounds are flagged.
- Mark rejected:
  - works on an awaiting round;
  - 409 when the round isn't awaiting;
  - 403 for someone who isn't a caller manager;
  - calls the Slack sync.
- 400 for a bad custom range.
- The script:
  - matches by name and alias;
  - leaves TBD and unknown names;
  - writes nothing without `--apply`.

Then the tab is checked in the browser against real data:
- week, month and custom periods;
- both sorts;
- Pending with Mark rejected, run on a test interview only;
- the Scheduled section.
