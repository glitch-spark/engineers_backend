# Caller teams in a separate Slack workspace — design

Date: 2026-11-23. Repos: engineers_backend (most of the work), engineers_app (admin screen).

## Why

Some interview rounds are handled by an outside group of callers (today "Jamil's group" is one entry in the caller
list). They should coordinate in their own Slack workspace, away from the main one: every round assigned to one of
them is announced there with the files they need, the team's manager is tagged every time, and they get a morning
list and 30-minute reminders. Interview managers sit in the same reminder channel. Today there is one such team;
more will follow, so a team is data an admin adds, not code.

## Goals

- A **caller team**: a name, members (external callers), a manager (an external caller), a team channel and a
  reminder channel in the callers' Slack workspace.
- Each external caller has a **Slack member ID** (for the callers' workspace) that admins and interview managers can
  edit.
- When an interview manager assigns a round to a team member, the **team channel** gets an "Interview scheduled" post
  tagging the assigned caller and the team manager, with the **brief PDF and the resume** uploaded into its thread.
- The **reminder channel** gets, for the team's rounds only:
  - a daily list at **06:00 America/Chicago**, edited in place as the day changes;
  - a reminder about **30 minutes before** each round, tagging the caller and the team manager.
- The main workspace is unchanged: #caller and the daily channel keep every post they get today, team rounds
  included.

## Non-goals

- No app access for external callers or the team manager. Nothing in the callers' workspace has buttons; assigning,
  confirming and follow-ups stay in the main workspace's #caller.
- No tagging of interview managers in the callers' workspace (they read the reminder channel; their Slack IDs there
  are not stored). Add later if wanted.
- Individual (non-team) callers: no change.
- Platform-user callers cannot be team members.

## Anti-goals

- A team post that leaks into the main workspace, or a main-workspace post that goes to the callers' workspace.
- A Slack failure in the callers' workspace that breaks or slows an interview save or the main workspace's posts.
- A team member assigned to a round and never told (the post must be retried by the 5-minute tick).

## Constraints

- One callers' workspace for all teams: a second Slack app install, its bot token in env
  `SLACK_CALLERS_BOT_TOKEN` (never in the database). Unset = team posting is off; everything else works.
- Bot scopes in that workspace: `chat:write`, `files:write`. The bot must be invited to each team channel.
- The cron tick (`python -m app.alerts_tick`, every 5 minutes, `slack_bot.run_alert_tick`) drives the daily list and
  the reminders, as it does for the main workspace.
- Uploaded files stay in the callers' workspace until someone deletes them there; the app cannot take them back.
  The brief and resume contain the profile's details and the job; that is accepted.
- No migration: new fields default to empty; a new collection for teams and new collections for the team posts.
  The existing `InterviewDayPost` unique index on `day` is not touched.

## Approaches

- **A. Teams as data, reusing the existing posting code (chosen).** A `CallerTeam` collection and an admin screen;
  the existing #caller/day-post/reminder code gains a workspace switch and a round filter and runs once more per
  team. A second team is added on the screen.
- **B. One team in env config.** Member ids, manager, channels in env vars. A couple of days cheaper, but a second
  team means rewriting into A, and membership changes need a redeploy. Rejected: more teams are coming.
- **C. Slack-side forwarding / workflows.** Cannot filter the team's rounds, tag the right caller or attach files.
  Rejected.

## Design

### Data

- `Caller` (app/models/caller.py) gains:
  - `slackUserId: Optional[str]` — member ID in the callers' workspace (format `U…`/`W…`, validated like
    `UserExtra.slackUserId`).
  - `teamId: Optional[str]` — the team it belongs to; one team per caller.
