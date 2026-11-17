# Callers and interview reminders — design

Date: 2026-11-14 · Repos: engineers_backend (most of it), engineers_app (Callers tab, picker, brief deep link)

## Why

Caller handling has drifted from how the team works:

- Callers are a bare name + colour list in a modal (`CallersManager`). Anyone on the platform can take a call, but the
  picker only knows the hand-kept list, so platform users can't be assigned as themselves and never get pinged.
- "Confirmed" is broken. The badge says "Waiting for the interview manager", but only the owner or an admin can set it
  (`routers/interviews.py` `_load_editable`; `InterviewSummary` shows "Mark confirmed" only when `canEdit`).
  Since 2026-10-01: 41 caller rounds, **0 ever confirmed**, so every daily-list line says "not confirmed" and the flag
  means nothing. Meanwhile 8 scheduled rounds have caller "TBD", which the noise hides.
- Reminders: only caller rounds get one (a reply in the #caller thread ~30 min before, tagging owner + interview
  managers). Self-taken rounds get nothing. The caller is never tagged.
- The daily list (`SLACK_CALLER_DAILY_CHANNEL_ID`, 05:00 Chicago) is plain text, caller rounds only, posted once and
  never updated, with no way to get the interview brief from it.

## Goals

1. Every platform user can be picked as a caller, with no setup. External callers stay as a managed name list.
2. Callers get their own **Callers** tab on the Interviews page (list + CRUD), replacing the modal.
3. Confirmation works: the assigned caller (or a manager) confirms, from Slack or the app; it means "the caller said yes".
4. One Slack channel — the existing daily caller channel — carries all interview reminders:
   a morning post of the day's rounds that stays current, and 30-min reminders that tag exactly who is on the call.
5. Each round in Slack has **Download brief** and **Open interview**, and the assigned caller can actually use them.

## Non-goals

- Contact details for external callers (none for now: no Slack, phone or email).
- DMs. Reminders go to the channel only.
- Changing what the interview brief contains (`lib/interviewBriefPdf.ts` stays as is).
- Changing who can *edit* interviews (still owner or admin).
- The #caller coordination threads (Caller needed / moved / rescheduled / confirmed replies) stay as they are, except
  that the 30-min reminder no longer posts there.

## Anti-goals

- A channel that floods people: one post per day plus thread replies, never one top-level message per reminder.
- Slack posts that expose candidate resumes to the whole channel (no file attachments; the brief downloads in the app).

## Decisions (made with the user)

| Topic | Decision |
|---|---|
| Who can be a caller | **Every active platform user, automatically** (option A), plus the managed external callers. |
| External callers | Name only, no contact details. |
| Confirmation | **Make it work** (option A): the assigned caller confirms; interview managers/admins can confirm for anyone, incl. external callers. |
| Channel | Reuse the existing daily caller channel (`SLACK_CALLER_DAILY_CHANNEL_ID`) for the morning post **and** the 30-min reminders. |
| Who is tagged 30 min before | Everyone on the call: the platform-user caller, and the owner. Self round → the owner. External or TBD caller → the interview managers (to pass it on / assign). |
| Brief in Slack | A **Download brief** link button that opens the app and downloads there (no file in Slack). |
| Callers UI | A **Callers** tab on Interviews; the current "Callers" tab is renamed **Caller schedule**. |

## Approaches considered

- **Platform callers** — A: every user auto-listed (chosen; zero setup, anyone can be pulled in at short notice).
  B: managers add platform users to the list (cleaner picker, but one more step every time; rejected by the user).
- **Confirmation** — A: make it work with a Slack button for the caller (chosen). B: drop it and treat "assigned" as
  "lined up" (simpler, but loses "assigned but hasn't said yes").
- **Where reminders go** — one shared channel (chosen; the user already has the daily channel), DMs (quietest, but
  managers lose the overview), both. Separate channels for self vs caller rounds was rejected as overwhelming.
- **Brief from Slack** — link into the app (chosen; reuses the client-side brief and keeps resumes out of the channel)
  vs the bot uploading the PDF + resume (exposes resumes channel-wide, needs a server-side brief builder).

## Design

### 1. Caller identity

A round's caller is either an **external caller** (a `Caller` document, as today) or a **platform user** (`UserExtra`).

- `InterviewCaller` (round-level, `models/interview.py`) gains a kind: `external` | `user`. `callerId` is the `Caller`
  id or the user id accordingly. `callerName` stays as the name snapshot (Slack text, older screens, removed callers).
  Existing rounds are all `external` (or TBD); migrate by defaulting the kind, no data rewrite needed.
