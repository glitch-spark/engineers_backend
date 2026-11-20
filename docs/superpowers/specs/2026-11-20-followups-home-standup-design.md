# Interview follow-ups on Home and in the daily standup — design

Date: 2026-11-20. The user chose option A ("act on Home"). Small change on top of
2026-11-20-interview-followup-design.md (the three reminder steps, `followUps` on the round, the
`GET /interviews/needs-input` list and the "Needs your input" section of the Interviews page).

## Why

Follow-ups are easy to forget when they live only on the Interviews page and in Slack threads. People start their
day on Home and report it in the daily standup, so both should show the rounds waiting on them, and the team should
see who followed up, without anyone typing it.

## Home (the dashboard's Today card)

A block under today's items, shown only when something waits, for the signed-in person's own interviews:

```
Interviews waiting on you (5)
☐ Follow up with Acme · Intro, Nov 13 · no result for 8 days       [Passed] [Rejected]
☐ Follow up with Globex · Tech round 1, Nov 12 · no result for 9 days  [Passed] [Rejected]
  Did you hear back? Initech · followed up Nov 14     [No, close as rejected] [Passed] [Rejected]
  2 rounds need "How did it go?" · 1 needs a reason                → Interviews
```

- **Follow-up rows** (`follow_up`): the checkbox records "I followed up" (the same call as the button on the
  Interviews page); Passed / Rejected as there (Rejected asks the reason).
- **Heard-back rows** (`heard_back`): the three buttons of the Interviews page, no checkbox.
- **The other two kinds** (`outcome`, `reason`) are one line with their counts and a link to the Interviews page;
  the line is left out when both are 0.
- The count in the heading is every waiting round (all four kinds). A saved row leaves the block. At most 5 rows
  are listed, oldest first; more → "…and {n} more → Interviews".
- A ticked follow-up cannot be unticked (the follow-up is recorded); the row moves to the standup's "done today".

## Daily standup

One line, in the three places the day's update appears (the app's standup draft, the Slack standup form, and the
posted update), right after the Interviews line:

`Follow-ups · done today: Acme, Globex · still to do: 3`

- **done today** = companies of the rounds this person followed up on that day (app timezone), in the order done;
  more than 5 → the first 5 then "+{n}".
- **still to do** = this person's `follow_up` + `heard_back` rounds right now.
- The line is left out when both parts are empty / 0. With nothing done: `Follow-ups · still to do: 3`. With
  nothing left: `Follow-ups · done today: Acme, Globex`.
- Read-only: nothing is typed, and saving the standup does not change it.

## Backend

- The day view that feeds the draft, the Slack form and the posted update (`daily_plans._view`) gains
  `followUps: {doneToday: [companyName…], toDo: number}`, computed from the person's interviews (follow-ups whose
  `at` is in that day; the same rules as `needs-input` for `toDo`). One shared helper with `needs-input`, not a copy.
- Home reads the existing `GET /interviews/needs-input` (own rows); no new endpoint.

## App

- The row rendering and its actions (follow-up, passed, rejected with reason, close as rejected) are shared between
  the Interviews page section and the Home block: one component, two layouts, not a copy.
- Home block in `TodayCard`; the standup draft shows the line where it shows the Interviews line.

## Approaches

1. **Act on Home, one read-only standup line (chosen).** Follow-ups get done where the day starts; the team sees
   them daily for free.
2. Summary on Home with a link — small, but one more click every time (the user declined it).
3. Turn follow-ups into real to-do items of the weekly plan — they would count toward routines and targets, which
   mixes company-driven waiting with the person's own plan.

## Builder's calls (not the user's words)

- Only the person's own rounds on Home and in their standup, also for admins.
- Five rows on Home, then a link.
- Follow-ups do not count toward any plan target or routine tally.

## Constraints

No runs against the real database; mongomock tests; single-line commits without trailers; TDD; backend full suite
with the usual ignores (2 known failures); app `npx vitest run`, `npx tsc --noEmit`.
