# Caller interviews in Slack: interview managers, one thread per interview, confirmations, daily list

Date: 2026-10-06
Repos: `engineers_backend`, `engineers_app` (branch `feat/interviews`, worktrees `.worktrees/interviews`)
Builds on: the existing #caller integration (`app/slack_bot.py` `notify_round_caller`, `app/interview_caller.py`,
`app/routers/interviews.py` `_sync_caller_posts`), the 30-minute caller reminder and the `SlackAlertLog` claim pattern.
Status: design approved in conversation, pending spec review

## 1. Goal

Caller interviews are coordinated in Slack, one place per interview:

1. A new caller interview posts to #caller (`SLACK_CALLER_CHANNEL_ID`) and tags the owner's **interview manager**.
2. The manager arranges a caller and comments in that thread (the bot does not read replies).
3. The owner (or an admin) marks the round **Confirmed** in the app; the bot replies in the same thread. It also
   replies when the interview is rescheduled, canceled or moved to the next round, tagging the manager and the owner.
4. Every day at 05:00 America/Chicago the bot posts the day's **confirmed** caller interviews to
   #engineer-caller-test (`SLACK_CALLER_DAILY_CHANNEL_ID`).

### Out of scope

Reading Slack replies (no Events API); manager permissions on interviews (managers do not gain view or edit
rights); a "manager" role; changes to the 30-minute reminder, per-user digests or daily-plan posts.

## 2. Data

### Users (`UserExtra`)

- `interviewManagerId: Optional[ObjectId]` — another app user; set by admins only.
- `slackUserId` (exists) becomes **typed by the user**: a Slack member ID matching `^[UW][A-Z0-9]{8,11}$`
  (trimmed, upper-cased). Blank clears it. Unique across users: saving an ID another user holds is a 409
  ("That Slack ID is already used by another user"). Existing values from the OAuth flow are kept as they are.
- The Connect Slack (OAuth) flow is removed: routes `POST /integrations/slack/oauth-start`,
  `GET /integrations/slack/oauth-callback`, `DELETE /integrations/slack/disconnect` and their UI. DMs, the digest and
  the test DM keep working off `slackUserId` with the bot token.

### Rounds (`InterviewStageEvent`) and interviews

- `confirmed: bool = False` on each round; the interview's top-level `confirmed` mirrors the latest round like
  `status` does (`_sync_top_from_tip`). Exposed in API responses as `confirmed`.
- `confirmed` resets to `False` when a round's `scheduledAt` changes (any write path, including the legacy
  `PUT /interviews/{id}` stage/status path). A new round starts `False`.
- `Interview.slackThread: Optional[{channel: str, ts: str}]` — the interview's single #caller thread, set when the
  first caller post is made. Interviews that already have per-round posts (`caller.slackChannelTs`) and no
  `slackThread` adopt their latest round's post as the thread (`channel` = the current `SLACK_CALLER_CHANNEL_ID`) on
  their next caller-relevant write; their older round posts are left alone.

## 3. API

