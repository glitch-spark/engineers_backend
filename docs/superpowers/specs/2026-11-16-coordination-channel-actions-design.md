# Interview coordination channel — from notifications to actions

Status: design, decisions settled with the user 2026-11-16 (builder check answered); awaiting approval. Builds on the callers project (`feat/callers-and-reminders`,
stacked under `feat/app-reorg`): every platform user is a caller, external callers are name-only, the Slack Confirm
button works through `POST /slack/...` block_actions handling (`app/routers/slack_bot.py:144`), rounds know their owner.

## Why

The coordination channel (`SLACK_CALLER_CHANNEL_ID`) is notification-only today. Each interview with a caller gets
one post — "📞 Caller needed" plus ten field lines (profile, company, JD link, recruiter, stage, time, method, caller,
"⏳ Not confirmed yet", cc) — and changes arrive as thread replies (`app/slack_bot.py` `sync_caller_thread`). Nothing
can be done from it: assigning a caller, confirming, the after-call status and the result are all done in the app.
The user wants the channel cleaned up and as many of those steps as possible doable from Slack.

## The flow (from the user)

1. The interview **owner, or Puma (interview manager)**, assigns the caller — internal (platform user) or external.
2. The caller confirms.
3. **Soon after the interview time is over**, the owner updates the round: **completed, canceled or rescheduled**.
4. **When recruiter feedback arrives**, the owner sets the **result: passed or rejected** — by hand.

## Decisions

1. **Shorter post, edited in place.** One post per interview (as today's single thread), rewritten on every change;
   the thread holds only questions that need an answer (after-call ask, result reminder), not change logs.
   ```
   📞 Lumin Digital · Intro · Mon Nov 16, 1:30 PM EST · Brandon Mayer
   Owner @Justin · Caller: Nicholas ✅ confirmed · Video (link)
   [Choose caller ▾] [Confirm] [Download brief] [Open interview]
   ```
   Recruiter, JD link and other detail move behind **Open interview**.
2. **Choose caller** (static select: platform users, then external callers, then "No caller (Self)") — usable by the
   interview's owner and interview managers (and admins); anyone else gets an ephemeral "Only the owner or an
   interview manager can assign the caller." Assigning updates the round exactly like the app does (same server
   function), resets `confirmed`, and re-renders the post.
3. **Confirm** — same rule and code path as the daily channel's Confirm (`app/round_confirm.py`).
4. **After-call ask** (owner only, user's choice; caller not asked): when a round's end time (`endsAt`, else start +
   1 h — builder confirms the existing default) has passed and the round is still `scheduled`, one thread reply:
   ```
   ↳ @Owner — Lumin Digital intro ended at 2:30 PM. How did it go?
     [Completed] [Canceled] [Rescheduled…]
   ```
   - **Completed** → status `completed`.
   - **Canceled** → opens a small form with an optional reason (`statusNote`) → status `canceled`.
   - **Rescheduled…** → opens a form with date, time (owner's time zone) and optional note → the round's time moves
     (as the app's reschedule does) and stays `scheduled`; the reminders and day post follow the new time.
   - After an answer the ask message is replaced with what was chosen ("✅ Completed — by @Justin"). Only the owner,
     interview managers and admins may answer; others get the ephemeral refusal.
5. **Result** — once a round is `completed`, the post shows **[Passed] [Rejected]** until set; Rejected opens an
   optional feedback note (`statusNote`). Same permissions as 4.
6. **Result chasing — both (user):**
   - **Per-round reminder 7 days after the interview** if its result is still unset: one thread reply mentioning the
     owner with **[Passed] [Rejected]**. Once per round.
   - **Weekly result roundup, Friday 6:00 pm Chicago**: one channel post listing, per owner (mentioned), every
     completed round still without a result, each with **[Passed] [Rejected]**. Answered rounds drop off (the post is
     edited in place); a round stays on following weeks' roundups until answered. Nothing to list → no post.
7. **Settled by the controller (builder-check answers):** each clicker assigns a caller through the path the app
   gives them (owner: the round's caller field; interview managers: the manager caller endpoint); "No caller (Self)"
   turns the caller request off. Only rounds ending after the release get the after-call ask or the 7-day reminder
   (no flood at launch). A clicker whose Slack ID isn't on a platform user gets the ephemeral "Link your Slack in
   Account settings first."
8. Everything the buttons do is the same server code the app uses; the app and Slack always agree.
9. **Interview managers (Puma) can update any interview (user):** after-call status (Completed / Canceled /
   Rescheduled) and result (Passed / Rejected), in Slack **and in the app**. Today the app's round edit
   (`PATCH /interviews/{id}/stages/{stage_id}`, `_load_editable`, `app/routers/interviews.py:499`) allows only the
   owner or an admin; widen it for interview managers for these fields (status, statusNote, the round's time),
   with tests, so Slack and the app stay identical.
10. **Only caller interviews are chased (user, to keep Slack quiet):** the after-call ask, the 7-day result reminder
    and the Friday roundup cover only rounds that have a caller (internal or external) — the interviews that already
    have a coordination post. Owner-only rounds get nothing in Slack.

## Goals / non-goals / anti-goals

- Goals: fewer, shorter messages; assign, confirm, after-call status, reschedule and result doable from Slack.
- Non-goals: creating interviews from Slack; editing the other interview fields; new statuses (the model keeps
  `scheduled | completed | passed | rejected | canceled`; "rescheduled" is a time change, legacy "failed" maps to
  rejected); DMs.
- Anti-goals: a Slack click that changes something the clicker may not change in the app; duplicate asks or reminders;
  losing a click silently (every click gets a visible result or an ephemeral error).

## Constraints

- Slack interactivity: the existing block_actions endpoint and signature check; forms need `views.open` (trigger_id)
  and `view_submission` handling — new. The Slack app's Interactivity request URL must point at the backend
  (also needed for Confirm; verify at release).
- Claims for once-only messages reuse the reminder claim store (`_reminder_claim`); keys `after-call:<roundId>:<epoch>`
  `result-reminder:<roundId>` and `result-roundup:<YYYY-MM-DD>` (the Friday).
- Slack limits: 3,000 chars per section, 50 blocks, select options ≤ 100 (option groups for users/external).
- Ships after the Bids page work, in the same release (user: ship together).

## Left to the builder

- Exact copy beyond the sketches; how the cron triggers the after-call ask (the existing 5-minute alerts tick is the
  natural home); the reschedule form's field layout; migrating existing posts (re-render on next change is enough).
