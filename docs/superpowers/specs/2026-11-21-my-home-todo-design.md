# My Home as the day's to-do list — design

Date: 2026-11-21. Approved by the user from the prototype
`engineers_app/.superpowers/brainstorm/1114988-1795286084/content/my-home-todo-v3.html` (binding for layout and
wording; its names and numbers are samples). Replaces the personal Home of 2026-11-21-admin-home-design.md (the
admin Home and its person page are unchanged).

## Why

Home is the first page a person opens each day: it says what to do today, and once every row is ticked they are
good to go. The week sits below as a read-only table; older things that wait on them are in their own block on top.

## Page (a non-admin's Home), top to bottom

1. **Pending items** — its own panel, folded by default; the heading is a button `Pending items` with the count in
   red and `Older things waiting on you`. Hidden when empty. Holds the needs-input rows `outcome` (not today's
   rounds), `heard_back`, `reason`, and the profile and bidder flags. **Follow-up rows are not here**: they are in
   Today.
2. **Today · {Fri, Nov 21}** — header `{x} of {y} done · {n} to go`. Rows, in this order:
   - **Bids** — a box that ticks itself (never clickable): ticked when the day's target is reached, and when there
     is no target. `{total} of {target}` (green at or over, amber under) `· self {s} / {selfTarget} · bidder {b} /
     {bidderTarget}` (targets left out when unset), the progress bar, and on the right `this week {total} of
     {target}` (from the week read's `bids.week`, falling back to `bids.total` / `bids.target`).
     Under it, only when `bidsByRegion` has more than one row: a small table Location · Self (`{self} / {target}`,
     amber under) · Bidder · Total.
   - **Interviews** — a self-ticking box: ticked when every round of today is marked and none is still to come, and
     when there is no round today (`none today`). `{marked} of {total} marked · self {s} · caller {c}` (today's
     rounds by `who.kind`), and on the right `this week {done} of {target} planned` (`interviews.done` /
     `interviews.target`; without a target just `this week {done}`). Under it the day's rounds as today (time,
     company, profile, who, steps, status, answer buttons once ended unmarked).
   - **Follow up** — one row per needs-input row with need `follow_up`: a checkbox (`I followed up with {company}`,
     ticked while saving, the row leaves once saved, comes back unticked on failure), `{company} · {stage} · no
     result for {n} days`.
   - **Daily to-dos** — the routine rows, as built.
   - When every row with a box is ticked: a green line `All done for today. You're good to go.`
3. **This week · Nov 17 – Nov 21** — header `{goals done} of {n} done · {days left | last day}`.
   - **One read-only table**, columns: label · Mon … Fri (date in the head, today's column tinted and marked
     `today`) · Week · Planned. Rows:
     - `Bids`: each day's total (green at or over the day's target, amber under, `–` for a day still to come), Week =
       `bids.week.total` (amber under target), Planned = `bids.week.target`; then two minor rows `self` and
       `bidder` (`byDay[].self` / `.bidder`; Week and Planned from `bids.week`).
     - `Interviews` with `self {n} · caller {n}` under the label: each day's cell lists that day's rounds like a
       small calendar — company (bold), then `{time} · {stage} · self | caller {name}`, coloured by status:
       `scheduled` sky (on a day before today: amber, `not marked`), `waiting` amber (`completed`), `followed_up`
       violet (`completed · followed up`), `passed` green, `rejected` red, `no_answer` grey (`no answer`),
       `canceled` grey with the company struck through. An empty day shows `–`. Week = `interviews.done`, Planned =
       `interviews.target`. A legend of the colours under the table.
     - a heading row `Daily to-dos`, then one row per routine (`routines.rows`): each day's mark as the routines grid
       shows it (✓, ✗, a number when short, · today), Week = `doneDays`, Planned = `totalDays`.
   - **Weekly goals** — the goal rows with their checkboxes, as built.
   - An older server (no `byDay` / `rounds`): the rows show what they have; the table is left out when there are no
     days.

No This month card, no Week / Day picker, no tiles.

## API (additive; backend commit b5bbdb9)

`GET /dashboard/week`: `bids.byDay[]` gains `self`, `bidder`; `interviews.rounds: [{date: YYYY-MM-DD (app
timezone), interviewId, companyName, stage, at: ISO, who: "self" | "caller", callerName: string | null, status:
"scheduled" | "waiting" | "followed_up" | "passed" | "rejected" | "no_answer" | "canceled"}]` for Mon–Fri, in time
order.

## Approaches

1. **One page component fed by the day read, the week read and the pending read (chosen).** The reads exist; the
   week read gains two fields.
2. The admin's person page in owner mode with a to-do list on top — tried and reverted by the user: too much of a
   dashboard.
3. A new single endpoint for the whole page — one request, but a second copy of three reads.

## Builder's calls

Pending starts folded. The week table is read-only, so its Bids and Interviews rows have no box. Chip wording for
the statuses. With no bids target or no round today the row counts as ticked.

## Constraints

No runs against the real database; single-line commits without trailers; TDD; `npx vitest run`, `npx tsc --noEmit`.
Dark mode; phone width (the table scrolls sideways inside its card). Nothing is pushed without the user's word.
