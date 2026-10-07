# Bidder bid reports (Slack channel)

Users manage bidders on the Bidders page. Each bidder points at the screenshot
folder the Bid Track browser extension uploads to (Backblaze B2 or AWS S3). The
5-minute cron counts each folder's bids and posts a **daily** bid count and a
**weekly** count + pay report to one shared Slack channel.

## What counts as a bid

The extension uploads one screenshot per step of an application, under
`<member>/<YYYY-MM-DD>/<profile>/<job>/step<n>_<trigger>_<stamp>.png`. A bidder's
folder is their member prefix, e.g. `s3://bid-screenshots/puma/`.

**Folders are automatic.** Creating a bidder gives them their own folder (a slug of
the name, `-2`, `-3`… on a clash) and sets its URL; once the bidder registers in the
extension, the backend signs every upload into that folder (section 5). Legacy
bidders get a folder from their old URL at startup.

- **One bid = one job folder** (the folder that holds the screenshots). step1,
  step2, next and submit screenshots of the same job count once.
- The job counts in the report window `[start, end)` that holds its **earliest**
  screenshot, so a job whose screenshots straddle the 6:30 PM cutoff counts once,
  on the day it started.
- Only `.png`, `.jpg`, `.jpeg` and `.webp` (any case) count. Files whose name starts
  with `_` (e.g. old connection-test uploads) are ignored.
- A job continued after the bidder's local midnight lands in a new date folder and
  counts twice; the extension decides that path.
- The profile is part of the path, so renaming a profile, or changing a bidder's
  profile while a job is in progress, splits that job into two folders: it counts twice.

Accepted folder URLs: `s3://<bucket>/<folder>/`, `https://<bucket>.s3.<region>.amazonaws.com/<folder>/`,
`https://s3.<region>.amazonaws.com/<bucket>/<folder>/`,
`https://s3.us-east-005.backblazeb2.com/<bucket>/<folder>/`,
`https://<bucket>.s3.us-east-005.backblazeb2.com/<folder>/` and the Backblaze console's
`https://f005.backblazeb2.com/file/<bucket>/<folder>/`. The URL only names the bucket
and folder; which service is read comes from `BID_STORAGE_ENDPOINT_URL`.

## 1. Backend env

```env
BID_STORAGE_PROVIDER=s3
# AWS: optional; blank falls back to AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION.
# Backblaze: required, no AWS fallback. The application key ID (005...) and application key (K005...).
BID_STORAGE_ACCESS_KEY_ID=005...
BID_STORAGE_SECRET_ACCESS_KEY=K005...
# Backblaze: optional, read from the endpoint (us-east-005).
BID_STORAGE_REGION=us-east-005
# Blank = AWS S3. Backblaze: https://s3.us-east-005.backblazeb2.com (https:// is added if missing)
BID_STORAGE_ENDPOINT_URL=https://s3.us-east-005.backblazeb2.com
# Bucket that bidder screenshot folders live in (new bidders get s3://<bucket>/<folder>/).
# Never change it once bidders exist (see section 5).
BID_STORAGE_BUCKET=bid-screenshots
BID_REPORT_TIMEZONE=America/Chicago
# HH:MM, 24h, in BID_REPORT_TIMEZONE.
BID_DAILY_REPORT_TIME=18:30
# monday ... sunday
BID_WEEKLY_REPORT_DAY=saturday
BID_WEEKLY_REPORT_TIME=16:00
# Optional cut-over day, YYYY-MM-DD in BID_REPORT_TIMEZONE: report windows starting before that day's
# local midnight (or straddling it) keep counting screenshot folders. Blank = approved bid records
# everywhere (section 6). Set it on the API service and on the report cron service.
BID_RECORDS_START=
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

## 3. Storage permissions

The backend lists keys to count bids and signs the Bid Track extension's screenshot
uploads (section 5), so its key needs write access as well as listing, on the whole
bucket. Bidders hold no storage keys of their own.

**Backblaze B2:** create an application key for the backend on the `bid-screenshots`
bucket with `listBuckets`, `listFiles` and `writeFiles`. Put its key ID and key in
`BID_STORAGE_ACCESS_KEY_ID` / `BID_STORAGE_SECRET_ACCESS_KEY` (both are required when
`BID_STORAGE_ENDPOINT_URL` is set; AWS_* keys are never used for Backblaze).

**AWS S3:** use an identity that can list and write the bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::<bucket>"},
    {"Effect": "Allow", "Action": "s3:PutObject", "Resource": "arn:aws:s3:::<bucket>/*"}
  ]
}
```

A folder that cannot be read does not stop the report: that bidder shows a warning in Slack
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

## 5. Bidder extension

