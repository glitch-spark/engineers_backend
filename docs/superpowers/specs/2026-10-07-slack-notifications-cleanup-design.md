# Slack notifications cleanup: one style, transactions with Approve/Reject, no coworkers

Date: 2026-10-07 · Branch: `feat/cleanup` (backend and frontend)

## Goal

Every Slack message the app sends follows one of three layouts (reminders, action items, transactions).
Transaction notifications work again and admins can approve or reject them from Slack. Coworkers on caller
rounds, the misused application-outcome ping, dead Slack code and leftover OAuth settings are removed.

## Context

- Interview managers are a user role (`interview_manager`); `interview_managers()` in `app/slack_bot.py` returns
  the active ones, by name.
- Transaction pings go through an incoming webhook (`app/slack.py`, `SLACK_WEBHOOK_URL`). In production Slack
  answers `404 no_service`: the webhook was revoked, so nothing is delivered.
- `SLACK_CALLER_DAILY_CHANNEL_ID` is not set in production, so the daily caller list never posts.
  `SLACK_REMINDER_MINUTES` is set in production but read by nothing.

## Decisions

- Transactions post with the existing bot token to a new `SLACK_TRANSACTIONS_CHANNEL_ID` (not a new webhook).
- The application-outcome ping is removed, not replaced.
- Coworkers are removed from the business logic entirely, including the digest's "Calls with you as caller"
  section and coworkers' access to interviews they were on. Stored `coworkerIds` stay in Mongo, unread.
- Only admins can approve or reject in Slack (in the app, the payer still can too).

## 1. Message style

Shared rules:

- Plain mrkdwn text. First line: an emoji, the owner's mention when the message has an owner, a bold title,
  then key facts joined by ` · `. Detail lines are `*Label:* value`. The last line is `cc …` when someone is
  cc'd. No Block Kit headers, dividers or "Posted by your app" footers; transactions alone use blocks, for buttons.
- Times are Slack date tags (`slack_time`), so each reader sees their own time zone.
- Mentions use `_mention` (Slack ID ping, else the name). Interview managers are always all active ones; with
  none, a `cc` that would name only managers is left out, and the #caller main post omits its cc line.

### Reminders

