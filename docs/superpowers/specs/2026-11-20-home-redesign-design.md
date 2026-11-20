# Home redesign (Today, This week, This month) — design

Date: 2026-11-20. Approved by the user from the prototype
(`engineers_app/.superpowers/brainstorm/581247-1795206047/content/home-redesign.html`) — it is binding for layout
and wording; its numbers are samples. Builds on 2026-11-20-followups-home-standup-design.md (the "Interviews waiting
on you" block, which becomes "Follow-ups" here).

## Why

Home is where the day starts, but its numbers don't say what to do: "Bids 124 / 275 · auto" and "Interviews 0 ·
auto" sit in a corner as chips, "Routines 8 / 15" can't be acted on, "▼49 vs pace" and "−$773 ▼122%" need decoding,
and today's interviews aren't on the page at all.

## Today card

Order: heading with the done count → **Daily** (Bids row, Interviews row with today's interviews, then the
person's routines as today) → **This week** (weekly goals, as today) → **Follow-ups**.

- **Bids row** (replaces the chip): `Bids · {done} of {target} · {n} to go` with a bar. Its checkbox ticks itself
  when done ≥ target and cannot be ticked by hand (dashed box, label "ticks itself"). No target → `Bids · {done}`,
  no checkbox, not in the done count.
- **Interviews row** (replaces the chip): `Interviews · {marked} of {total} marked`. Ticks itself when every round
  of today is marked and none is still to come. No rounds today → `Interviews · none today`, no checkbox, not in
  the done count.
- **Today's interviews**, listed under that row in time order, each:
  - `{time} · {company}` · profile · who takes it: `Self`, `Caller: {name} ✓ confirmed`, `Caller: {name}`
    (not confirmed) or `Caller: not assigned yet`;
  - the steps so far: every earlier round `{stage} ✓ passed` / `{stage} ✗ rejected` / `{stage} canceled` /
    `{stage}` (held, no result), then today's round highlighted `{stage} · today`;
  - status of today's round: `In {n} hours` / `In {n} minutes` (not started), `Happening now`,
    `Ended {time} · not marked` with the five buttons (Completed, Passed, Rejected, Rescheduled, Canceled — the same
    actions as the "How did it go?" rows), or `Completed ✓`, `Passed ✓`, `Rejected`, `Canceled`,
    `Rescheduled to {date}`.
- **A round of today** = a round whose start is today (the person's timezone as the day view already uses), or one
  moved away from today (`reschedules` with `fromAt` today and `toAt` another day; shown as Rescheduled). AI
  interviews and home assessments are left out. **Marked** = status other than `scheduled`, or rescheduled away.
- **Done count** in the heading: `{x} of {y} done` over the daily rows that have a checkbox (the two auto rows when
  they apply, plus routines).
- **Follow-ups** (was "Interviews waiting on you"): heading `Follow-ups ({n})` with the hint `tick when you've
  followed up with the company`; follow-up and heard-back rows as built; then one line for OLDER rounds only
  (before today) that need "How did it go?" or a reason, with the link to Interviews. `n` = follow-up + heard-back
  rows. Today's unmarked rounds are answered in the list above, so they are not counted again here.
- The number box of a counted routine is the size of the other inputs.

## This week card

- Heading right side: `{Weekday} · last day` on the last working day, else `{Weekday} · {n} days left`.
- **Bids**: `{total} of {target} · {n} to go today|this week`; bar amber when behind where the week should be
  today, green otherwise (blue with no target). Under it Mon–Fri, each day's bids, days under the daily target in
  amber, today labelled; note `Target {n} a day · amber = under target`. No target → no colours, no note.
- **Interviews**: `{held} held · {passed} passed · {waiting} waiting for a result` (zero parts left out) with a
  stacked bar (passed, rejected, no answer, waiting, canceled) and a second line `{canceled} canceled ·
  {scheduled} more scheduled this week` (zero parts left out). Outcomes as defined in
  2026-11-20-interview-statistics-design.md.
- **Routines** (full width): `Routines · {done} of {expected} done this week`, then one line per routine with Mon–Fri:
  `✓` done, `✗` missed (a past day not done), `{value} / {target}` a counted routine short of its target that day,
  `·` today not done yet, blank for a future day or a day the routine was not shown; last column `{d} of {n} days`.
  Legend line as in the prototype. Days = the week's working days the plan covers (as `routines_tally` counts).

## This month card

- **Earnings**: net this month; `last month {±$}`; `in {$} · out {$}`; link Transactions. No percentage.
- **Interviews in progress**: the stage counts as today (`2 Intro · 1 Tech`), `{n} waiting for a result`,
  `{n} follow-up(s) due`; link Interviews.
- **Pass rate** (the prototype's optional tile, kept): this month's pass rate for the person with the team's,
  `{passed} passed of {decided} decided rounds`; `too few to tell` under 20 decided; link Statistics → Interviews.
  Left out when the person has no decided round this month.

## API contract

All additions; existing keys stay. Field names are binding.

- `GET /daily-plans/{day}` (the day view) adds:
  - `todayInterviews: [{interviewId, roundId, companyName, profileName, stage, scheduledAt, endsAt, status,
    state: "upcoming" | "now" | "unmarked" | "completed" | "passed" | "rejected" | "canceled" | "rescheduled",
    rescheduledTo: ISO | null, who: {kind: "self" | "caller", name: string | null, assigned: bool,
    confirmed: bool}, steps: [{stage, state: "passed" | "rejected" | "canceled" | "held" | "today"}]}]`
    (time order; `steps` ends with the round itself as `today`);
  - `interviewsMarked: {marked, total}`.
- `GET /dashboard/week` adds:
  - `bids.byDay: [{date, done, target: number | null}]` (Mon–Fri);
  - `interviews.outcomes: {held, passed, rejected, noAnswer, waiting, canceled, scheduled}`;
  - `routines.rows: [{id, text, days: [{date, state: "done" | "missed" | "short" | "today" | "future" | "off",
    value: number | null, target: number | null}], doneDays, totalDays}]` beside the existing done / expected.
- `GET /dashboard/net-monthly`: each month carries `in` and `out` (positive numbers) beside `net`, if not already.
- `GET /dashboard/month-interviews` (new, own numbers; admins may pass `userId` like the sibling routes):
  `{pipeline: [{stage, label, count}], waitingForResult, followUpsDue, passRate: {value: number | null, passed,
  decided, few, teamRate: number | null}}`.

## Approaches

1. **Extend the three existing Home reads and add one small month read (chosen).** Each card keeps one request;
   rules (outcomes, marked, follow-ups due) stay in the tested backend and reuse the statistics and needs-input code.
2. One big `/home` endpoint — one request, but it couples three cards that refresh at different moments (a tick
   refetches only the day).
3. Compute in the app from `/interviews` — duplicates the outcome and follow-up rules in the browser.

## Builder's calls (not the user's words)

- The Pass rate tile is kept, because the approved prototype shows it; it was marked optional there.
- Today's unmarked rounds are not repeated in the Follow-ups count.
- Auto rows cannot be ticked by hand.

## Constraints

No runs against the real database; mongomock tests; single-line commits without trailers; TDD; backend full suite
with the usual ignores (2 known failures); app `npx vitest run`, `npx tsc --noEmit`. Dark mode and phone width
(tiles stack, the routines grid scrolls sideways inside its card).
