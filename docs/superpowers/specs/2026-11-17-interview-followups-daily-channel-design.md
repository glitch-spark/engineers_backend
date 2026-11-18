# Interview follow-ups move to the daily channel

Date: 2026-11-17. Status: draft for approval (user answered the builder-check questions).

## Why

The coordination channel (`SLACK_CALLER_CHANNEL_ID`, "#caller") is cluttered: every interview post carries
Download brief + Open interview buttons for its whole life, and once a round is completed the post grows Passed /
Rejected buttons too. Results and "did it happen?" checks are time-based work; they belong next to the day's
schedule in the daily interview channel (`SLACK_CALLER_DAILY_CHANNEL_ID`), where people already look before each
interview. Today that channel gets nothing after an interview ends, its 30-minute reminders are buried as replies under the
morning post, and interviews the owner takes alone are never followed up at all.

## Goals

1. Coordination post is about one thing: who is on the call. No Brief / Open buttons, no result buttons.
2. Each interview round gets **its own message** in the daily channel: the 30-minutes-before reminder, posted
   top-level (no longer a reply under the morning day post). That message is the round's home: every later
   follow-up for the round is a reply in its thread.
3. Every round (caller rounds **and self rounds**) gets one after-interview check in its thread, tagging the owner,
   settled in one click: Completed / Passed / Rejected / Rescheduled… / Canceled.
4. A round left "Completed" with no result gets a 1-week result reminder in its thread (Passed / Rejected). This
   replaces the Friday roundup, which is removed.
5. The morning day post stays as the day's overview.

## Non-goals

