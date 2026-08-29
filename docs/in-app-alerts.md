# In-app alerts (card renewals, weekly plan, daily bid pace)

Inbox rows live in Mongo (`notifications`). Nothing here posts to Slack.

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
| Cron Schedule | `*/15 * * * *` (UTC; Railway minimum is 5 minutes) |
| Restart policy | `NEVER` |
| Variables | Same `MONGODB_URI`, `APP_TIMEZONE` as the API |

The process connects, upserts due inbox rows, closes Mongo, and exits 0.

Timezone for “today” / Monday 00:00 uses the user’s Slack timezone when set, otherwise `APP_TIMEZONE`.
