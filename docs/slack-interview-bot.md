# Slack interview bot

One Slack App bot (`SLACK_BOT_TOKEN`) does four things:

1. **#caller threads** — one thread per caller interview in `SLACK_CALLER_CHANNEL_ID`, tagging the owner's
   interview manager; replies when a round is confirmed, rescheduled, canceled or moved to the next round.
2. **30-minute reminders** — a reply in that thread ~30 min before each caller round starts.
3. **Daily caller list** — at 05:00 America/Chicago, the day's confirmed caller interviews in
   `SLACK_CALLER_DAILY_CHANNEL_ID` (e.g. #engineer-caller-test).
4. **Personal digest** — a private DM with each user's interviews for the day, at their chosen time.

## 1. Create the Slack App

1. Go to https://api.slack.com/apps → **Create New App** → Blank app.
2. **OAuth & Permissions** → Bot Token Scopes: `chat:write`, `im:write`.
3. **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-…`).
4. Invite the bot to #caller and to the daily-list channel (`/invite @<bot>`); both may be private.
5. Keep the app **unpublished** (not in the App Directory).

## 2. Backend env

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_BOT_CRON_SECRET=generate-a-long-random-string
SLACK_CALLER_CHANNEL_ID=C0123456789          # #caller: one thread per caller interview
SLACK_CALLER_DAILY_CHANNEL_ID=C0987654321    # daily list; unset = off
SLACK_CALLER_DAILY_TIME=05:00                # HH:MM in SLACK_CALLER_DAILY_TIMEZONE
SLACK_CALLER_DAILY_TIMEZONE=America/Chicago  # CST/CDT handled automatically
SLACK_DIGEST_HOUR=8
SLACK_DIGEST_MINUTE=0
APP_TIMEZONE=America/New_York
```

`APP_TIMEZONE` is the default when a user has not set a Slack timezone in Profile.

## 3. People

- **Slack member ID** — each user types theirs in **Profile → Slack** (Slack: your profile → ⋮ → **Copy member
  ID**, e.g. `U04ABC12345`). It is used to @-mention them and to DM them. One ID belongs to one user.
  Without one, messages show their name and they get no ping or DM.
- **Interview manager** — admins pick one per user on **Users** (edit a user → Interview manager). The manager is
  tagged in that user's #caller threads and daily-list lines.

## 4. #caller threads

- The first round with **Request a caller** on posts the interview's main post: profile, company, JD link,
  recruiter, stage, time, method, caller, coworkers, **Interview manager**, and **Confirmed / Not confirmed yet**.
  Interviews from before this change keep their older per-round posts; their latest one becomes the thread.
- Every change edits the main post to describe the **latest round** and adds one reply, ending with
  `cc @manager @owner`:

  | Change | Reply |
  |---|---|
  | Next round | `➡️ Moved to {stage} — {time} · needs confirmation` (or `· no caller needed`) |
  | Confirmed in the app | `✅ Confirmed — {stage} · {time} · caller {name}` |
  | Confirmed unticked | `⚠️ Confirmation removed — {stage} · {time}` |
  | New time | `🔁 Rescheduled — {old} → {new} · needs confirmation` (Confirmed is cleared) |
  | Canceled / rejected | `⛔ Canceled — {stage} · {time}` |
  | Back from canceled | `✅ Back on — {stage} · {time} · needs confirmation` |
  | Other caller details | `✏️ Caller details updated` with `Field: old → new` lines |
  | Round deleted | `🗑️ Round removed — {stage}` |

- The manager arranges the caller and comments in the thread; the owner (or an admin) then ticks **Confirmed** on
  the round in the app. The bot does not read thread replies.
- Deleting the interview deletes its main post. Slack failures are logged and never block saving.

## 5. Cron

Railway sibling service: start command `python -m app.alerts_tick`, schedule `*/5 * * * *`. Each tick runs the
30-minute reminders, the daily caller list (posted once per day, catching up later that day if a tick was
missed), then personal digests, then in-app alerts and bid reports. Copy `SLACK_BOT_TOKEN`, the `SLACK_CALLER_*`
settings and `FRONTEND_ORIGIN` onto that service. Do not cron the uvicorn API.

HTTP fallback every 1–5 minutes:

```bash
curl -X POST https://<api>/internal/slack-bot/tick \
  -H "X-Cron-Secret: $SLACK_BOT_CRON_SECRET"
```

## 6. Personal digest

Users with a member ID and alerts on get a DM at their digest time (Profile → Slack: timezone, time, **Send test
DM**) listing their own interviews (`createdBy`) for the day, plus a “Know your why” line. If the test DM fails,
open a DM with the bot in Slack once and retry.
