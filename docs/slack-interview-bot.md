# Slack interview bot

One Slack App bot (`SLACK_BOT_TOKEN`) sends every Slack message the app posts, in three categories:

| Category | Messages | Where |
|---|---|---|
| **Reminders** | 30-minute reminder, daily caller list | #caller thread, `SLACK_CALLER_DAILY_CHANNEL_ID` |
| **Action items** | #caller thread (main post and replies), Daily Plan | `SLACK_CALLER_CHANNEL_ID`, `SLACK_DAILY_PLAN_CHANNEL_ID` |
| **Transactions** | New transaction with Approve / Reject buttons | `SLACK_TRANSACTIONS_CHANNEL_ID` |

Shared format: plain Slack text. The first line is an emoji, the owner's mention when the message has an owner, a
bold title, then key facts joined by ` · `. Detail lines are `*Label:* value`. The last line is `cc …` when someone is
cc'd. Times are Slack date tags, so each reader sees their own time zone. A mention pings the person's Slack member
ID, or shows their name when they have none. Text typed in the app (company, recruiter, caller, profile and
transaction text) is escaped, so it cannot ping a channel or add a Slack token. Only a well-formed http(s) URL
becomes a link; a URL with `<`, `>`, `|` or spaces is shown as plain text.

## 1. Create the Slack App

1. Go to https://api.slack.com/apps → **Create New App** → Blank app.
2. In the app's permissions page, add Bot Token Scopes: `chat:write`, `im:write`.
3. **Install to Workspace** → copy the bot token (`xoxb-…`, shown after installing).
4. Invite the bot (`/invite @<bot>`) to #caller, the daily-list channel, the daily-plan channel and the
   transactions channel; they may be private.
5. **Interactivity & Shortcuts** → turn on → Request URL
   `https://engineersbackend-production-902c.up.railway.app/integrations/slack/interactions` → Save. Without it the
   Approve / Reject buttons do nothing.
6. **Basic Information** → copy the **Signing Secret** into `SLACK_SIGNING_SECRET` (below).
7. Keep the app **unpublished** (not in the App Directory).

## 2. Backend env

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_BOT_CRON_SECRET=generate-a-long-random-string
SLACK_CALLER_CHANNEL_ID=C0123456789          # #caller: one thread per caller interview
SLACK_CALLER_DAILY_CHANNEL_ID=C0987654321    # daily caller list; must be set, unset = no daily list
SLACK_CALLER_DAILY_TIME=05:00                # HH:MM in SLACK_CALLER_DAILY_TIMEZONE
SLACK_CALLER_DAILY_TIMEZONE=America/Chicago  # CST/CDT handled automatically
SLACK_DAILY_PLAN_CHANNEL_ID=C0246813579      # daily plans, edited in place
SLACK_TRANSACTIONS_CHANNEL_ID=C0135792468    # transactions with Approve / Reject; unset = no transaction posts
SLACK_SIGNING_SECRET=...                     # verifies button clicks; unset = posts without buttons
SLACK_DIGEST_HOUR=8
SLACK_DIGEST_MINUTE=0
APP_TIMEZONE=America/New_York
```

`APP_TIMEZONE` is the default when a user has not set a Slack timezone in Profile. Everything is optional: with a
setting unset, that message is skipped. Slack failures are logged and never block saving.

## 3. People

- **Slack member ID** — each user types theirs in **Profile → Slack** (Slack: your profile → ⋮ → **Copy member
  ID**, e.g. `U04ABC12345`). It is used to @-mention them (and by **Send test DM**). One ID belongs to one
  user. Without one, messages show their name and they get no ping. There are no per-user digest DMs.
- **Owner** — the user who created the interview (or the transaction). Their mention leads the message.
- **Interview manager** — a user role; admins set it on **Users** (edit a user → Role → Interview manager). Every
  active interview manager is cc'd on all #caller posts, replies, reminders and daily lists, and can open any
  interview (read-only) to coordinate it. With none, the `cc` line is left out.
- **Admin** — the only role that can approve or reject a transaction from Slack (see section 7).

## 4. #caller threads (action items)

The first round with **Request a caller** on posts the interview's main post; later changes edit it to describe the
**latest round**:

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

- Once the owner confirms the round, the `Not confirmed yet` line reads `:white_check_mark: Confirmed`.
- Canceled or rejected: `:x: @owner — *Canceled* ~Caller needed~`, the time struck through, no confirmation line.
- Latest round without a caller: `:telephone_receiver: @owner — *No caller needed* for Tech Round 1`, then Profile,
  Company name, Stage and Interview time.
- Interviews from before the single thread keep their older per-round posts; their latest one becomes the thread.

Every change also adds one reply, owner first, ending with `cc @im1 @im2`:

| Change | Reply |
|---|---|
| Next round | `:arrow_right: @owner — *Moved to {stage}* — {time} · needs confirmation` (or `· no caller needed`) |
| Confirmed in the app | `:white_check_mark: @owner — *Confirmed* — {stage} · {time} · caller {name}` |
| Confirmed unticked | `:warning: @owner — *Confirmation removed* — {stage} · {time}` |
| New time | `:repeat: @owner — *Rescheduled* — {old} → {new} · needs confirmation` (Confirmed is cleared) |
| Canceled / rejected | `:no_entry: @owner — *Canceled* — {stage} · {time}` (*Rejected* likewise) |
| Back from canceled | `:white_check_mark: @owner — *Back on* — {stage} · {time} · needs confirmation` |
| Other caller details | `:pencil2: @owner — *Caller details updated*`, then `Field: old → new` lines |
| Round deleted | `:wastebasket: @owner — *Round removed* — {stage}` |

The manager arranges the caller and comments in the thread; the owner (or an admin) then ticks **Confirmed** on
the round in the app. The bot does not read thread replies. Deleting the interview deletes its main post.

**Daily Plan** (`SLACK_DAILY_PLAN_CHANNEL_ID`) is edited in place as the plan changes. Its first line is
`:spiral_note_pad: @owner's *Daily Plan*`.