Bidders sign in to the Bid Track Chrome extension; the backend builds each screenshot's
object path and returns a short-lived signed upload link, so bidders hold no storage keys.

- **Invites.** Creating a bidder returns a one-time invite code (`<TAG>-XXXX-XXXX`, valid
  7 days, single use). The bidder enters it with a username and password in the extension.
- **Statuses.** `invited` (code issued, not registered), `active` (registered),
  `archived` (cannot log in; its folder is kept).
- **Reset login** invalidates the bidder's tokens and password and issues a new invite code;
  archiving also invalidates tokens. **New invite code** is for bidders not yet registered.
- **Backend key.** The backend's application key needs `listBuckets`, `listFiles` and
  `writeFiles` on the whole bucket (section 3). Signing happens locally, so a key without
  `writeFiles` is not detected when the link is signed: every bidder PUT then fails with 403.
  `BID_STORAGE_BUCKET` must be set, otherwise uploads answer 503.
- **Never change `BID_STORAGE_BUCKET`** once bidders exist: registered bidders are counted
  from `s3://<old bucket>/<folder>/` while their uploads would be signed for the new bucket.
- **Routes** (`/ext`): `POST /ext/register` (invite code, username, password), `POST /ext/login`,
  `GET /ext/me`, `POST /ext/password` (`currentPassword`, `newPassword`; logs out the bidder's other
  browsers and returns a fresh `token`; a wrong current password counts toward the login rate limit), and
  `POST /ext/upload-url` (`job`, `step`, `trigger`, `capturedAt`, optional `format`: `png` (default),
  `webp` or `jpeg`) which returns `url`, `key`, `expiresAt` and `headers`; PUT the image to `url` within
  5 minutes with the returned `Content-Type`. Registering switches counting to the bidder's folder
  (`s3://<bucket>/<folder>/`).
- **Errors** are `{"error": "<message>"}` with the status. A username or password that breaks
  the rules answers 400 with `Username must be 3–40 characters: lowercase letters, numbers, dots,
  underscores or dashes`, `Password must be at least 8 characters` or `Password is too long`
  (over 72 bytes); any other malformed body answers 400 `Invalid request`.
- **Tokens** last 30 days. `/ext/me` and `/ext/upload-url` add a fresh `token` to the response
  once the presented one is over 7 days old, and the extension stores it, so a bidder who
  keeps using the extension is never logged out by expiry; a token left unused for 30 days
  expires and the bidder logs in again.
- **Rate limit.** 10 failed logins or registrations (rule violations included) per username
  and per client IP in 15 minutes answer 429. The client IP is the first `X-Forwarded-For`
  entry. Verify on Railway whether its proxy overwrites or appends `X-Forwarded-For`; if it
  appends, switch `client_ip` (`app/bids/rate_limit.py`) to the right-most entry.

## 6. Rollout

1. Set `BID_STORAGE_*` (section 1) and the key from section 3 on the API **and** cron
   services. Deploy the backend, then the app. Existing bidders show "Invited · no code yet";
   counting continues from their old folders.
2. Run `python -m app.bids.folders --report` (read-only, with the production env). It lists
   every active bidder whose folder URL will change when they register (name, folder,
   current URL). Registering switches counting to the new folder for the whole week in
   progress, so register those bidders right after a weekly cutoff, or adjust them by hand.
3. Issue invite codes and send each bidder the extension zip (version **3.1.0** only: a 3.0.0 build
   does not confirm uploads, so its bids' screenshots would show as missing) with the code. Tell them:
   Remove any older "Bid Track Local" extension first (or unzip this version over its folder
   and click Reload). Two installs side by side double-count or lose bids.
4. Revoke each old shared upload key (`puma-uploader`, …) as soon as that prefix's bidders
   have registered, so only the backend can write there.
5. Reports count approved bid records (the database) by default. To keep windows from before
   rollout on the old screenshot-folder count, set `BID_RECORDS_START=<YYYY-MM-DD>` (in
   `BID_REPORT_TIMEZONE`) on the API service **and** the report cron service (`python -m
   app.alerts_tick`, a separate Railway service). A window counts records when it **starts** on or
   after that day's local midnight; a window that starts earlier, including one that straddles
   it, keeps counting screenshot folders. Unset, every window counts records. Only **approved** bids count toward `count` and pay (`count x rate`);
   pending and rejected bids are reported beside it, e.g. `(pending 1, rejected 1)` in Slack
   and `pending` / `rejected` in the app's report rows (`null` on folder-counted rows). The
   live counts judge each open window by its own start in the same way, so for a while after
   rollout "today" can come from records and "this week" from folders. Bids are counted on
   the day of their first screenshot.
