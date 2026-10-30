# Statistics → Callers (caller pass-rate statement)

Date: 2026-10-30 · Branch: `feat/caller-stats` (on `feat/bids-per-user-chart`: needs its shared filter bar and
custom range) · Frontend: `engineers_app`, same branch name

## Goal

Callers are paid by how many of their interview rounds pass. A Callers tab on Statistics shows, for a period, each
caller's rounds and each round's result, so paying them needs no digging through the Interviews page. Each caller's
view is laid out as the statement that a later PDF export will send them.

Out of scope: pay amounts and pay status, and the PDF export itself (its future shape is noted below so the view is
built for it).

## Who sees it

Admins and interview managers (`routers/callers.is_caller_manager`), as it is about paying callers. Others don't get
the tab; the endpoint returns 403 to them.

## Which rounds count

A caller round is a `stageHistory` entry with `caller.enabled`. A round is in the report when:

- its date (`scheduledAt`, falling back to `at`) is a local day (`dashboard_stats.app_tz`) within the period;
- its interview's profile is in the selected region (`Account.region`), or the region is All, as on the Interview rate
  tab.

Its caller is `caller.callerId`; rounds without one are **Unassigned**.

## Results and pass rate

| Round status | Shown as | Where |
|---|---|---|
| passed | ✓ Passed | statement |
| rejected | ✗ Rejected | statement |
| completed | Awaiting result | statement |
| canceled | Canceled | statement |
| scheduled (or none) | Scheduled | **Scheduled section**, kept apart from the statement |

Pass rate = passed ÷ (passed + rejected); `null` (shown "–") when none is decided. Awaiting, canceled and scheduled
rounds don't change it.

A scheduled round dated before today is flagged "date passed, status not updated", so stale rounds get fixed before
paying.

## Layout (frontend)

Third Statistics tab, **Callers**, after Interview rate and Resume compare. It uses the shared sticky `FilterBar`
(region, Week / Month / Custom), so the period carries over between tabs.

1. **Caller cards**: one per caller in the caller list (`/callers`, in its colour), plus **Unassigned** when it
   has rounds. Each card shows the statement's rounds, ✓ passed · ✗ rejected · awaiting · canceled, the pass rate and
   the number scheduled. Click a card to select it; the first caller with rounds starts selected. Callers with no
   rounds in the period are shown dimmed (still selectable, empty statement).
2. **Statement** of the selected caller: title "‹Caller› · ‹period label›", a **Sort: Date | Company** toggle
   (date: oldest first; company: A–Z, then date), and columns Date · Company · Round (stage) · Engineer · Result.
   Under the table a totals line: Rounds · Passed · Rejected · Awaiting · Canceled · Pass rate.
3. **Scheduled** of the selected caller: the same columns and sort, under its own heading, with the stale flag.

Clicking a row does nothing for now (the Interviews page's Callers tab is where rounds are edited).

### Later: PDF export

A per-caller **Export PDF** button with an **Include scheduled rounds** checkbox: the PDF is the statement (and the
Scheduled section when ticked). The endpoint already returns both, so the export adds no counting of its own.

## Backend

`app/caller_stats.py` (counting, no routes), and the route in `app/routers/metrics.py`:

`GET /metrics/caller-stats?period=week|month|custom&date=YYYY-MM-DD&to=YYYY-MM-DD&region=all|Latam|EU|US`

Dates are checked by the existing `_stats_days` (400 on a bad custom range). Response (all callers at once, so
switching cards needs no request; sorting is done in the browser):

```json
{
  "period": "month", "region": "all", "start": "2026-10-01", "end": "2026-10-31", "label": "Oct 2026",
  "callers": [
    {"callerId": "…", "name": "Muhammad", "color": "sky",
     "counts": {"passed": 5, "rejected": 5, "awaiting": 7, "canceled": 2, "scheduled": 4}, "passRate": 0.5}
  ],
  "rounds": [
    {"interviewId": "…", "roundId": "…", "callerId": "…" , "date": "2026-10-03T15:00:00Z",
     "company": "Acme", "stage": "tech_round_1", "engineer": "Puma", "result": "passed"}
  ]
}
```

`callerId` is `null` for Unassigned (its entry in `callers` has `callerId: null`, name "Unassigned"). `result` is one
of `passed | rejected | awaiting | canceled | scheduled`. Callers are in caller-list order, Unassigned last.

## Old rounds with a typed caller name

Rounds saved before the caller list have only `callerName` (e.g. 7 "Pietro", 6 "Muhammad", 7 "Mohammad"), so they
would land in Unassigned. `scripts/link_typed_callers.py` sets their `callerId` once:

- match `callerName` to a caller's name ignoring case, plus an alias list (`mohammad` → Muhammad);
- "TBD", "Anyone" and unknown names (e.g. "David") are left as they are;
- dry run by default: prints each change (interview, round, typed name → caller) and the left-over names; writes only
  with `--apply`, which is run after the printout is reviewed.

## Testing

Unit tests (mongomock, as `tests_unit/test_interview_rates.py`):

- counts and pass rate per caller, `null` when nothing is decided;
- scheduled rounds counted apart and not in the pass rate;
- period (local days) and region filters; rounds without a caller under Unassigned;
- 403 for a user who isn't a caller manager, 400 for a bad custom range;
- the script: matches by name and alias, leaves TBD/unknown names, writes nothing without `--apply`.

Then the tab is checked in the browser against real data (week, month, custom; both sorts; the Scheduled section).