- `PATCH /interviews/{id}/stages/{stage_id}` accepts `confirmed: bool`. Same edit permission as today (owner or
  admin; `_load_editable`). `confirmed: true` on a round without a caller is a 400 ("Only caller rounds are
  confirmed"). Responses include `confirmed` on rounds and on the interview.
- `PATCH /integrations/slack/prefs` accepts `slackUserId: Optional[str]` (validation and uniqueness above).
  `GET /integrations/slack/status` returns it (`slackUserId`), with the existing timezone and digest fields.
- `PUT /users/{id}` (admin) accepts `interviewManagerId: Optional[str]`: must be an existing, active user other than
  `{id}` (else 400 "Choose another active user"); `null` clears it. `GET /users` returns `interviewManagerId` and
  `interviewManagerName`.

## 4. Slack behaviour in #caller (one thread per interview)

### Main post

The existing `caller_round_text`, describing the **latest round**, plus:
- `Interview manager: <@manager>` (or the manager's name without a ping when they have no Slack ID, or
  `no interview manager assigned`).
- A badge line: `:white_check_mark: Confirmed` or `:hourglass_flowing_sand: Not confirmed yet`.
- When the latest round has no caller: the post says `No caller needed for {stage}` instead of the caller fields.

It is created by the first caller round and edited in place afterwards (`chat.update`); a gone message is reposted as
today and the new ts saved to `slackThread`.

### Thread replies

One reply per write, chosen from the before/after snapshots of the affected round (first match wins). Every reply
tags the manager and the owner (`_mention`, name fallback without a Slack ID).

| Change | Reply |
|---|---|
| New round added (next round) | `:arrow_right: *Moved to {stage}* — {time} · needs confirmation` (`· no caller needed` when the new round has none) |
| `status` → canceled | `:no_entry: *Canceled* — {stage} · {time}` (as today) |
| canceled → active | `:white_check_mark: *Back on* — {stage} · {time} · needs confirmation` |
| `scheduledAt` changed | `:repeat: *Rescheduled* — {old time} → {new time} · needs confirmation` |
| `confirmed` false → true | `:white_check_mark: *Confirmed* — {stage} · {time} · caller {callerName}` |
| `confirmed` true → false (no time change) | `:warning: *Confirmation removed* — {stage} · {time}` |
| other caller fields (method, link, caller, coworkers, stage, status) | existing `:pencil2: *Caller details updated*` reply |
| round deleted | `:wastebasket: *Round removed* — {stage}`; main post re-describes the new latest round |
| interview deleted | main post deleted (as today) |

Times use `slack_time` (each reader's own timezone). Posting stays inline in the request (as today); Slack failures
are logged and never fail the save.

## 5. Daily list (#engineer-caller-test)

- Settings: `SLACK_CALLER_DAILY_CHANNEL_ID` (unset = no daily post), `SLACK_CALLER_DAILY_TIME` (default `05:00`),
  `SLACK_CALLER_DAILY_TIMEZONE` (default `America/Chicago`; `ZoneInfo`, so CST/CDT are automatic).
- Runs in the existing cron tick (`app/alerts_tick.py`, every 5 min) after the caller reminders. It posts once per
  local day: from the configured time until the end of that local day, the first tick claims
  `SlackAlertLog(userId=CALLER_CHANNEL_SENTINEL, kind="caller-daily:YYYY-MM-DD")`; a failed post releases the claim.
- Content: caller rounds with `caller.enabled`, `confirmed`, status not canceled/rejected, and `scheduledAt` in
  [local midnight, next local midnight) converted to UTC; sorted by time. Header
  `:telephone_receiver: *Caller interviews today — {Wed Oct 7}*`, then one line per round:
  `{time} · {company} · {stage} · {profile} · caller {callerName} · {method} — <@owner> <@coworkers…> <@manager> · <thread link>`.
  Empty day: `:telephone_receiver: No confirmed caller interviews today ({Wed Oct 7}).`
- The thread link comes from `chat.getPermalink`; if it fails, the line has no link.

## 6. App UI (`engineers_app`)

- **Profile → Slack card:** a "Slack member ID" text field (hint: Slack profile → ⋮ → Copy member ID), Save and
  Clear; the Connect / Disconnect buttons and OAuth copy go. Timezone, digest and test DM stay. The text that promises
  instant create/reschedule DMs is corrected.
- **Users board:** an "Interview manager" column (name or "—") and a picker in the edit dialog (active users other
  than the one being edited, from `GET /users/lookup`).
- **Interviews:** a **Confirmed** switch in the round form when "Request a caller" is on (owner/admin only); a quick
  Confirmed toggle in the interview panel for the latest caller round; a Confirmed / Not confirmed badge on caller
  rounds in the panel and the list. Types: `confirmed` on rounds and interviews.

## 7. Edge cases

- Owner without a manager: post and replies say "no interview manager assigned" and still tag the owner.
- Manager or owner without a Slack ID: their name, no ping.
- `SLACK_CALLER_CHANNEL_ID` unset: no #caller posts (as today); confirmations still save.
- An interview whose first caller round comes later (round 2): that round creates the thread.
- Turning off "Request a caller" on the latest round: main post shows "No caller needed for {stage}"; no daily-list
  entry.

## 8. Testing

- Backend unit tests (mongomock + a fake Slack client, no real Slack or DB): thread creation and adoption of an old
  round post; each reply in the table; `confirmed` reset on reschedule; the 400 for confirming a non-caller round;
  manager assignment rules (admin only, not self, active); Slack ID validation and uniqueness; the daily list's day
  bounds (including the Nov 1 DST day), once-per-day claim, catch-up after a missed tick, empty-day post.
- Frontend: `tsc -b` and a Vite build; a browser check in the sandbox (profile Slack ID, Users manager picker,
  Confirmed switch/toggle and badges).
- Docs: `docs/slack-interview-bot.md` (typed member ID instead of OAuth, #caller thread behaviour, the daily list and
  its settings) and `.env.example`.
