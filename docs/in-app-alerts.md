# In-app alerts (card renewals, weekly plan, daily bid pace)

Inbox rows live in Mongo (`notifications`). The HTTP tick below does not post to Slack.
The Railway sibling (`python -m app.alerts_tick`) also runs the Slack interview digest first.

## Local / HTTP tick

```bash
curl -X POST http://localhost:8080/internal/alerts/tick \
  -H "X-Cron-Secret: $SLACK_BOT_CRON_SECRET"
```

Uses the same `SLACK_BOT_CRON_SECRET` as the Slack digest tick.

## Railway cron

Do **not** put a cron schedule on the always-on API service (`uvicorn` never exits; Railway would skip later runs).

Add a **sibling service** in the same project (`alerts-cron`), same repo and Dockerfile:

| Setting | Value |
|---|---|
| Custom start command | `python -m app.alerts_tick` |
| Cron Schedule | `*/5 * * * *` (UTC; Railway minimum is 5 minutes) |
| Restart policy | `NEVER` |
| Variables | Same `MONGODB_URI`, `AUTH_SECRET`, `APP_TIMEZONE`, `SLACK_BOT_TOKEN`, `FRONTEND_ORIGIN` as the API |

The process runs Slack digest first (5-minute window), then upserts due inbox rows, closes Mongo, and exits. Slack DMs still require `SLACK_BOT_TOKEN`; without it that step is a no-op.

Timezone for “today” / Monday 00:00 uses the user’s Slack timezone when set, otherwise `APP_TIMEZONE`.
