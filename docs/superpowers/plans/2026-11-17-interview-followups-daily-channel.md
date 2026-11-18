# Interview Follow-ups in the Daily Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each interview round one home message in the daily channel (its 30-min reminder) whose thread carries
the after-interview check and the 1-week result reminder, for caller and self rounds; strip the #caller post down to
caller assignment; drop the Friday roundup.

**Architecture:** A new `RoundSlackPost` collection records each round's home message (per round + start). One helper,
`post_in_home`, threads a follow-up under it or creates it. Reminders post top-level and record the home; the
existing chasers in `app/coordination/chase.py` stop filtering to caller rounds, read the daily channel, and post via
`post_in_home`. Existing click handlers are reused; two new ones cover Passed / Rejected from the check.

**Tech Stack:** Python 3.12, FastAPI, Beanie/Motor, Slack Web API (Block Kit), pytest + pytest-asyncio +
mongomock-motor (unit tests in `tests_unit/`).

**Spec:** `docs/superpowers/specs/2026-11-17-interview-followups-daily-channel-design.md`

## Global Constraints

- Every follow-up for a round goes in its home message thread in `SLACK_CALLER_DAILY_CHANNEL_ID`; never the
  #caller thread, never the morning day post thread.
- Chasers run only when bot token + daily channel + `SLACK_CALLER_ASKS_FROM` are set. No new setting.
- After-interview check: round status still `scheduled`, not a home assessment, end ≥ 15 min ago, end ≤ 2 days ago,
  end after the cutoff. Caller **and** self rounds.
- 1-week reminder: status `completed`, 7-day mark (end + 7 days) passed, and passed no more than 2 days ago.
- Dedup keys unchanged: `round-reminder:<roundId>:<startEpoch>`, `after-call:<roundId>:<startEpoch>`,
  `result-reminder:<roundId>`.
