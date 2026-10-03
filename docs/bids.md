# Bidder bid reports (Slack channel)

Users manage bidders on the Bidders page. Each bidder points at a screenshot
folder in S3. The 5-minute cron counts the screenshots in each folder and posts
a **daily** bid count and a **weekly** count + pay report to one shared Slack
channel. A bid is one `.png`, `.jpg`, `.jpeg` or `.webp` object (any case, subfolders
included) whose `LastModified` falls in the report window `[start, end)`.

## 1. Backend env

```env
BID_STORAGE_PROVIDER=s3
# Optional; blank falls back to AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION.
BID_STORAGE_ACCESS_KEY_ID=
BID_STORAGE_SECRET_ACCESS_KEY=
BID_STORAGE_REGION=
BID_REPORT_TIMEZONE=America/Chicago
# HH:MM, 24h, in BID_REPORT_TIMEZONE.
BID_DAILY_REPORT_TIME=18:30
# monday ... sunday
BID_WEEKLY_REPORT_DAY=saturday
BID_WEEKLY_REPORT_TIME=16:00
# Unset = reports are saved but not posted.
SLACK_BIDS_CHANNEL_ID=C0123456789
# Same bot as the interview bot.
SLACK_BOT_TOKEN=xoxb-...
# Only needed for the HTTP fallback.
SLACK_BOT_CRON_SECRET=...
# The SPA's origin; the report's "View in app" link points at <FRONTEND_ORIGIN>/bidders.
FRONTEND_ORIGIN=https://app.example.com
```

Keep comments on their own lines: a `KEY=   # note` line sets KEY to the comment text.
Invalid times, weekday names or timezones fail at startup. Copy the same keys
onto the cron service (`python -m app.alerts_tick`), including `FRONTEND_ORIGIN`:
it defaults to `http://localhost:5173`, so without it the "View in app" link in
Slack points at localhost.

## 2. Slack

The bot needs `chat:write` (already granted for the interview bot). **Invite the
bot to the bids channel** (`/invite @<bot>`) or posts fail with `not_in_channel`.
The report is one channel post per run; a long report continues as thread
replies.

## 3. S3 permissions

Use a read-only identity. It only lists keys, it never reads objects:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "s3:ListBucket",
    "Resource": "arn:aws:s3:::<bucket>",
    "Condition": {"StringLike": {"s3:prefix": ["<bidder-prefix-1>/*", "<bidder-prefix-2>/*"]}}
  }]
}
```

List the bidders' prefixes, or `*` if one bucket holds only screenshots. A folder
that cannot be read does not stop the report: that bidder shows a warning in Slack
and on the page, and the rest are reported normally.

## 4. Cron and catch-up

`python -m app.alerts_tick` (schedule `*/5 * * * *`) runs the bid reports as its
third step, after the Slack digest and in-app alerts. A failure there does not
block the other steps.

- A report runs when the latest cutoff is less than **6 hours** old. A cron run missed
  at the cutoff is picked up by the next tick; older periods are skipped, so the first
  deploy never posts stale reports.
- Each period is reported once. A run first claims `bids-daily:<date>` / `bids-weekly:<date>`
  in `SlackAlertLog` (an overlapping cron + HTTP run that cannot claim it skips the period),
  then saves rows (unique on kind, period, bidder) and posts. The claim is released if
  anything fails before the post succeeds.
- If the Slack post fails, the saved rows are kept and the post is retried on the next
  tick (counts are not re-fetched).
- If Slack is not configured, rows are saved and the claim is released, so the report posts
  once the channel and token are set (while still inside the 6 hour window).
- No active bidders: the period is marked done without posting.
- Windows are built from local wall-clock times, so a day crossing a DST change is
  23 or 25 hours long.

HTTP fallback (GitHub Actions or crontab):

```bash
curl -X POST -H "x-cron-secret: $SLACK_BOT_CRON_SECRET" $APP_BASE_URL/internal/bids/tick
```

The response lists the status per kind: `not-due`, `done`, `no-bidders`, `posted`,
`saved:slack-not-configured` or `failed:post`.