30-minute reminder (thread reply in the interview's #caller thread, as today):

```
:alarm_clock: *Starting in 28 min* — 10:30 AM · Intro · Acme
*Profile:* Ian Meron
*Method:* Video meeting — https://meet…
*Caller assigned:* Sam
cc @owner @im1 @im2
```

Daily caller list (`SLACK_CALLER_DAILY_CHANNEL_ID`, 05:00 America/Chicago, confirmed caller rounds only):

```
:alarm_clock: *Caller interviews today* — Wed Oct 7
• 10:30 AM · Acme · Intro · Ian Meron · caller Sam · Video meeting — @owner · <thread>
cc @im1 @im2
```

No rows: `:alarm_clock: *Caller interviews today* — Wed Oct 7 · none confirmed.`

Personal digest (DM, at each user's digest time) lists the user's own interviews only:

```
:alarm_clock: *Today's interviews* — Wed Oct 7 · 2 interviews
• 10:30 AM · Ian Meron · Acme · Intro
• Time TBD · Maria K · Globex · Final
<https://app/interviews|Open interviews>
_Know your why: …_
```

No interviews: `:alarm_clock: *Today's interviews* — Wed Oct 7 · none today. Have a rest.` The day comes from
the user's time zone; each row's time is the caller start when there is one, else "Time TBD" (as today). The
company links to the job URL when there is one.

### Action items

#caller main post (one per interview, edited to describe the latest round):

```
:telephone_receiver: @owner — *Caller needed*
*Profile:* Ian Meron
*Company name:* Acme
*JD Link:* https://jobs…
*Recruiter name:* Jane 🇺🇸
*Stage:* Intro
*Interview time:* 10:30 AM
*Method:* Video meeting — https://meet…
*Caller assigned:* Sam
:hourglass_flowing_sand: Not confirmed yet
cc @im1 @im2
```

- Canceled/rejected round: `:x: @owner — *Canceled* ~Caller needed~`, time struck through, no confirmation line.
- Latest round without a caller: `:telephone_receiver: @owner — *No caller needed* for Tech Round 1`, then
  Profile, Company name, Stage, Interview time.
- No Coworkers or Interview manager lines.

Thread replies keep today's events and wording, with the owner moved to the front and managers cc'd:

| Event | First line |
|---|---|
| moved | `:arrow_right: @owner — *Moved to Tech Round 1* — {time} · needs confirmation` (or `· no caller needed`) |
| confirmed | `:white_check_mark: @owner — *Confirmed* — Intro · {time} · caller Sam` |
| unconfirmed | `:warning: @owner — *Confirmation removed* — Intro · {time}` |
| rescheduled | `:repeat: @owner — *Rescheduled* — {old} → {new} · needs confirmation` (or `· confirmed, caller Sam`) |
| ended | `:no_entry: @owner — *Canceled* — Intro · {time}` (Rejected likewise) |
| back_on | `:white_check_mark: @owner — *Back on* — Intro · {time} · needs confirmation` |
| removed | `:wastebasket: @owner — *Round removed* — Panel` |
| details | `:pencil2: @owner — *Caller details updated*`, then `Field: old → new` lines |

Every reply ends with `cc @im1 @im2` (left out when there are no interview managers).

Daily plan (`SLACK_DAILY_PLAN_CHANNEL_ID`, edited in place): only the first line changes, to
`:spiral_note_pad: @owner's *Daily Plan*`.

### Transactions

```
:money_with_wings: @owner — *New transaction* · $120.00
*Payer:* @payer
*Date:* Oct 7
*Method:* Card ••1234 (Ops card)      ← or "Coin"
*Billing:* Monthly                    ← or "One-time"
*Description:* …
[Approve] [Reject]
```

- Lines with no value are left out (Description, Notes). A Notes line is shown when set.
- After a decision the buttons are replaced by `:white_check_mark: *Approved* by @admin` or
  `:no_entry: *Rejected* by @admin`, and a thread reply says
  `:white_check_mark: @owner — *Approved* by @admin · $120.00` (Rejected likewise).
- A transaction with no stored post (created before this change, or Slack was off) gets that line as a standalone
  channel message instead.

## 2. Transactions in Slack

New module `app/slack_transactions.py`:

- `transaction_text(tx, owner, payer) -> str` and `transaction_blocks(tx, owner, payer, decided_by) -> list` build
  the post (buttons only while pending and when `SLACK_SIGNING_SECRET` is set).
- `sync_transaction_post(tx)`: post to `SLACK_TRANSACTIONS_CHANNEL_ID`, or `chat.update` the stored post; a gone
  post is reposted. Stores `slackChannel` / `slackTs` on the transaction. Never raises.
- `announce_decision(tx, by)`: update the post and reply in its thread, or post standalone when there is none.
- `delete_transaction_post(tx)`.
- `fmt_usd` moves here from `app/slack.py`; `app/bids/slack_format.py` imports it from here.

`Transaction` gains `slackChannel: Optional[str]` and `slackTs: Optional[str]`.

Hooks in `app/routers/transactions.py`:

- Create → `sync_transaction_post`.
- Update of a pending transaction's fields → `sync_transaction_post`.
- Status change → shared `set_transaction_status(tx, status, by)` (sets status, `approvedBy`, `approvedAt`, saves,
  then `announce_decision`). The PUT route keeps its rules (admin or payer) and calls it.
- Delete → `delete_transaction_post`.

### Interactions endpoint

`POST /integrations/slack/interactions` (no app auth; Slack signs it):

- Verify `X-Slack-Signature` = `v0=` HMAC-SHA256 of `v0:{X-Slack-Request-Timestamp}:{raw body}` with
  `SLACK_SIGNING_SECRET`, constant-time compare; timestamp older than 5 minutes → 401. Missing secret → 503.
- Body is form-encoded `payload` JSON. Handled: `block_actions` with `action_id` `tx_approve` or `tx_reject`
  and `value` = transaction id. Anything else → 200, ignored.
- The clicker is the active `admin` whose `slackUserId` equals `payload.user.id`. Otherwise an ephemeral reply via
  `response_url`: "Only admins can approve or reject transactions in Slack."
- Unknown transaction → ephemeral "This transaction no longer exists." Already decided → ephemeral
  "Already approved by Name." (or rejected).
- Otherwise `set_transaction_status(tx, "approved" | "rejected", admin)`; the post update and thread reply show the
  result. Always answers 200 quickly.

## 3. Removals

- **Coworkers** (backend): `InterviewCaller.coworkerIds` and its two indexes in the model list, `CallerBody`
  `coworkerIds` and its validation, the `coworkers` snapshot key and change label, `is_coworker`, the coworker
  clause in `_can_view`, `_populate_coworkers`, the digest's coworker query and "Calls with you as caller"
  section, coworker mentions in Slack. `_can_view` becomes admin, interview manager or owner.
- **Coworkers** (frontend): the Coworkers picker in `CallerFields.tsx`, `callerCoworkerIds` in
  `lib/interviewForm.ts`, `coworkerIds` / `coworkers` in `api/endpoints.ts`. The caller toggle's help text becomes
  "A caller joins at this round's start time. Posts to the #caller channel on Slack."
- **Application outcome ping**: the `notify_new_interview` call in `app/routers/applications.py`.
- **Dead code**: `app/slack.py` (after its callers move), `notify_interview_dm`, `_interview_blocks` and helpers
  only they use.
- **Settings**: `SLACK_WEBHOOK_URL`, `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_OAUTH_REDIRECT_URI` from
  `app/config.py`, `.env.example` and the local `.env`. New: `SLACK_TRANSACTIONS_CHANNEL_ID`, `SLACK_SIGNING_SECRET`.
- Docs: `docs/slack-interview-bot.md` describes the new formats, transactions and the interactivity setup.

## 4. Rollout (manual, by the user)

1. Slack app → Interactivity & Shortcuts: on; Request URL
   `https://engineersbackend-production-902c.up.railway.app/integrations/slack/interactions`. Copy the Signing Secret.
2. Create the transactions channel; invite the bot.
3. Railway: set `SLACK_SIGNING_SECRET`, `SLACK_TRANSACTIONS_CHANNEL_ID`; delete `SLACK_WEBHOOK_URL`,
   `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_OAUTH_REDIRECT_URI`, `SLACK_REMINDER_MINUTES`; optionally set
   `SLACK_CALLER_DAILY_CHANNEL_ID`. Changing variables redeploys `dev` at once, so do it when this ships.

## 5. Error handling

All Slack calls stay best-effort: failures are logged and never fail the user's request. The interactions endpoint
never returns an error to Slack for a handled click (problems go to the clicker as ephemeral text), except 401 for a
bad signature and 503 when the secret is missing.

## 6. Testing

Unit tests (no network, no real DB; mongomock where storage is needed):

- Text builders: #caller main post and variants, every reply, reminder, daily list (with and without managers),
  digest (with rows, empty), daily plan heading, transaction post (pending, decided, card/coin, optional lines).
- `sync_transaction_post` / `announce_decision` with Slack faked: post, update, gone-post repost, standalone decision.
- Signature: valid, wrong secret, stale timestamp, missing secret.
- Interactions: admin approves, admin rejects, non-admin refused, unlinked Slack user refused, already decided,
  unknown transaction, unrelated action ignored.
- `set_transaction_status` from the PUT route still enforces admin-or-payer.
- Coworker removal: `_can_view`, caller body without coworkers, snapshot without coworkers.
- Frontend: `tsc -b` (2 known errors in `InterviewPrepLibrary.tsx`) and `vite build`.