- **The owner is never their own caller** (my call). An interview's owner is not offered in the picker for rounds of
  their own interview; taking the call yourself is the existing "Self" (caller off). This keeps `caller.enabled` the
  single self-vs-caller switch for stats, daily-plan interview counts, #caller threads and reminders.
- **Merge external callers who are also users** (my call). Checked 2026-11-14: the external list is Muhammad, Pietro,
  Jamil's group, Nicholas; only **Nicholas** is also an app user (2 interviews have him as caller). Migration rewrites
  those rounds to `user` kind with his user id and archives the external "Nicholas", so he appears once.
- Platform callers = active users (approved, not deactivated), all roles including admins and interview managers.
  (7 users today, so the People group is short.)
  Their display name is the user's name (fallback email). Renaming a user follows onto rounds the same way
  `_rename_on_rounds` does for external callers.
- Colours: external callers keep their chosen colour. Platform users get a colour derived from their id (stable), and
  calendar chips show initials as well as colour so reuse among 8 colours stays readable. Managers may override a
  platform user's caller colour (stored per user; builder decides where).
- `caller_stats.py` and Statistics → Callers include platform callers (grouped by kind+id).

### 2. Caller picker (round form, Caller schedule dialog)

One searchable picker, two groups: **People** (platform users, the current user first) and **External callers**, plus
"Not assigned yet (TBD)". Archived external callers and deactivated users are not offered, but a saved one stays
shown as "(not on the list)", as today.

### 3. Interviews page tabs

`Interviews · Caller schedule · Callers` (today: `Interviews · Callers`).

- **Caller schedule** = today's `/interviews/callers` (+ `/calendar`), renamed. Routes keep working; builder may add
  `/interviews/caller-schedule` with the old paths redirecting.