## 5. Reminders

**30-minute reminder** — a reply in the interview's #caller thread, ~30 min before each caller round starts:

```
:alarm_clock: *Starting in 28 min* — 10:30 AM · Intro · Acme
*Profile:* Ian Meron
*Method:* Video meeting — https://meet…
*Caller assigned:* Sam
cc @owner @im1 @im2
```

**Daily caller list** — at 05:00 America/Chicago in `SLACK_CALLER_DAILY_CHANNEL_ID` (e.g. #engineer-caller-test),
the day's **caller interviews only** (rounds with a caller requested), by start time, confirmed or not. Rounds
without a caller, and canceled/rejected/completed rounds, are not listed. Each line tags the round's owner;
unconfirmed rounds are flagged; the interview managers are cc'd once at the end. **`SLACK_CALLER_DAILY_CHANNEL_ID`
must be set**, or the list is never posted. It is posted once per day, catching up later that day if a tick was
missed. This list replaces the old personal digest DMs.

```
:alarm_clock: *Caller interviews today* — Wed Oct 7
• 10:30 AM · Acme · Intro · Ian Meron · caller Sam · Video meeting — @owner · <thread>
• 11:00 AM · Initech · Tech Round 1 · Ana P · caller TBD · Video meeting · :hourglass_flowing_sand: not confirmed — @owner2 · <thread>
cc @im1 @im2
```

With no rows: `:alarm_clock: *Caller interviews today* — Wed Oct 7 · none today.`

**Test DM** — Profile → Slack → **Send test DM** checks the member ID. If it fails, open a DM with the bot in Slack
once and retry. The Profile timezone still sets the user's local day for in-app alerts and daily caps.

## 6. Cron

Railway sibling service: start command `python -m app.alerts_tick`, schedule `*/5 * * * *`. Each tick runs the
30-minute reminders and the daily caller list, then in-app alerts and bid reports. Copy
`SLACK_BOT_TOKEN`, the `SLACK_CALLER_*` settings and `FRONTEND_ORIGIN` onto that service. Do not cron the uvicorn
API.

HTTP fallback every 1–5 minutes:

```bash
curl -X POST https://<api>/internal/slack-bot/tick \
  -H "X-Cron-Secret: $SLACK_BOT_CRON_SECRET"
```

## 7. Transactions

Each new transaction is posted to `SLACK_TRANSACTIONS_CHANNEL_ID` with buttons (blocks, the one place the bot uses
them):

```
:money_with_wings: @owner — *New transaction* · $120.00
*Payer:* @payer
*Date:* Oct 7
*Method:* Card ••1234 (Ops card)      ← or "Coin"
*Billing:* Monthly                    ← or "One-time"
*Description:* …
[Approve] [Reject]
```

Lines without a value (Description, Notes) are left out. Editing a pending transaction edits the post; deleting the
transaction deletes it.

- **Who can decide** — in Slack only an **admin**, matched by Slack member ID: the clicker must be an active admin
  whose member ID is saved in Profile. Anyone else gets a private (ephemeral) "Only admins can approve or reject
  transactions in Slack." In the app the payer can decide too.
- **After a decision** — the buttons are replaced by `:white_check_mark: *Approved* by @admin` (or
  `:no_entry: *Rejected* by @admin`) and a thread reply says `:white_check_mark: @owner — *Approved* by @admin ·
  $120.00`. A transaction with no stored post (created before this feature, or Slack was off) gets that line as a
  standalone channel message instead.
- **Already decided or deleted** — the clicker gets a private "Already approved by Name." (or rejected) or "This
  transaction no longer exists."
- **Requirements** — the Interactivity Request URL from section 1 and `SLACK_SIGNING_SECRET`. Every click is
  verified with the signing secret; an unsigned or stale request is rejected (401) and a missing secret answers 503.
