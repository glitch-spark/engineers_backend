# Caller Interview Slack Coordination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Interview managers, typed Slack IDs, a Confirmed flag on caller rounds, one #caller thread per interview with replies for confirm / reschedule / cancel / next round, and a 05:00 Chicago daily list in #engineer-caller-test.

**Architecture:** Pure decision and text functions (`app/interview_caller.py`, `app/slack_bot.py`) are unit-tested without Slack or a DB; one sync function (`sync_caller_thread`) replaces the per-round `notify_round_caller` path and is called from the interview routes; the daily list is a new step in the existing 5-minute cron using the `SlackAlertLog` claim pattern. Frontend adds fields to existing screens.

**Tech Stack:** FastAPI + Beanie/MongoDB (mongomock-motor in tests), Slack Web API via `_slack_api`; React + TS + SWR + Tailwind.

**Spec:** `docs/superpowers/specs/2026-10-06-caller-interview-slack-design.md`

## Global Constraints

- Worktrees on branch `feat/interviews`: backend `engineers_backend/.worktrees/interviews`, frontend `engineers_app/.worktrees/interviews` (node_modules is a junction — don't npm install). Commit per task; never push.
- Tests never touch a real DB or Slack: the local `.env` is production. Backend: `C:/Users/Administrator/Documents/Engineer/engineers_backend/.venv/Scripts/python.exe -m pytest tests_unit --continue-on-collection-errors` (3 known collection errors). Frontend: `npx tsc -b` (2 known `InterviewPrepLibrary.tsx` errors) and `npx vite build --outDir <scratchpad>/fe-build --emptyOutDir`.
- Slack member ID: `^[UW][A-Z0-9]{8,11}$` after trim + upper-case; blank clears; duplicate → 409 `That Slack ID is already used by another user`.
- Manager: `interviewManagerId` set by admins only; must be an existing active user other than the user (else 400 `Choose another active user`); `null` clears.
- `confirmed` only on caller rounds (else 400 `Only caller rounds are confirmed`); resets to `False` when `scheduledAt` changes; new rounds start `False`; editable by owner or admin (`_load_editable`).
- Thread reply copy (exact): `:arrow_right: *Moved to {stage}* — {time} · needs confirmation` (`· no caller needed` when the new round has no caller); `:no_entry: *{Canceled|Rejected}* — {stage} · {time}`; `:white_check_mark: *Back on* — {stage} · {time} · needs confirmation`; `:repeat: *Rescheduled* — {old} → {new} · needs confirmation`; `:white_check_mark: *Confirmed* — {stage} · {time} · caller {callerName}`; `:warning: *Confirmation removed* — {stage} · {time}`; `:wastebasket: *Round removed* — {stage}`; other changes keep `:pencil2: *Caller details updated* by …` + change lines. Every reply ends with a line `cc <@manager> <@owner>` (names without a Slack ID; `no interview manager assigned` when none).
- Daily list: settings `SLACK_CALLER_DAILY_CHANNEL_ID` (unset = off), `SLACK_CALLER_DAILY_TIME` (`05:00`), `SLACK_CALLER_DAILY_TIMEZONE` (`America/Chicago`); claim `SlackAlertLog(userId=CALLER_CHANNEL_SENTINEL, kind="caller-daily:YYYY-MM-DD")`; header `:telephone_receiver: *Caller interviews today — {Wed Oct 7}*`; empty `:telephone_receiver: No confirmed caller interviews today ({Wed Oct 7}).`
- Times in Slack text always via `slack_time` (each reader's own zone).

## Review Focus

- A round rescheduled and confirmed in the same PATCH → ends confirmed: an explicit `confirmed: true` in that request wins over the reset (the person confirmed the new time). (Task 2 test)
- An interview with old per-round posts gets a next round → the new reply goes to the adopted latest post, no second main post. (Task 4 test)
- Next round added with `markPreviousPassed` → exactly one reply (`Moved to`), not an extra "details updated" for the previous round. (Task 4 test)
- Daily list on the DST day (Sun Nov 1 2026) → rounds between 00:00 and 24:00 Chicago (25 h) are listed, none from the next day. (Task 5 test)
- A Slack ID already held by another user → 409, the other user keeps it. (Task 1 test)

---

### Task 1: Interview managers and typed Slack IDs (backend)

**Files:**
- Modify: `app/models/user_extra.py` (add `interviewManagerId: Optional[PydanticObjectId] = None`), `app/routers/users.py` (`UserUpdate.interviewManagerId`, rules, list adds `interviewManagerName`), `app/routers/slack_bot.py` (`SlackPrefsUpdate.slackUserId`, `_slack_profile_fields` returns `slackUserId`; delete `oauth-start`, `oauth-callback`, `disconnect` routes and their helpers if unused), `app/slack_bot.py` (test-DM error text `Add your Slack member ID before sending a test DM`)
- Test: `tests_unit/test_interview_manager.py` (mongomock + httpx ASGI client, `get_current_user` overridden; pattern: `tests_unit/test_bids_review.py` `make_client`)

**Interfaces:**
- Produces: `UserExtra.interviewManagerId`; `GET /users` items carry `interviewManagerId: str|null`, `interviewManagerName: str|null`; `PATCH /integrations/slack/prefs {slackUserId}`; `GET /integrations/slack/status` → `slackUserId`. Helper `normalize_slack_id(raw: str) -> Optional[str]` (raises `ValueError`) in `app/slack_bot.py`.

- [ ] **Step 1: Write failing tests:** `test_admin_sets_and_clears_manager`, `test_manager_must_be_active_other_user` (self → 400, inactive → 400, unknown id → 400, all with detail `Choose another active user`), `test_staff_cannot_set_manager` (403), `test_users_list_has_manager_name`, `test_slack_id_is_normalized_and_saved` (`" u04abc12345 "` → `U04ABC12345`), `test_slack_id_rejects_bad_format` (`"@pedro"` → 400), `test_slack_id_taken_is_409_and_other_keeps_it`, `test_blank_slack_id_clears`, `test_oauth_routes_are_gone` (`POST /integrations/slack/oauth-start` → 404/405).
- [ ] **Step 2: Run** `pytest tests_unit/test_interview_manager.py -v` — Expected: FAIL (fields/routes missing).
- [ ] **Step 3: Implement** per Interfaces and Global Constraints.
- [ ] **Step 4: Run** the file, then the full suite — Expected: pass (fix `tests_unit/test_slack_bot.py` only where it asserted the removed OAuth routes).
- [ ] **Step 5: Commit** `feat(users): interview managers and typed Slack member IDs`.

### Task 2: Confirmed caller rounds (backend)

**Files:**
- Modify: `app/models/interview.py` (`InterviewStageEvent.confirmed: bool = False`; `Interview.confirmed: bool = False`), `app/routers/interviews.py` (`StagePatch.confirmed: Optional[bool]`; `_sync_top_from_tip` mirrors `confirmed`; reset in `update_interview_stage` and the legacy `update_interview` stage path when `scheduledAt` changes value; 400 for non-caller rounds), `app/interview_caller.py` (`round_caller_snapshot` adds `"confirmed": bool(entry.confirmed)`)
- Test: `tests_unit/test_round_confirmed.py` (route level, mongomock; patch `app.routers.interviews._sync_caller_posts` / Task 4's replacement and `_after_save` to no-ops)

**Interfaces:**
- Produces: `entry.confirmed`, `interview.confirmed`; snapshot key `"confirmed"`; API JSON `confirmed` on rounds and interview.

- [ ] **Step 1: Write failing tests:** `test_owner_confirms_caller_round` (PATCH `{confirmed: true}` → round + interview `confirmed` true), `test_confirm_non_caller_round_is_400`, `test_reschedule_resets_confirmed`, `test_same_time_keeps_confirmed`, `test_reschedule_and_confirm_in_one_patch_stays_confirmed`, `test_new_round_starts_unconfirmed`, `test_other_user_cannot_confirm` (404/403 as `_load_editable`), `test_snapshot_has_confirmed`.
- [ ] **Step 2: Run** `pytest tests_unit/test_round_confirmed.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement.** Apply the reset before applying `body.confirmed`, so an explicit value in the same request wins.
- [ ] **Step 4: Run** the file and the full suite — Expected: pass.
- [ ] **Step 5: Commit** `feat(interviews): confirmed flag on caller rounds`.

### Task 3: Thread events and Slack text (backend, pure)

**Files:**
- Modify: `app/interview_caller.py` (add `caller_thread_event`), `app/slack_bot.py` (`caller_round_text` gains `manager` and the confirmed badge / no-caller variant; add `caller_thread_reply`)
- Test: `tests_unit/test_caller_thread_text.py`

**Interfaces:**
- Produces:
  - `caller_thread_event(prev: Optional[dict], new: Optional[dict], *, added: bool = False, removed: bool = False) -> Optional[str]` returning `"moved" | "removed" | "ended" | "back_on" | "rescheduled" | "confirmed" | "unconfirmed" | "details" | None`, first match in that order (`ended`: was on, now status in `ENDED_STATUSES`; `back_on`: the reverse; `rescheduled`: `time` differs; `confirmed`/`unconfirmed`: the flag flips; `details`: any other snapshot difference).
  - `caller_round_text(interview, entry, *, account, owner, coworkers, manager: Optional[UserExtra]) -> str` — existing lines plus `*Interview manager:* {mention | name | no interview manager assigned}` and a badge line `:white_check_mark: Confirmed` / `:hourglass_flowing_sand: Not confirmed yet`; when `entry.caller` is missing or disabled: header `:telephone_receiver: *No caller needed for {stage}* — posted by {owner}` with Profile / Company / Stage / Interview time only.
  - `async caller_thread_reply(event: str, entry, prev: Optional[dict], *, owner: Optional[UserExtra], manager: Optional[UserExtra]) -> Optional[str]` — copy per Global Constraints; `details` reuses `_change_lines`; returns None when `details` has no lines.

- [ ] **Step 1: Write failing tests:** one per event (`test_event_moved_wins_when_added`, `test_event_ended_and_back_on`, `test_event_rescheduled_before_confirmed`, `test_event_confirmed_and_unconfirmed`, `test_event_details_for_other_fields`, `test_event_none_when_unchanged`); text: `test_main_post_shows_manager_and_badge`, `test_main_post_without_manager`, `test_main_post_no_caller_variant`, `test_reply_texts_match_copy` (each event's first line, exact), `test_reply_cc_line_names_without_slack_id`.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file and `tests_unit/test_caller_post_sync.py tests_unit/test_caller_reminder.py` — Expected: pass (update existing `caller_round_text` call sites for `manager=None`).
- [ ] **Step 5: Commit** `feat(slack): caller thread events and message text`.

### Task 4: One thread per interview (backend)

**Files:**
- Modify: `app/models/interview.py` (`class SlackThread(BaseModel): channel: str; ts: str`; `Interview.slackThread: Optional[SlackThread] = None`), `app/slack_bot.py` (add `sync_caller_thread`; remove `notify_round_caller`; `_reminder_post` replies in `interview.slackThread.ts`, falling back to the round's legacy `caller.slackChannelTs`), `app/routers/interviews.py` (`_sync_caller_posts` calls `sync_caller_thread` with `added` / `deleted` round ids; `add_interview_stage` passes the new round id; delete-interview path deletes the main post), `app/interview_caller.py` (`build_round_caller` stops copying `slackChannelTs` forward — keep the field for old data)
- Modify tests: `tests_unit/test_caller_post_sync.py` (rewrite for `sync_caller_thread`)

**Interfaces:**
- Consumes: Task 3 functions; Task 1 `interviewManagerId`.
- Produces: `async sync_caller_thread(interview, before: dict[str, Optional[dict]], *, added: Iterable[str] = (), deleted: Iterable[str] = (), delete_all: bool = False) -> bool` — returns True when `interview.slackThread` changed (caller saves). Behaviour:
  1. `delete_all`: `chat.delete` the main post; return.
  2. No `slackThread`: adopt the latest round with a legacy `caller.slackChannelTs` (`channel` = `SLACK_CALLER_CHANNEL_ID`); else, if any round is on (caller enabled, not ended), post the main post (latest round) and save `{channel, ts}`, no reply; else nothing.
  3. With a thread: if any round's event (from `caller_thread_event`) is not None, `chat.update` the main post for the latest round, then post one reply: the `moved` event for an added round if any, else the latest changed round's event. A gone main post (`_MESSAGE_GONE`) is reposted and the new ts saved; no reply then.
  4. Never raises; logs and returns False on Slack errors.

- [ ] **Step 1: Write failing tests** (fake `_slack_api`, as in the current file): `test_first_caller_round_creates_thread`, `test_no_caller_round_posts_nothing`, `test_next_round_replies_moved_in_same_thread_and_updates_main`, `test_next_round_with_mark_previous_passed_posts_one_reply`, `test_confirm_replies_confirmed`, `test_reschedule_replies_rescheduled`, `test_cancel_replies_canceled`, `test_old_round_post_is_adopted_as_thread`, `test_gone_main_post_is_reposted`, `test_round_deleted_replies_removed`, `test_interview_delete_removes_main_post`, `test_reminder_posts_in_interview_thread`, `test_reminder_falls_back_to_round_post`.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite — Expected: pass.
- [ ] **Step 5: Commit** `feat(slack): one #caller thread per interview`.

### Task 5: Daily list in #engineer-caller-test (backend)

**Files:**
- Modify: `app/config.py` (three settings), `.env.example`, `app/slack_bot.py` (daily list), `app/slack_bot.py` `run_alert_tick` (call after caller reminders, own try/except, result key `caller_daily`), `docs/slack-interview-bot.md` (typed member ID, #caller thread behaviour, daily list and settings; remove the "never posted to a shared channel" claim)
- Test: `tests_unit/test_caller_daily.py`

**Interfaces:**
- Produces:
  - `caller_daily_due(now: datetime, *, at: time, tz: ZoneInfo) -> Optional[date]` — local date when local `now` ≥ `at` that day, else None.
  - `caller_daily_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]` — UTC-aware [local midnight, next local midnight).
  - `caller_daily_text(day: date, rows: list[dict]) -> str` — rows `{start, company, stage, profile, callerName, method, mentions: str, link: Optional[str]}` sorted by `start`.
  - `async run_caller_daily(now: datetime, *, load, claim, release, post, people, permalink, configured) -> str` — returns `"posted" | "not_due" | "already_sent" | "off" | "failed"`. `load(start, end)` returns `[(interview, entry)]` for rounds with `caller.enabled`, `confirmed`, status not in `ENDED_STATUSES`, `scheduledAt` in range (Mongo `$elemMatch`, as `_reminder_load`).

- [ ] **Step 1: Write failing tests:** `test_due_only_after_five_chicago` (04:59 CDT → None, 05:00 → date), `test_bounds_on_dst_day_are_25_hours` (2026-11-01), `test_text_lists_rows_with_mentions_and_link`, `test_text_empty_day`, `test_posts_once_per_day` (second run → `already_sent`), `test_catch_up_later_that_day_posts`, `test_failed_post_releases_claim`, `test_off_without_channel`, `test_only_confirmed_active_rounds_are_loaded` (mongomock: unconfirmed, canceled, other-day and non-caller rounds excluded), `test_alert_tick_runs_daily_after_reminders`.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.** Mentions per row: owner, coworkers, owner's manager (`_mention`), de-duplicated; `link` via `chat.getPermalink {channel, message_ts}` on `slackThread`, None on error.
- [ ] **Step 4: Run** the file and the full suite — Expected: pass.
- [ ] **Step 5: Commit** `feat(slack): daily confirmed caller interviews list`.

### Task 6: Profile Slack ID and Users manager (frontend)

**Files:**
- Modify: `src/api/endpoints.ts` (Slack status `slackUserId`; `updateSlackPrefs` accepts `slackUserId: string | null`; remove `startSlackOAuth` / `disconnectSlack`; user type `interviewManagerId`, `interviewManagerName`; `updateUser` accepts `interviewManagerId: string | null`), `src/pages/Profile.tsx` (`SlackAlertsCard`: member-ID input with Save / Clear and hint "In Slack: your profile → ⋮ → Copy member ID"; remove Connect/Disconnect + OAuth result handling; fix the instant-DM copy), `src/pages/Users.tsx` (column "Interview manager"; picker in the edit modal from `api.lookupUsers()` excluding the edited user, "None" option)
- Interfaces: Consumes Task 1 API.

- [ ] **Step 1: Implement.**
- [ ] **Step 2: Run** `npx tsc -b` and the Vite build — Expected: only the 2 known errors; `✓ built`.
- [ ] **Step 3: Commit** `feat(profile,users): Slack member ID and interview manager`.

### Task 7: Confirmed in the interview UI (frontend)

**Files:**
- Modify: `src/api/endpoints.ts` (`confirmed: boolean` on rounds and interviews; round PATCH accepts `confirmed`), `src/components/interview/RoundFields.tsx` / `InterviewForm.tsx` (Confirmed switch when "Request a caller" is on, in `editRound` mode; sent in the PATCH), `src/components/interview/InterviewPanel.tsx` (quick Confirmed toggle for the latest caller round, owner/admin only — same rule as the existing edit buttons), `src/lib/stageBadge.ts` or a small `ConfirmedBadge` in `src/components/interview/` (green `Confirmed` / amber `Not confirmed`), `src/pages/interviews/InterviewsList.tsx` (badge on caller rows)
- Interfaces: Consumes Task 2 API.

- [ ] **Step 1: Implement.**
- [ ] **Step 2: Run** `npx tsc -b` and the Vite build — Expected as Task 6.
- [ ] **Step 3: Commit** `feat(interviews): confirm caller rounds in the app`.

### Task 8: Browser sandbox check (controller)

- [ ] **Step 1: Run** the sandbox (`scratchpad/e2e`, in-memory DB, production blocked; Slack faked by a capturing `_slack_api` in `e2e_backend.py`): set a Slack ID in Profile → admin assigns a manager on Users → create a caller interview (main post captured, manager tagged) → confirm in the panel (reply `Confirmed`) → reschedule (reply `Rescheduled`, badge back to Not confirmed) → next round (reply `Moved to`) → call `run_caller_daily` for that day (list captured).
- [ ] **Step 2: Look at** screenshots of Profile, Users, the panel badge/toggle; record defects for the final review.