- Old buttons already in Slack keep working: no action id is removed from `COORD_ACTIONS` or `HANDLERS`.
- Commit messages: single line, no co-author trailer (user's global rule). Commit only once the user has approved
  committing; work on branch `feat/interview-followups-daily-channel` off `dev`.
- Run tests with `.venv/bin/python -m pytest -q tests_unit/<file>` (needs `mongomock-motor` in the venv).

## Review Focus

1. Home message deleted in Slack: the next follow-up posts top-level and replaces the stored home (Task 2 test).
2. Round rescheduled after its reminder: follow-ups go to the new start's home, not the old one (Task 2 test).
3. Round added < 30 min before start (no reminder, so no home): the check creates the home top-level (Task 4 test).
4. Daily channel set but `SLACK_CALLER_CHANNEL_ID` unset: chasers still run (Task 4 test).
5. A round asked in #caller before deploy (key exists) is not asked again (Task 4 test).

---

### Task 1: Coordination post without buttons

**Files:**
- Modify: `app/coordination/blocks.py` (`post_blocks`)
- Test: `tests_unit/test_coordination_blocks.py`

**Interfaces:**
- Produces: `post_blocks(...)` same signature; returns `[section]` or `[section, actions]`.

- [ ] **Step 1: Write failing tests**

```python
def test_post_has_brief_and_open_as_text_links(): ...
    # section text contains f"<{base}/interview/{iv}?brief={rnd}|Brief>" and f"<{base}/interviews?panel={iv}|Open>"
    # no element anywhere has text "Download brief" or "Open interview"
def test_open_round_actions_are_the_caller_select_only(): ...
    # status scheduled → blocks[1]["elements"] == [caller_select element] (len 1, type "static_select")
@pytest.mark.parametrize("status", ["completed", "passed", "rejected", "canceled"])
def test_settled_round_has_no_actions_block(status): ...
    # len(blocks) == 1; no COORD_RESULT_PASSED / COORD_RESULT_REJECTED anywhere
```

Update existing tests in the file that assert the old buttons.

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/python -m pytest -q tests_unit/test_coordination_blocks.py`
- [ ] **Step 3: Implement** — links via `interview_day.blocks._mrkdwn_link` appended to the Owner/Caller line as
  `· Brief · Open`; actions block only when `r.status not in _STATUS_LINES`.
- [ ] **Step 4: Run, expect PASS** (same command, plus `tests_unit/test_coordination_*.py`).
- [ ] **Step 5: Commit** — `git commit -m "feat(coordination): caller post keeps only the caller select"`

### Task 2: Round home message

**Files:**
- Create: `app/models/round_slack_post.py`, `app/interview_day/home.py`
- Modify: `app/models/__init__.py`, `app/db.py` (register the model like `InterviewDayPost`)
- Test: `tests_unit/test_round_home.py`

**Interfaces:**
- Produces:
  - `class RoundSlackPost(TimestampedDocument)`: `roundId: str`, `interviewId: str`, `startEpoch: int`,
    `channel: str`, `ts: str`; `Settings.name = "roundslackposts"`, unique index `(roundId, startEpoch)`.
  - `home.start_epoch(start: datetime) -> int` (`int(aware_utc(start).timestamp())`).
  - `async home.record_home(round_id: str, interview_id: str, start: datetime, channel: str, ts: str) -> None`
    (upsert on `roundId + startEpoch`).
  - `async home.post_in_home(round_id: str, interview_id: str, start: datetime, channel: str, text: str,
    blocks: list[dict]) -> str` → ts of the posted message. Home exists → `chat.postMessage` with its `thread_ts`;
    Slack says gone (`slack_bot._MESSAGE_GONE | {"thread_not_found"}`) or no home → post top-level and
    `record_home` with the new ts. Other errors raise.

- [ ] **Step 1: Write failing tests** (fake `slack_bot._slack_api` returning `{"ok": True, "ts": "<n>"}`)

```python
async def test_no_home_posts_top_level_and_records_it(): ...      # no thread_ts sent; RoundSlackPost saved with ts
async def test_home_exists_replies_in_its_thread(): ...           # thread_ts == stored ts; record unchanged
async def test_gone_home_posts_top_level_and_replaces_it(): ...   # first call raises RuntimeError("message_not_found")
async def test_home_is_per_start(): ...                           # home for start A not used for start B
```

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/python -m pytest -q tests_unit/test_round_home.py`
- [ ] **Step 3: Implement** the model, registration and `home.py`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(interview-day): round home message in the daily channel"`

### Task 3: Reminders post top-level and become the home

**Files:**
- Modify: `app/interview_day/reminders.py` (`run_round_reminders`, `_send`)
- Test: `tests_unit/test_interview_reminders.py`

**Interfaces:**
- Consumes: `home.record_home`.
- Produces: after a sent reminder, `RoundSlackPost(roundId=r.round_id, startEpoch=start_epoch(r.start))` exists.

- [ ] **Step 1: Write / update tests**

```python
async def test_reminder_is_top_level_even_with_a_day_post(slack): ...   # no "thread_ts" in the posted body
async def test_sent_reminder_is_recorded_as_the_rounds_home(slack): ... # RoundSlackPost with the returned ts
async def test_rescheduled_round_gets_a_new_home(slack): ...            # two records, one per startEpoch
```

Rewrite the tests that assert threading under the day post (`test_round_just_after_midnight_threads_under_its_own_days_post`,
`test_lost_day_post_falls_back_to_top_level`, `test_round_after_midnight_without_its_post_yet_is_posted_top_level`,
`test_no_day_post_posts_top_level`) to assert top-level; delete the ones that only test the day-post lookup.

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/python -m pytest -q tests_unit/test_interview_reminders.py`
- [ ] **Step 3: Implement** — post with no `thread_ts`; on success `record_home(...)` with the response ts; a
  failing `record_home` is logged, not counted as a failed reminder (the message is out). Drop the unused
  `parents` / `parent_ts_for_day` use here.
- [ ] **Step 4: Run, expect PASS** (plus `tests_unit/test_interview_day_*.py`).
- [ ] **Step 5: Commit** — `git commit -m "feat(interview-day): 30-min reminder is the round's own message"`

### Task 4: Chasers cover every round, post in the home, no roundup

**Files:**
- Modify: `app/coordination/chase.py`, `app/slack_bot.py` (module docstring only), `docs/slack-interview-bot.md`,
  `readme.md` (`SLACK_CALLER_ASKS_FROM` / roundup wording if present)
- Test: `tests_unit/test_coordination_chase.py`, `tests_unit/test_coordination_roundup.py`

**Interfaces:**
- Consumes: `home.post_in_home`.
- Produces:
  - `async ended_rounds(status: str, *, ended_after: datetime, ended_by: datetime) -> list[ChaseRound]` — replaces
    `caller_rounds`: same bounded query and filters **minus** `caller.enabled` and `slackThread` requirements.
  - Constants `ASK_DELAY = timedelta(minutes=15)`, `MAX_AGE = timedelta(days=2)`.
  - `_active_cutoff()` → cutoff when token + **daily** channel (`slack_bot._caller_daily_configured()`) + cutoff set.
  - `run_after_call_asks(now)`: `ended_rounds("scheduled", ended_after=max(cutoff, now - MAX_AGE),
    ended_by=now - ASK_DELAY)`.
  - `run_result_reminders(now)`: `ended_rounds("completed", ended_after=max(cutoff, now - RESULT_REMINDER_AFTER -
    MAX_AGE), ended_by=now - RESULT_REMINDER_AFTER)`.
  - `_chase` posts with `post_in_home(str(c.entry.id), str(c.interview.id), c.start, daily_channel, text, blocks)`.
  - `run_coordination_chase` no longer calls `run_result_roundup`; keeps `refresh_latest_roundup` (old roundup rows
    still get removed when answered). `_open_rounds` drops the caller filter.

- [ ] **Step 1: Write / update tests**

```python
async def test_self_round_is_asked(slack, justin): ...                      # caller=False, thread=False → 1 ask
async def test_ask_waits_fifteen_minutes_after_the_end(slack, justin): ...   # now = end+14m → 0; end+15m → 1
async def test_rounds_ended_over_two_days_ago_are_not_asked(slack, justin): ...  # end = now-2d-1m → 0
async def test_ask_threads_under_the_rounds_home(slack, justin): ...         # RoundSlackPost exists → thread_ts == its ts, channel == daily
async def test_round_without_home_gets_one_created(slack, justin): ...       # no record → top-level in daily, record created
async def test_runs_without_the_caller_channel(slack, justin, monkeypatch): ...  # SLACK_CALLER_CHANNEL_ID="" → still asks
async def test_off_without_the_daily_channel(slack, justin, monkeypatch): ...    # daily unset → 0
async def test_round_asked_under_the_old_flow_is_not_asked_again(slack, justin): ...  # pre-insert after-call key → 0
async def test_result_reminder_covers_self_rounds_in_the_home_thread(slack, justin): ...
async def test_result_reminder_skips_rounds_past_the_two_day_window(slack, justin): ...  # end+7d+2d+1m ago → 0
async def test_roundup_is_no_longer_posted(slack, justin): ...                # Friday 18:05 tick → no roundup post
```

Update tests asserting the #caller thread / caller-only behavior (`test_caller_turned_off_is_not_asked` becomes
"asked"; `test_gone_thread_posts_in_the_channel` moves to Task 2's coverage). In `test_coordination_roundup.py`
keep tests for refreshing / answering an existing roundup; drop the ones that post a new one.

- [ ] **Step 2: Run, expect FAIL** — `.venv/bin/python -m pytest -q tests_unit/test_coordination_chase.py tests_unit/test_coordination_roundup.py`
- [ ] **Step 3: Implement** per Interfaces; delete `run_result_roundup`, `roundup_due`, `_roundup_message` and
  `roundup_blocks` if nothing else imports them (grep). Update docstrings/docs to say follow-ups live in each round's
  home thread in the daily channel.
- [ ] **Step 4: Run, expect PASS** — the two files plus `tests_unit/test_coordination_*.py tests_unit/test_slack_bot.py`.
- [ ] **Step 5: Commit** — `git commit -m "feat(coordination): follow-ups for every round in its daily-channel thread, roundup dropped"`

### Task 5: Five-button check and its handlers

**Files:**
- Modify: `app/coordination/blocks.py` (`ask_blocks`, ids), `app/coordination/actions.py`, `app/coordination/modals.py`
- Test: `tests_unit/test_coordination_blocks.py`, `tests_unit/test_coordination_clicks.py`, `tests_unit/test_coordination_modals.py`

**Interfaces:**
- Produces: `COORD_AFTER_PASSED = "coord_after_passed"`, `COORD_AFTER_REJECTED = "coord_after_rejected"` (added to
  `COORD_ACTIONS`); `actions.after_passed(click, user)`, `actions.after_rejected(click, user)` registered in `HANDLERS`.

- [ ] **Step 1: Write failing tests**

```python
def test_ask_has_five_buttons_in_order(): ...
    # action_ids == [COORD_AFTER_COMPLETED, COORD_AFTER_PASSED, COORD_AFTER_REJECTED, COORD_AFTER_RESCHEDULED, COORD_AFTER_CANCELED]
    # labels == ["Completed", "Passed", "Rejected", "Rescheduled…", "Canceled"]
async def test_after_passed_writes_passed_on_an_ended_scheduled_round(): ...   # status passed; ask replaced by "🎉 Passed — by …"
async def test_after_passed_refuses_a_round_not_ended_yet(): ...               # MOVED reply, status unchanged
async def test_after_rejected_opens_the_reject_form(): ...                      # views.open with the reject view
async def test_reject_form_accepts_an_ended_scheduled_round(): ...              # submission → status rejected
async def test_reject_form_refuses_a_scheduled_round_not_ended(): ...           # MOVED, unchanged
async def test_reject_form_still_accepts_completed(): ...                       # old result button path
```

- [ ] **Step 2: Run, expect FAIL** — the three test files.
- [ ] **Step 3: Implement** — handlers reuse `_clicked_round` + `_ask_still_open`, then `_write_and_show(..., "passed")`
  / `_open_form(..., reject_view)`. In `modals.process_coordination_submission`, for `rejected` accept `completed`, or
  `scheduled` whose `round_ends_at` is past (reuse the existing MOVED check).
- [ ] **Step 4: Run, expect PASS** — then the full suite `.venv/bin/python -m pytest -q tests_unit` (baseline any
  pre-existing failures first and compare).
- [ ] **Step 5: Commit** — `git commit -m "feat(coordination): check offers Passed and Rejected directly"`