- **Callers** (new, e.g. `/interviews/callers/list` or `?view=` — builder's call; old modal removed):

```
Callers                                              [+ Add external caller]
People in the app                     Upcoming  This week  Pass rate
  ● Kelvin                                 2         5       60% →
  ● Justin                                 0         1       —
External callers
  ● Muhammad                               3         7       64% →     ⋯ Edit · Archive
  ● Jamil's group                          0         2       71% →     ⋯
  [ ] Show archived
```

  - Platform users are listed read-only (name comes from the account); all active users are shown (7 today). If the
    team grows past ~20, the builder may collapse people with no caller rounds behind "Show everyone".
  - External callers: Add / Edit (name, colour) in a side panel; **Archive** instead of delete (hidden from pickers,
    history and stats keep the name); Delete only when the caller has never had a round.
  - Row click → Caller schedule filtered to that caller. Pass rate → Statistics → Callers for that caller.
  - Everyone sees the tab; add/edit/archive only for admins and interview managers (`is_caller_manager`).

### 4. Confirmation

- Meaning: the assigned caller has said they'll take the call.
- Who can confirm/unconfirm: the **assigned platform caller**, **interview managers**, **admins**, and (as today) the
  **owner**. For external callers, a manager confirms on their behalf.
- Where: [Confirm] button in Slack (daily post + 30-min reminder), "Mark confirmed" in the interview panel and in the
  Caller schedule dialog (`CallerRoundDialog`).
- Backend: a dedicated endpoint (e.g. `POST /interviews/{id}/stages/{roundId}/confirm` with `{confirmed: bool}`) with
  its own permission check, instead of the owner/admin-only `PATCH`. It fires the existing "confirmed"/"unconfirmed"
  #caller thread event and refreshes the day's post.
- Cleared automatically (existing behaviour, keep): reschedule; also clear on caller change.
- Copy: badge tooltip "Waiting for the caller to confirm"; TBD rounds show "No caller yet" instead of "Not confirmed".
- Slack button: reuse the interactivity endpoint (`routers/slack_bot.py`, `block_actions`, as `slack_transactions.py`
  does). Map the clicking Slack user to a platform user by Slack member ID; if they aren't allowed, reply ephemerally
  "Only the caller or an interview manager can confirm".

### 5. Access for the assigned caller

The assigned platform caller of any round of an interview can **view** that interview (`GET /interviews/{id}`, panel,
`/interview/:id` read-only), download its brief and linked resume, and confirm. Editing stays owner/admin. Today a staff
caller would get 403 from the Slack links.

Brief fallbacks for the caller (my call): when no resume is linked, the caller may pick from **that profile's stored
resume files** (read-only, no cost — they are taking the call as that profile). The paid "restore" (regenerating a
missing file with the LLM) stays owner/admin; the caller sees "The resume file is missing — ask the owner to restore
it". Access is granted through the interview (caller of one of its rounds), not by widening profile/job checks
globally (`_can_open_resume_files`, the resume job checks).

### 6. Slack: the day's post (same channel as today)

Posted at `SLACK_CALLER_DAILY_TIME` (05:00 America/Chicago) as Block Kit, and **edited in place** whenever a round of
that day is added, assigned, confirmed/unconfirmed, rescheduled, canceled or removed (store the post's channel + ts per
day; same pattern as the bid reports' in-place updates).

Contents: **all of the day's active rounds** — caller rounds and self-taken rounds — by start time:

```
⏰ Interviews today — Fri Nov 13

10:30 AM · KODE Labs · Intro · Kevin Smyth
  Senior Frontend Engineer · Round 1 · Video (Meet link)
  On the call: @Muhammad (caller) ✅ confirmed · owner @Yuari
  [Download brief] [Open interview]

11:30 AM · Skydio · Intro · Andre Ballard
  Phone (Slynumber) · ⚠️ No caller yet — @PUMA
  owner @Panda
  [Download brief] [Open interview] [Assign caller]

2:00 PM · Torus · Intro · James Bankston
  Video · caller Pietro (external) · ⏳ not confirmed — @PUMA
  owner @Panda
  [Download brief] [Open interview] [Confirm]

3:00 PM · Acme · Tech round 1 · Self · @Justin
  [Download brief] [Open interview]
```

- Per round: time (Slack date token so each reader sees their own time), company, stage, profile, position, round
  number, method + meeting link/number, caller (kind) + confirmation, owner, thread permalink to #caller for caller
  rounds.
- Tags: people on the call are tagged (platform caller, owner). "No caller yet" and external-caller-not-confirmed lines
  tag the interview managers on that line (replaces today's single cc line).
- Buttons: **Download brief** and **Open interview** (URL buttons into the app), **Confirm** (interactive; only on
  unconfirmed rounds with an assigned caller), **Assign caller** (URL to the Caller schedule with that round open;
  only on TBD rounds).
- No rounds today: "No interviews today." Large days: stay under Slack's 50-block limit (split into a second message
  or compact older lines — builder's call).
- Rounds before the post time are listed but not reminded retroactively.

### 7. Slack: 30-minute reminders

- ~30 min before each active round (caller **and** self), reply **in the thread of that day's post**, tagging the people
  on the call (see Decisions). If the day's post doesn't exist (not yet posted, posting failed), post top-level in the
  channel.
- Same block as the round's line in the day's post, with the same buttons and "Starting in N min".
- Dedup as today (`SlackAlertLog`, key per round + start time, so a reschedule gets a new reminder).
- The #caller-thread reminder (`run_caller_reminders` → `_reminder_post`) is removed.
- People without a Slack member ID are named, not tagged.

### 8. Brief deep link (app)

`/interview/:id?brief=<roundId>` (or the panel route; builder's call): after sign-in redirect, loads the interview and
runs the existing `useBriefDownload` flow for that round (brief PDF, then the linked resume; the resume picker if none
is linked; restore if the stored file is gone).

### 9. Related fix

Account settings shows the **time zone** only once Slack is linked. Show it always (it drives local days for alerts),
and prompt users without a Slack member ID that they won't be tagged in interview reminders.

## Constraints

- Slack bot token, signing secret and the daily channel are already configured in Railway; no new channel/env needed.
  The user confirmed (2026-11-14) that **everyone is a member of the daily channel**, so tags in it notify everyone;
  no invite step or new bot scopes. (The bot can't read the member list — it lacks the scope — so don't build a
  membership check.) All 7 users now have a Slack member ID (Justin's added 2026-11-14).
- The app-reorganization project later opens a read-only interview summary to everyone; it builds on §5 here and does
  not change what the assigned caller can do.
- The cron (`python -m app.alerts_tick`, every 5 min) drives reminders and the daily post; keep it idempotent.
- Times: rounds are stored naive UTC; the daily day boundary uses `SLACK_CALLER_DAILY_TIMEZONE`.
- Backend tests: see the repo's test conventions; mock Slack (old branches' tests have posted to real Slack before).

## Left to the builder

Exact routes for the Callers tab and caller schedule rename; where platform-user colour overrides are stored; how to
split an oversized day post; Block Kit layout details; migration mechanics for the caller kind; endpoint naming.
