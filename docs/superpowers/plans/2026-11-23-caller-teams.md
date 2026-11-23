# Caller Teams Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Caller teams whose rounds are announced (with brief + resume files), listed daily and reminded in their own
channels of a separate Slack workspace, tagging the assigned caller and the team manager.

**Architecture:** New package `app/caller_teams/` (models live in `app/models/`) holds the team-channel sync, the
reminder-channel day list and reminders, and the brief/resume files. It talks to the callers' workspace through
`slack_bot._slack_api(..., workspace="callers")`. It hooks into the existing write path (`_sync_caller_posts`) and
cron tick (`run_alert_tick`) without changing what the main workspace gets. Admin UI lives on the existing
Callers directory page in engineers_app.

**Tech Stack:** FastAPI, Beanie/MongoDB (mongomock-motor in tests), httpx Slack Web API, Playwright PDF
(`resume_service._html_to_pdf`); React 18 + TypeScript + SWR + Tailwind, vitest + Testing Library.

**Spec:** `docs/superpowers/specs/2026-11-23-caller-teams-design.md` (read it first; this plan does not repeat its
tables).

## Global Constraints

- Callers' workspace token: env `SLACK_CALLERS_BOT_TOKEN`; unset or empty = all team posting off, nothing raised.
- Bot scopes: `chat:write`, `files:write`. Nothing posted in the callers' workspace has buttons or app links.
- Main workspace (#caller, daily channel, reminders) behaviour and existing tests unchanged.
- Daily list default `dailyTime="06:00"`, `timezone="America/Chicago"`; reminder lead = `slack_bot.CALLER_REMINDER_LEAD` (30 min).
- Only upcoming rounds (start > now, or no start) are ever posted or get files.
- Brief file name: `Brief - <Company> - <Round label> - <YYYY-MM-DD>.pdf` (date in team timezone; omit date part when TBD).
- Permissions: every new endpoint behind `require_caller_manager` (admin, interview_manager).
- No migration, no change to `InterviewDayPost`'s index. Unique indexes on new collections only.
- Slack failures never fail or slow an interview save: run in background tasks, log with interview id, round id, team id.
- Never log the token. Commit messages: single line, no co-author trailer.
- Local backend points at prod MongoDB: do not exercise write endpoints locally; tests use mongomock.

## Review Focus

1. **Same round posted twice** (interview-write background task and 5-minute sweep race) → exactly one team post. Claim the `TeamRoundPost` row (unique `teamId+roundId`) before calling Slack. Test in Task 4.
2. **Token unset / Slack down** → interview save and main-workspace posts unaffected; team sync returns quietly and the sweep retries next tick. Tests in Tasks 1 and 4.
3. **Caller or manager without a Slack ID** → post shows their name in plain text; no `<@>` with empty id. Test in Task 4.
4. **Resume missing or S3 unreachable** → brief still uploaded; thread says "No resume linked — ask the interview manager." Test in Task 3/4.
5. **Local-day edges** → a round at 00:30 America/Chicago belongs to that local day's list, not the UTC day; TBD rounds are left out of the day list and reminders. Test in Task 5.

---

### Task 1: Data models and callers' workspace Slack client

**Files:**
- Modify: `app/models/caller.py` (add fields), `app/models/__init__.py`, `app/db.py` (register documents), `app/config.py` (`SLACK_CALLERS_BOT_TOKEN: Optional[str] = None`), `app/slack_bot.py:84` (`_slack_api`)
- Create: `app/models/caller_team.py`, `app/caller_teams/__init__.py`, `app/caller_teams/slack.py`
- Test: `tests_unit/test_caller_teams_models.py`, `tests_unit/test_caller_teams_slack.py`

**Interfaces:**
- Produces:
  - `Caller.slackUserId: Optional[str] = None`, `Caller.teamId: Optional[str] = None`
  - `CallerTeam(TimestampedDocument)`: `name`, `nameKey` (unique), `managerCallerId: str`, `channelId: str`, `reminderChannelId: str`, `dailyTime: str = "06:00"`, `timezone: str = "America/Chicago"`, `archivedAt: Optional[datetime] = None`; collection `callerteams`
  - `TeamRoundPost`: `teamId`, `interviewId`, `roundId`, `channel`, `ts: str = ""`, `callerId: str = ""`, `startEpoch: Optional[int]`, `filesFor: Optional[int]` (`-1` = files sent for a TBD round), `state: str` (`"scheduled"|"canceled"|"off"`); unique (`teamId`,`roundId`); collection `teamroundposts`
  - `TeamDayPost`: `teamId`, `day: str`, `channel`, `ts: list[str]`, `interviewIds: list[str] = []`; unique (`teamId`,`day`); collection `teamdayposts`
  - `slack_bot._slack_api(method: str, body: dict, *, workspace: Literal["main","callers"] = "main") -> dict`
  - `caller_teams.slack.callers_configured() -> bool`
  - `caller_teams.slack.api(method: str, body: dict) -> dict` (= `_slack_api(..., workspace="callers")`)
  - `caller_teams.slack.upload_files(channel: str, thread_ts: str, files: list[tuple[str, bytes]], comment: str) -> None`
  - `caller_teams.slack.mention(caller: Optional[Caller]) -> str` → `<@U…>` with an id, else the name, else `""`
  - `caller_teams.slack.workspace_name() -> Optional[str]` (`auth.test` → `team`; None when unset/invalid)

- [ ] **Step 1: Write failing tests**

```python
def test_callers_workspace_uses_its_own_token(monkeypatch): ...
    # _slack_api("chat.postMessage", {...}, workspace="callers") sends "Bearer xoxb-callers";
    # default workspace still sends SLACK_BOT_TOKEN.
def test_callers_api_raises_when_token_unset(): ...  # RuntimeError("SLACK_CALLERS_BOT_TOKEN is not configured")
def test_callers_configured_false_for_blank_token(): ...
def test_upload_files_three_calls(): ...
    # per file: files.getUploadURLExternal(filename, length) then PUT bytes to upload_url;
    # then one files.completeUploadExternal with channel_id, thread_ts, initial_comment, files=[{id,title}...]
def test_mention_with_and_without_slack_id(): ...
    # mention(Caller(name="Omar", slackUserId="U07Q3KD2M9A")) == "<@U07Q3KD2M9A>"; no id → "Omar"; None → ""
def test_team_round_post_unique_per_team_and_round(): ...  # second insert raises DuplicateKeyError
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests_unit/test_caller_teams_slack.py tests_unit/test_caller_teams_models.py -q` → FAIL (import errors)
- [ ] **Step 3: Implement** the models, config field and client. `getUploadURLExternal` is form-encoded (`filename`, `length`), so `upload_files` uses httpx directly with the callers token for that call and the raw PUT/POST of bytes; `completeUploadExternal` goes through `api`. Each Slack call keeps the existing 15 s timeout.
- [ ] **Step 4: Run** the two files plus `tests_unit/test_caller_post_sync.py tests_unit/test_caller_daily.py tests_unit/test_callers.py -q` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat: caller team models and callers workspace Slack client"`

---

### Task 2: Caller Slack ID / team and team admin endpoints

**Files:**
- Modify: `app/routers/callers.py`, `app/caller_directory.py` (`CallerEntry`, `_external_entry`)
- Create: `app/routers/caller_teams.py` (mounted under `/callers/teams`; include it in `app/main.py` **before** the `/callers/{caller_id}` routes are matched)
- Test: `tests_unit/test_caller_teams_router.py`

**Interfaces:**
- Consumes: Task 1 models, `caller_teams.slack.workspace_name`, `caller_teams.slack.api`
- Produces:
  - Caller PUT/POST body gains `slackUserId: Optional[str]` (`""` clears; else must match `^[UW][A-Z0-9]{6,}$`, 422 "Slack member ID looks like U0123ABCD.") and `teamId: Optional[str]` (`""` clears; must be a non-archived team, 422 "No such team."). External callers only; 400 for a user caller.
  - `CallerEntry` adds `slackUserId: str` (`""` when none), `teamId: str`, `teamName: str` (user entries: all `""`)
  - `GET /callers/teams` → `{"teams": [TeamOut]}`; `TeamOut` = team fields + `_id`, `memberIds: list[str]`, `archived: bool`
  - `POST /callers/teams` (201), `PATCH /callers/teams/{id}` (partial; `archived: bool` archives/unarchives); 409 "A team with this name already exists."; manager must be a non-archived external caller (422 "Pick an external caller as manager."); `dailyTime` `HH:MM`, `timezone` a valid `ZoneInfo` (422 otherwise)
  - `GET /callers/teams/connection` → `{"connected": bool, "workspace": str | None}`
  - `POST /callers/teams/{id}/test` → `{"channel": {"ok": bool, "error": str | None}, "reminderChannel": {...}}` (posts "Test post from the interview bot for <team>." to each)

- [ ] **Step 1: Write failing tests** (follow `tests_unit/test_callers.py` client/auth fixtures)

```python
def test_staff_cannot_create_team(): ...                       # 403
def test_create_team_and_list_with_members(): ...              # members = callers with teamId
def test_manager_must_be_external_caller(): ...                # 422
def test_set_slack_id_validates_format(): ...                  # "abc" → 422; "U07Q3KD2M9A" stored; "" clears
def test_set_team_on_caller_and_directory_shows_team_name(): ...
def test_archived_team_rejected_for_new_member(): ...          # 422 "No such team."
def test_connection_reports_unset_token(): ...                 # {"connected": False, "workspace": None}
def test_send_test_reports_each_channel(): ...                 # second channel error "not_in_channel" surfaced
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests_unit/test_caller_teams_router.py -q` → FAIL
- [ ] **Step 3: Implement** the endpoints and directory fields.
- [ ] **Step 4: Run** the new file + `tests_unit/test_callers.py tests_unit/test_caller_directory.py -q` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat: caller team admin endpoints and caller Slack ID"`

---

### Task 3: Brief PDF and resume files on the server

**Files:**
- Create: `app/caller_teams/files.py`, `app/caller_teams/brief.html` (Jinja-free: `str.format`/`html.escape` template)
- Modify: `app/routers/job_applies.py:2144-2190` — move the "tailored / uploaded resume bytes" read into `app/resume_files.py` as `read_linked_resume`, and use it there (behaviour unchanged)
- Test: `tests_unit/test_caller_teams_files.py`

**Interfaces:**
- Consumes: `resume_service._html_to_pdf(html) -> bytes`, `resume_files.read_resume_file(key)`, `Interview.resumeLink`
- Produces:
  - `resume_files.read_linked_resume(*, kind: str, resume_job_id: Optional[str], account_id: Optional[str], resume_id: Optional[str]) -> Optional[tuple[str, bytes]]` → `(filename, bytes)`; None when nothing linked; raises `resume_files.FileUnavailable` when the record/file is gone
  - `caller_teams.files.brief_filename(company: str, stage_label: str, day: Optional[str]) -> str`
  - `caller_teams.files.brief_pdf(iv: Interview, round_id: str, tz: str) -> tuple[str, bytes]`
  - `caller_teams.files.round_files(iv: Interview, round_id: str, tz: str) -> tuple[list[tuple[str, bytes]], Optional[str]]` → files plus a note (`"No resume linked — ask the interview manager."` when the resume is missing/unreadable, else None). The brief failing raises; the resume failing only sets the note.

Brief content and order: same as `engineers_app/src/lib/interviewBriefPdf.ts` `interviewBrief` — company, stage label, when (team tz, `Thu, Nov 6, 2026 · 2:00 – 3:00 PM CST`), status, Position / Tech stack / Job post / Profile, How / Link-or-Details, notes, previous rounds table, job description. Empty values skipped. No owner, caller name, applicant or interviewer.

- [ ] **Step 1: Write failing tests** (patch `_html_to_pdf` to return `b"%PDF"` and capture the html)

```python
def test_brief_filename():
    assert brief_filename("Acme/Inc", "Tech round 1", "2026-11-24") == "Brief - Acme Inc - Tech round 1 - 2026-11-24.pdf"
    assert brief_filename("Acme", "Intro", None) == "Brief - Acme - Intro.pdf"
def test_brief_html_has_fields_and_escapes(): ...   # "<script>" in notes appears escaped; owner name absent
def test_brief_when_in_team_timezone(): ...         # 2026-11-24T15:30Z, America/Chicago → "9:30"
def test_round_files_tailored_resume(): ...         # two files, note None
def test_round_files_missing_resume_keeps_brief(): ...  # FileUnavailable → one file + the note
def test_job_applies_signed_link_unchanged(): ...   # existing signed-link test still passes via the helper
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests_unit/test_caller_teams_files.py -q` → FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** new file + `tests_unit/ -q -k "job_appl or resume_file"` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat: server-side interview brief PDF and linked resume read"`

---

### Task 4: Team channel sync ("Interview scheduled")

**Files:**
- Create: `app/caller_teams/sync.py`, `app/caller_teams/blocks.py`
- Modify: `app/routers/interviews.py:348-377` (`_sync_caller_posts`: also start a background `sync_team_posts(iv.id)` task, like `_refresh_day_post`; deleted rounds included), `app/slack_bot.py` `run_alert_tick` (add an isolated `sweep_team_posts(now)` step)
- Test: `tests_unit/test_caller_teams_sync.py`

**Interfaces:**
- Consumes: Tasks 1 and 3
- Produces:
  - `sync.TeamRound` (dataclass): `team: CallerTeam`, `caller: Caller`, `manager: Optional[Caller]`, `iv: Interview`, `entry: InterviewStageEvent`, `start: Optional[datetime]`
  - `sync.team_round(iv, entry, *, callers: dict[str, Caller], teams: dict[str, CallerTeam]) -> Optional[TeamRound]` — a TeamRound when the round's caller is enabled, `callerKind == "external"`, and that caller's `teamId` is a non-archived team; else None. Status is not checked here; `sync_round` reads it to decide "canceled".
  - `sync.sync_round(iv, entry_or_none, round_id: str, now: datetime, *, callers, teams) -> str` → action taken: `"posted"|"edited"|"replied"|"canceled"|"off"|"none"|"skipped"`
  - `sync.sync_team_posts(interview_id: str, *, now: Optional[datetime] = None) -> None` (never raises; logs)
  - `sync.sweep_team_posts(now: datetime) -> dict[str, int]` — every interview with an upcoming round that is a team round or has a `TeamRoundPost`
  - `blocks.scheduled_blocks(tr: TeamRound, state: str) -> tuple[str, list[dict]]` and `blocks.reply_text(kind: str, tr, *, previous: Optional[Caller] = None) -> str`

Behaviour = the spec's change table plus "Time TBD" / "Time set". State kept on `TeamRoundPost` (`callerId`, `startEpoch`, `state`, `filesFor`). Claim first: insert the `TeamRoundPost` (ts `""`) before `chat.postMessage`; on DuplicateKeyError return `"skipped"`; on Slack failure delete the claim. Files: upload when `filesFor != startEpoch` (or `-1` for TBD), after the post exists; a failed upload leaves `filesFor` unchanged so the sweep retries.

- [ ] **Step 1: Write failing tests** (fake `caller_teams.slack.api` / `upload_files`, record calls; mongomock)

```python
def test_assigning_member_posts_and_uploads(): ...         # 1 postMessage tagging <@omar> <@jamil>; upload 2 files in its thread
def test_second_sync_is_noop(): ...                        # "none", no Slack calls
def test_concurrent_syncs_post_once(): ...                 # asyncio.gather(sync x2) → one postMessage
def test_other_member_assigned_edits_and_replies(): ...    # chat.update + reply tagging new caller + manager
def test_time_moved_replies_and_new_brief(): ...
def test_time_first_set_from_tbd(): ...                    # reply "Time set: …" + new brief
def test_canceled_round(): ...                             # update to Canceled + tagged reply; then "none"
def test_moved_off_team(): ...                             # caller outside team → "Moved off this team" + reply tagging previous caller
def test_member_left_team_via_sweep(): ...                 # Caller.teamId cleared; sweep marks off
def test_archived_team_marks_off(): ...
def test_past_round_never_posted(): ...                    # start < now, no post → "skipped", no calls
def test_no_slack_id_uses_name(): ...                      # text contains "Bilal", no "<@>"
def test_token_unset_does_nothing(): ...                   # callers_configured False → no calls, no rows
def test_slack_failure_releases_claim(): ...               # postMessage raises → no TeamRoundPost left
def test_resume_missing_note_in_thread(): ...
def test_interview_save_starts_team_sync(): ...            # _sync_caller_posts schedules sync_team_posts; main #caller calls unchanged
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests_unit/test_caller_teams_sync.py -q` → FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** new file + `tests_unit/test_caller_post_sync.py tests_unit/test_coordination_*.py tests_unit/test_interview_caller.py -q` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat: team channel interview scheduled posts with brief and resume"`

---

### Task 5: Reminder channel — daily list and 30-minute reminders

**Files:**
- Create: `app/caller_teams/reminders.py`
- Modify: `app/slack_bot.py` `run_alert_tick` (isolated steps `run_team_day_posts(now)`, `run_team_reminders(now)`); `app/routers/interviews.py` background task from Task 4 also refreshes the team's day post (`refresh_team_day(team_id, now)`)
- Test: `tests_unit/test_caller_teams_reminders.py`

**Interfaces:**
- Consumes: Tasks 1, 4 (`team_round`, `TeamRoundPost` for the thread permalink via `chat.getPermalink`)
- Produces:
  - `reminders.team_rounds_for_day(team: CallerTeam, day: date) -> list[TeamRound]` (local day in `team.timezone`; starts only; by start)
  - `reminders.run_team_day_posts(now: datetime) -> dict[str, str]` — per team: post once from `dailyTime` until local day ends (reuse `slack_bot.caller_daily_due`), else edit in place; "No team interviews today." when empty
  - `reminders.refresh_team_day(team_id: str, now: datetime) -> None`
  - `reminders.run_team_reminders(now: datetime) -> dict[str, int]` — rounds with start in `(now, now + CALLER_REMINDER_LEAD]`; dedup key `team-reminder:<teamId>:<roundId>:<startEpoch>` via `slack_bot._reminder_claim` / `_reminder_release`

- [ ] **Step 1: Write failing tests**

```python
def test_day_post_waits_until_daily_time(): ...      # 05:59 Chicago → nothing; 06:00 → postMessage
def test_day_post_lists_only_team_rounds(): ...      # an individual caller's round absent
def test_day_post_local_day_edge(): ...              # 00:30 Chicago (06:30Z) round in that local day; previous-day 23:30 Chicago not
def test_day_post_edits_in_place(): ...              # second run → chat.update same ts
def test_empty_day_says_no_interviews(): ...
def test_reminder_once_per_round_and_start(): ...    # two ticks → one message, tags caller + manager, has thread link
def test_rescheduled_round_gets_new_reminder(): ...
def test_tbd_round_no_reminder_not_listed(): ...
def test_archived_team_skipped(): ...
def test_tick_isolates_team_failures(): ...          # team step raising does not stop main day post / reminders
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests_unit/test_caller_teams_reminders.py -q` → FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** new file + `tests_unit/test_caller_daily.py tests_unit/test_alerts_tick.py -q` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat: team reminder channel daily list and 30-minute reminders"`

---

### Task 6: App — callers' Slack ID, team, teams admin, picker tag

**Files (engineers_app, branch `feat/caller-teams` from `main`):**
- Modify: `src/api/endpoints.ts:1182-1206` (`Caller` gets `slackUserId`, `teamId`, `teamName`; `updateCaller` body gets `slackUserId?`, `teamId?`; add `CallerTeam`, `listCallerTeams`, `createCallerTeam`, `updateCallerTeam`, `getCallerTeamsConnection`, `testCallerTeam`)
- Modify: `src/pages/interviews/CallersDirectory.tsx` (Slack ID + Team columns for external callers when `canManage`; amber "No Slack ID: shown by name, not tagged" for team members without one)
- Create: `src/pages/interviews/CallerTeamsPanel.tsx` (below the directory; team cards: name, manager select of external callers, team channel ID, reminder channel ID, daily time; members as chips; Save, Archive, Send test post; connection line "Connected: <workspace> ✓" / "Not connected — set SLACK_CALLERS_BOT_TOKEN")
- Modify: `src/components/interview/CallerEditPanel.tsx` (option label `Omar · Jamil's group` when `teamName`)
- Test: `src/pages/interviews/CallersDirectory.test.tsx`, `src/pages/interviews/CallerTeamsPanel.test.tsx`

**Interfaces:**
- Consumes: Task 2 endpoints and payloads exactly.

- [ ] **Step 1: Write failing tests**

```tsx
it('saves a caller Slack ID and team', ...)          // updateCaller called with { slackUserId: 'U07Q3KD2M9A', teamId: 't1' }
it('flags a team member without a Slack ID', ...)
it('hides team and Slack ID controls without canManage', ...)
it('creates a team with manager and channels', ...)
it('shows connection state', ...)                    // both connected and not-connected texts
it('shows each channel result of Send test post', ...)
it('tags team members in the caller picker', ...)    // "Omar · Jamil's group"
```

- [ ] **Step 2: Run** `npx vitest run src/pages/interviews` → FAIL
- [ ] **Step 3: Implement** (reuse existing input/button classes from CallersDirectory; no new deps)
- [ ] **Step 4: Run** `npx vitest run && npm run lint` → all pass, lint clean
- [ ] **Step 5: Commit** `git commit -m "feat: caller teams admin and caller Slack IDs"`

---

### Task 7: Full verification

- [ ] Backend: `.venv/bin/pytest tests_unit -q` → only the 4 known failures from before this branch (2 `test_stats_bids`, 2 `test_bids_week`).
- [ ] App: `npx vitest run` and `npm run lint` clean.
- [ ] Run the architecture self-audit (global CLAUDE.md) and record its one-line verdict.
- [ ] Draft both PR bodies in the scratchpad (backend → `dev`, app → `main`, backend merges first; setup steps from the spec). Do not push until the user says so.