- No change to the morning day post layout (its Brief / Open text links stay).
- No change to the in-app interview flow, statuses or the fields of the reject / cancel / reschedule forms (only the
  reject form's submit check widens, see "Calls made by the designer").
- No new channels, settings or DMs. Every interview owner is already in the daily channel (user confirmed).

## Anti-goals (a failure even if it works)

- The same round asked twice (once in #caller from the old flow, once in the daily channel).
- An old button already sitting in Slack (coordination post, old thread ask, old roundup, old reminder reply)
  stops working or errors.
- A late or rescheduled answer overwrites a round that has since moved.
- A follow-up lands somewhere unrelated to its round (another round's thread, the day post thread).

## Approaches considered

- **A. One home message per round, follow-ups as its replies (picked by the user).** Each round's 30-min reminder
  is a top-level message in the daily channel; the after-interview check and 1-week reminder reply in its thread.
  Each round's whole story is in one thread, and the channel reads as a timeline of interviews. Cost: one
  top-level message per round in the channel.
- B. One reply per round under the day post (the user's first pick, replaced by A): everything for the day in one
  long thread; rounds' follow-ups interleave.
- C. One checklist reply per day / status inside the day post: dropped (weak pings; Slack's 50-block limit).

## Design

### 1. Coordination post (`app/coordination/blocks.py: post_blocks`)

- Section text: unchanged head and Owner/Caller line, plus status line when set. Brief and Open become mrkdwn text
  links appended to the Owner/Caller line (`· <url|Brief> · <url|Open>`), same URLs as today, same style as the day
  post's `day_line`.
- Actions block: only while the round is not completed/passed/rejected/canceled — the Choose caller select alone.
  Otherwise no actions block at all.
- Passed / Rejected never render on this post again.
- Old posts are re-rendered by the existing `chat.update` path the next time their round changes; until then their
  old buttons keep working (handlers stay registered).

### 2. The round's home message (`app/interview_day/reminders.py`)

- `run_round_reminders` posts each due reminder **top-level** in the daily channel (drop the `thread_ts` of the day
  post). Content unchanged (`round_blocks` with the "Starting in N min" header and its buttons).
- Store the sent message per round and start: new collection (e.g. `RoundSlackPost {roundId, interviewId,
  startEpoch, channel, ts}`, unique on `roundId + startEpoch`). Dedup stays the existing claim key
  `round-reminder:<roundId>:<startEpoch>`.
- A rescheduled round gets a new reminder (new start → new key) and so a new home message; follow-ups go to the
  home message of the round's **current** start.
- No home message (round added less than 30 min before it starts, reminders off at the time, rounds from before
  this ships, or the message deleted in Slack): the first follow-up posts top-level in the daily channel and is
  stored as the round's home message, so later follow-ups thread under it.

### 3. After-interview check (replaces the #caller after-call ask)

- **Who:** every round the day post lists — caller and self — except home assessments, whose status (normalized) is
  still `scheduled`, and whose end (`round_ends_at`) is ≥ 15 min ago, **no more than 2 days ago** (user's answer: no
  backlog of old self rounds on deploy day; also bounds any catch-up after an outage) and after the
  `SLACK_CALLER_ASKS_FROM` cutoff.
  Self rounds have no `slackThread`, so the query must not require one (unlike `caller_rounds`).
- **Where:** reply in the round's home message thread (section 2).
- **Message:** `@Owner — BOND · Intro ended 12:15 PM. How did it go?` then
  `[Completed] [Passed] [Rejected] [Rescheduled…] [Canceled]`.
- **Dedup:** reuse `after-call:<roundId>:<startEpoch>` (same `SlackAlertLog` claim/release), so a round already
  asked in #caller under the old flow is not asked again, and a rescheduled round is asked again after its new end.
- **Clicks:** Completed / Rescheduled… / Canceled reuse the existing after-call handlers (`after_completed`,
  `after_rescheduled`, `after_canceled`: same permission check, still-open check, forms, answer line).
  Passed and Rejected from this ask are new actions (`coord_after_passed`, `coord_after_rejected`): same still-open
  check as the other after-call answers (round still `scheduled` and really ended), then Passed writes `passed`
  directly, Rejected opens the existing reject form. The existing `result_*` handlers (which require `completed`)
  stay unchanged for old buttons.
- **After a click:** the ask is replaced by its answer line (existing `show_answer`), and the day post is re-rendered
  so its line shows the new status (today's via `refresh_today`; a past day's only if the refresh path supports it).

### 4. 1-week result reminder

- `run_result_reminders` keeps its rule (completed, no result, 7 days after the round, once per round, key
  `result-reminder:<roundId>`) but now covers self rounds too and replies in the round's home message thread instead
  of the #caller thread. Buttons Passed / Rejected (existing `result_*` handlers).
- Same 2-day bound (designer's call, mirrors the check): only rounds whose 7-day mark passed in the last 2 days, so
  old completed rounds without a result don't all get a reminder on deploy day.
- This is the only result follow-up (user's answer).

### 5. Friday roundup — removed

- User's answer: drop it; the 1-week reminder is the only result follow-up. Stop posting it (`run_result_roundup`
  out of `run_coordination_chase`), delete `roundup_blocks` / roundup code that nothing else uses.
- Keep what old roundup messages need: their Passed / Rejected buttons still work and `show_answer` still edits the
  old roundup in place (`ResultRoundupPost` lookup + `refresh_roundup`), with self rounds not dropped on redraw.

### 6. Turn-on and gating

- The chasers run when a bot token, the **daily** channel and `SLACK_CALLER_ASKS_FROM` are set (today: the caller
  channel). No new setting.
- Deploy is a straight swap: the first tick after deploy uses the new flow; dedup keys prevent repeats.

## Calls made by the designer (from the builder check)

- Reject form from the new check: the reject modal's submit check (`_NEEDED_STATUS`, `app/coordination/modals.py`)
  also accepts a round still `scheduled` that has really ended. Without it, Rejected from the check is always
  refused. The form itself is unchanged.
- Self rounds anywhere a round set is re-read for follow-ups (e.g. the roundup redraw, `_open_rounds` in
  `chase.py`) must not drop rounds without a caller.

## Builder checks (verify, don't assume)

- 30-minute reminders: `load_rounds` already lists self rounds; confirm a rescheduled round gets a new reminder
  (new key per start) and that a canceled round gets none.
- Whether `refresh_today` (or a sibling) can re-render a past day's post; if not, re-rendering only today's post on
  answer is acceptable.

## Left to the builder

- Exact module layout (extend `coordination/chase.py` or add `interview_day/followups.py`).
- Round query shape, as long as it's bounded like `caller_rounds` (no full-history scans).
- Tests: the new round selection (self + caller, home assessments out, 15-min delay, 2-day bound, cutoff), the post
  without buttons, the top-level reminder + stored home message, the fallback home message, the five-button ask in
  the home thread, the two new handlers, the reject form accepting an ended scheduled round, the 1-week reminder in
  the home thread (self rounds included, 2-day bound), the roundup no longer posted.
