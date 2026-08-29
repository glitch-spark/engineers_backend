# Slack interview bot (private daily digests)

Per-user morning digests via a Slack App bot (date-only interviews — no T-30).
Interview details are never posted to a shared channel. Each digest includes a
daily “Know your why” line.

## 1. Create the Slack App

1. Go to https://api.slack.com/apps → **Create New App** → Blank app.
2. **OAuth & Permissions**
   - Bot Token Scopes: `chat:write`, `im:write`
   - User Token Scopes (under User Token Scopes): `identity.basic`
3. **Redirect URLs**: add  
   `https://<your-api-host>/integrations/slack/oauth-callback`  
   (local: `http://localhost:8080/integrations/slack/oauth-callback`)
4. **Install to Workspace** → copy **Bot User OAuth Token** (`xoxb-…`).
5. From **Basic Information** copy **Client ID** and **Client Secret**.
6. Keep the app **unpublished** (not in the App Directory).

## 2. Backend env

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_CLIENT_ID=...
SLACK_CLIENT_SECRET=...
SLACK_OAUTH_REDIRECT_URI=http://localhost:8080/integrations/slack/oauth-callback
SLACK_BOT_CRON_SECRET=generate-a-long-random-string
SLACK_DIGEST_HOUR=8
SLACK_DIGEST_MINUTE=0
APP_TIMEZONE=America/New_York
```

`APP_TIMEZONE` is the default when a user has not set a Slack timezone in Profile.

## 3. Connect as a user

1. Log into Engineers → **Profile**.
2. Click **Connect Slack** → approve in Slack.
3. Set **timezone** and **digest time**, save, then **Send test DM**.
4. If test DM fails, open a DM with the bot in Slack once and retry.

Only users who connect receive DMs. Unconnected teammates get nothing.

## 4. Cron

Hit the tick endpoint every 1–5 minutes (Render Cron, GitHub Actions, or crontab):

```bash
curl -X POST https://<api>/internal/slack-bot/tick \
  -H "X-Cron-Secret: $SLACK_BOT_CRON_SECRET"
```

The job sends a daily digest (when in the user’s digest window) listing that
user’s interviews for the calendar day in their timezone (`createdBy` only),
plus today’s Know your why quote. Each interview line shows company · position,
the profile’s country flag/code · region · profile name, then date · stage:

```
• Acme · Senior Backend
  🇧🇷 BR · Latam · AI Full-stack
  Sat Aug 29 · ai_interview
```

## 5. Privacy

- Queries always filter `createdBy = linked Engineers user`.
- Messages go only to that user's Slack DM.
- Existing `SLACK_WEBHOOK_URL` channel pings are separate and unused for digests.
