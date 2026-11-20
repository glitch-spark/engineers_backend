# Interview reminders in the app, with a follow-up step — design

Date: 2026-11-20. Flow given by the user (three steps, Slack and app alike). Built before the interview statistics
tab (2026-11-20-interview-statistics-design.md) because it captures data that tab reads.

## Why

The reminders about a round exist only in Slack. The Interviews page should ask the same things, so nothing depends
on reading a Slack thread. And a held round that gets no answer should lead to a follow-up with the company and
then be closed, instead of staying open forever.

## The three steps (the user's; same in Slack and in the app)

| Step | When | Question | Answers |
|---|---|---|---|
| 1. After the interview | 15 minutes after the round's end time, round still `scheduled` | How did it go? | Completed · Passed · Rejected · Rescheduled · Canceled (Rejected, Rescheduled and Canceled ask the reason) |
| 2. Follow up | 7 days after the round ended, round `completed`, no follow-up yet | No result yet — follow up with the company | I followed up · Passed · Rejected |
| 3. Heard back? | 7 days after the follow-up, round still `completed` | Did you hear back? | No, close as rejected (reason "No answer from the company", suggested) · Passed · Rejected |

Step 1 exists in Slack today. Step 2 exists in Slack as a plain "How did it end?" (Passed / Rejected) and gains the
follow-up. Step 3 is new. All three are new in the app.

## App: "Needs your input" on the Interviews page

The section that exists today (rounds missing a reason, held rounds missing a result) becomes the home of all
reminders. One row per round, newest first, grouped under small headings in this order:

1. **How did it go?** — step 1 rows. `{company} · {profile} · {stage} · ended {time}`. Buttons: Completed, Passed,
   Rejected, Rescheduled, Canceled. Rejected and Canceled open the reason dialog; Rescheduled opens the Change time
   dialog (new time + reason), both as they exist.
2. **Follow up with the company** — step 2 rows. `No result for {n} days`. Buttons: I followed up, Passed, Rejected.
3. **Did you hear back?** — step 3 rows. `Followed up on {date}, no result yet`. Buttons: No, close as rejected ·
   Passed · Rejected. "No, close as rejected" saves rejected with reason "No answer from the company" in one click.
4. **Add the reason** — canceled / rejected rounds without a reason (as today).

A saved row leaves the list. Admins see the owner's name on each row (as today). The section's count is the number
of rows; hidden when empty.

## Slack

- Step 1: unchanged.
- Step 2 (the existing 7-day reminder): new text `{owner} — {company} {stage} on {time} has no result yet. Follow up
  with the company, then mark it here.` Buttons `I followed up`, `Passed`, `Rejected`. After "I followed up" the
  message becomes `📨 {name} followed up on {date}.`
- Step 3 (new), 7 days after the follow-up: `{owner} — {company} {stage} on {time}: did you hear back after your
  follow-up?` Buttons `No, close as rejected`, `Passed`, `Rejected`. Claim key `heard-back:<roundId>:<followUp at>`;
  the existing `MAX_AGE` rule applies so nothing is back-posted on deploy day.
- Same switches as today (`SLACK_CALLER_ASKS_FROM`, daily channel).
- An answer given in the app also settles the Slack message for that round where one exists (the existing
  "answered" edit), and the other way round, because both read the round's state.

## Data (`InterviewStageEvent`)

- `followUps: List[{at, by}] = []` — append-only.
- `statusAt: Optional[datetime] = None` — set whenever the round's status changes, on every path through the round
  update. Old rounds have none; it cannot be backfilled. The statistics tab uses it for "days until an answer".
- "No, close as rejected" = status `rejected`, `statusReason: "no_answer"`.

## API

- `POST /interviews/{iv_id}/stages/{stage_id}/follow-up` — same access as changing the round's status; the round
  must be `completed`, else 400. Appends a follow-up; returns the interview.
- Round JSON gains `followUps` and `statusAt`.
- `GET /interviews/needs-input` — each item gets `need`:
  - `outcome` (step 1): status `scheduled`, the round ended 15+ minutes ago;
  - `follow_up` (step 2): latest round, `completed`, ended 7+ days ago, no follow-up;
  - `heard_back` (step 3): latest round, `completed`, latest follow-up 7+ days ago; item carries `followedUpAt`;
  - `reason`: canceled / rejected without a reason (as today).
  A `completed` round between steps (ended under 7 days ago, or followed up under 7 days ago) is not listed.
  Rounds since 2026-10-06 only; AI interviews and home assessments never get `outcome`, `follow_up` or
  `heard_back`. `counts` has one number per need. The earlier `result` need is replaced by `follow_up` /
  `heard_back`; app and backend ship together (backend first: an old app ignores needs it does not know).

## Approaches

1. **Follow-ups on the round; the app list and the Slack reminders both read the round's state (chosen).** One
   source of truth, no new collection, no migration; reuses the existing list, dialogs and Slack claims.
2. Close silent rounds automatically (cron writes rejected / no answer) — no clicks, but the system would set a
   status no person chose, and a late answer would have to undo it.
3. A separate reminders / tasks collection shared by Slack and the app — a general to-do engine; far more than
   three steps on one object need.

## Builder's calls (not the user's words)

- If the owner never presses "I followed up", the round stays under "Follow up with the company" in the app; Slack
  does not repeat the reminder. The statistics still count the round as "no answer" 14 days after it was held.
- Pressing "I followed up" again (after step 3 appears, via Slack or app there is no such button) is not offered:
  one follow-up per round is asked for. The field is a list so a later second follow-up can be added without a
  data change.
- Step 1 rows cover rounds since 2026-10-06, so an old round that was never updated shows up once and can be closed.
- `statusAt` is added here because every status change already passes through one place.
- Not built now: a drafted follow-up email; in-app notifications (bell) for these steps.

## Constraints

No runs against the real database; mongomock tests; single-line commits without trailers; TDD; backend full suite
with the usual ignores (2 known failures); app `npx vitest run`, `npx tsc --noEmit`. Depends on the 7-day hold
(`feat/needs-input-hold`) being merged first.