- New `CallerTeam` (collection `callerteams`):
  - `name`, `nameKey` (unique, lowercased), `managerCallerId` (an external `Caller`, required; need not be a
    member), `channelId`, `reminderChannelId` (Slack channel IDs in the callers' workspace),
    `dailyTime: str = "06:00"`, `timezone: str = "America/Chicago"`, `archivedAt`.
  - Archiving a team stops all its posting; members keep `teamId` but are treated as individual callers.
- A **team round**: a caller round (`caller.enabled`, `callerKind == "external"`, `callerId` set) whose caller's
  `teamId` points at a non-archived team. Membership is read at post time.
- New `TeamRoundPost` (collection `teamroundposts`): `teamId`, `interviewId`, `roundId`, `channel`, `ts` (the
  scheduled post), `callerId` (who was tagged), `startEpoch` (start the post and brief were for), `filesFor`
  (startEpoch the files were uploaded for). Unique on (`teamId`, `roundId`).
- New `TeamDayPost` (collection `teamdayposts`): `teamId`, `day`, `channel`, `ts: list[str]`, `interviewIds`.
  Unique on (`teamId`, `day`).
- Reminder dedup: `SlackAlertLog` with key `team-reminder:<teamId>:<roundId>:<startEpoch>` (same sentinel user as
  the existing caller-channel keys).

### Slack client

- `slack_bot._slack_api(method, body, *, workspace="main")`: `"callers"` uses `SLACK_CALLERS_BOT_TOKEN`. All existing
  calls keep the default.
- File upload helper `upload_files(channel, thread_ts, files: list[(filename, bytes)], comment)`, callers'
  workspace: `files.getUploadURLExternal` per file, PUT the bytes, then one `files.completeUploadExternal` with
  `channel_id` and `thread_ts`.
- Mentions use `<@{Caller.slackUserId}>`; a caller without one is shown by name (and logged once per round).

### Team channel: "Interview scheduled"

Hooked in the same places the #caller thread is synced: `routers/interviews._sync_caller_posts` and the Slack
"Choose caller" path in `app/coordination/actions.set_round`. Runs in the background like `_refresh_day_post`
(the save never waits on Slack; failures are logged with interview id, round id and team id).

For each round of the interview, compare its team-round state with its `TeamRoundPost`:

| Change | What happens |
|---|---|
| Round becomes a team round (no post yet) | Post "Interview scheduled" top-level, tagging caller + manager; upload brief + resume in its thread. |
| Same team, another member assigned | Edit the post; thread reply tagging the new caller + manager ("Now on this call: …"). |
| Start time moved | Edit the post; thread reply "Moved to <new time>" tagging caller + manager; upload a new brief. |
| Round canceled / rejected | Edit the post to "Canceled"; thread reply tagging caller + manager. |
| Caller changed to someone outside the team (or TBD / Self / caller off), or round deleted | Edit the post to "Moved off this team"; thread reply tagging the previous caller + manager. |
| Other edits (notes, method, link) | Edit the post only. |

Edits do not notify in Slack, so anything a person must act on gets a tagged thread reply.

The post shows: company, round type, start (in the team's timezone, plus Slack's `<!date>` so each reader also sees
their own time), call method and number/link, the assigned caller and the manager. No owner, no app links.

**Only upcoming rounds** (start in the future, or no start yet) are ever posted. An edit to an old interview never
posts a past round, so nothing is uploaded for it.

**Time not set yet (TBD):** post at assignment with "Time TBD" and upload the brief. When the time is first set,
edit the post, add a tagged thread reply ("Time set: …") and upload a new brief. *(Builder's call.)*

The 5-minute tick also runs a **sweep** over every upcoming round that is a team round or already has a
`TeamRoundPost`, and brings each in line with the table above. This covers:
- a background task that failed;
- a team or channel set up after the assignment. At rollout, upcoming rounds already assigned to members get posted
  on the first tick; no backfill script is needed;
- admin changes that touch no interview: a caller moved in or out of a team, a team archived, the manager changed,
  or a Slack ID added. A round whose caller left the team, or whose team was archived, is marked "Moved off this
  team". A new manager is tagged from the next reply on.

*(Builder's call.)*

### Files

- **Brief PDF, built on the server.** Same content as the app's brief (`engineers_app/src/lib/interviewBriefPdf.ts`,
  `interviewBrief`): company, round, when (team timezone), status, position, tech stack, job post, profile, meeting
  how/link, round notes, previous rounds, job description. Same file name: `Brief - <Company> - <Round> - <YYYY-MM-DD>.pdf`.
  Built as HTML and rendered with the Playwright PDF path the resume service already uses.
- **Resume**: the interview's linked resume (`resumeLink`) — tailored/fixed via `ResumeApplication` (`s3Key` or
  `pdfData`), uploaded via the account's resume (`s3Key`) — read the way `job_applies` serves signed resume links
  (factor that read into a shared helper). Uploaded as the PDF itself, not a zip. No linked resume or a missing file:
  the brief still goes, and the thread says "No resume linked — ask the interview manager."

### Reminder channel

- **Daily list** at the team's `dailyTime` in its `timezone`: the team's rounds of that local day, built like the
  main day post (one line per round: time, company, round type, caller, method), without app links or buttons.
  Edited in place whenever a team round of that day changes (same trigger as the main day post) and on each tick.
  No rounds that day: a one-line "No team interviews today."
- **30-minute reminder**: one top-level message per round and start, ~30 minutes before (same window logic as
  `run_round_reminders`), tagging caller + manager, with a link to the round's team-channel thread (files are
  there). A rescheduled round gets a new reminder.

### Admin screen (engineers_app)

On the existing callers screen, for admins and interview managers (same permission as caller edits):
- Each external caller: a **Slack ID** field and a **Team** select.
- A **Teams** section: add/edit/archive a team (name, manager, team channel ID, reminder channel ID, daily time).
- Caller pickers show "· <Team name>" next to a team member's name.
- The team card shows the connection: "Connected: <workspace name> ✓" from Slack `auth.test` with the callers'
  token, or "Not connected — set SLACK_CALLERS_BOT_TOKEN" when unset/invalid.
- A **Send test post** button posts a short test message to both team channels and reports each result (catches a
  wrong channel ID or a bot not invited).

Backend: `GET/POST/PATCH /callers/teams` (+ archive), `GET /callers/teams/connection`,
`POST /callers/teams/{id}/test`, and `slackUserId`/`teamId` on the existing caller create/update, all behind
`require_caller_manager`.

### Rollout of the existing "Jamil's group" caller

It stays an ordinary caller entry; no data script. At rollout an interview manager adds the real members as
callers (with Slack IDs and the team), creates the team, reassigns upcoming "Jamil's group" rounds to a person, and
archives the old entry. *(Default chosen; the user did not pick another option.)*

## Builder's calls

- Template, layout and wording of the posts and the brief HTML.
- Exact field validation messages.
- How the post sync is factored, as long as the main workspace's behaviour and tests are unchanged.

## Setup (done by the user, outside the code)

1. Create a Slack app in the callers' workspace (api.slack.com/apps → From scratch); bot scopes `chat:write`,
   `files:write`; install; copy the `xoxb-` bot token to Railway as `SLACK_CALLERS_BOT_TOKEN`. The token decides
   the workspace; there is no workspace ID setting.
2. Create the team channel and the reminder channel; invite the bot to both; invite the interview managers to the
   reminder channel.
3. In the app: set each team member's and the manager's Slack member ID (from the callers' workspace), create the
   team with both channel IDs.
