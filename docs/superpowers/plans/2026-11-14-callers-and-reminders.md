# Callers and Interview Reminders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every app user can be a caller, callers get their own tab, confirmation works (from Slack or the app), and the existing daily caller channel carries an always-current morning post of all the day's rounds plus 30-minute reminders tagging whoever is on the call, each with Download brief / Open interview.

**Architecture:** A caller is either an external `Caller` document or an active `UserExtra`; a small directory module merges both and every caller lookup goes through it. Confirmation and assigned-caller access live in two focused backend modules used by both the REST routes and the Slack button. The Slack side moves out of the 939-line `slack_bot.py` into a new `app/interview_day/` package (load rounds → build Block Kit → post/update → remind). The frontend replaces the callers modal with a Callers tab and adds a brief deep link.

**Tech Stack:** FastAPI + Beanie (MongoDB), pytest + mongomock-motor + httpx ASGI clients (`tests_unit/`), Slack Web API (Block Kit, `block_actions`), React 18 + Vite + SWR + Tailwind (no frontend test runner: `npm run lint` = `tsc -b --noEmit`, `npm run build`).

**Spec:** `engineers_backend/docs/superpowers/specs/2026-11-14-callers-and-reminders-design.md`

## Before starting

- Work in **git worktrees** branched `feat/callers-and-reminders` from `origin/dev` (backend) and `origin/main` (app), checked 2026-11-14 at backend `b7836d4` / app `fd17206`. The main working copies hold week-old uncommitted, never-pushed caller WIP (follow-up Self rounds on the Callers tab, home assessments out of ranges; files dated 2026-11-07) on `fix/interviews-home-assessment` (backend) / `fix/callers-follow-up-rounds` (app). Leave it untouched; if it is revived later it rebases onto this work (overlap: `routers/interviews.py`, `CallerRoundsList.tsx`, `InterviewsPage.tsx`, `callerColors.ts`, `CallersLegend.tsx`).
- The Bidders page was reworked into one page on 2026-11-14 (`3c0d3d7`, `fd17206`); this plan doesn't touch Bidders, and every function/file it references was re-checked on the branches above.
- Backend tests: `cd engineers_backend && .venv/Scripts/python -m pytest tests_unit/<file> -q`. `tests_unit/conftest.py` blanks every Slack setting, so tests never post. Three `tests_unit` files fail at collection on pristine `dev` (`test_resume_experience_roles`, `test_style_serializer`, `test_template_annotate_removals`) and two fail only because the real `.env` is loaded; compare failure sets against `dev`, don't expect all green.
- Frontend checks: `cd engineers_app && npm run lint && npm run build`.

## Global Constraints

- Caller kinds: `"external"` (a `Caller` doc) and `"user"` (a `UserExtra`); "self" = round caller disabled; "TBD" = caller enabled, none assigned.
- Platform callers = `UserExtra` with `isActive != False`, every role.
- An interview's owner is never offered or accepted as the caller of their own interview's rounds.
- Who may confirm/unconfirm a caller round: admin, `interview_manager`, the interview's owner, the round's assigned **user** caller.
- Confirmation is cleared on reschedule (existing) and whenever the assigned caller changes (new).
- Reminders and the day post go to `SLACK_CALLER_DAILY_CHANNEL_ID` at `SLACK_CALLER_DAILY_TIME` in `SLACK_CALLER_DAILY_TIMEZONE` (defaults `05:00`, `America/Chicago`); no new settings.
- Home-assessment rounds (`InterviewStage.home_assessment`) are not calls: never listed or reminded. Ended statuses (`ENDED_STATUSES` = canceled, rejected) are not listed or reminded.
- Reminder lead: 30 minutes (`CALLER_REMINDER_LEAD`).
- People are tagged with `<@slackUserId>` only if they have one and are active; otherwise their plain name.
- Exact copy:
  - Slack refusal: `Only the caller or an interview manager can confirm.`
  - Badge tooltip (not confirmed): `Waiting for the caller to confirm`
  - TBD round label: `No caller yet`
  - Missing resume file for a caller: `The resume file is missing — ask the owner to restore it.`
  - Owner picked as own caller (400): `You take your own interview as Self — pick someone else as the caller.`
  - Delete external caller with rounds (409): `This caller has rounds — archive them instead.`
- Slack buttons: action_id `interview_confirm`, value `"<interviewId>:<roundId>"`. URL buttons use `get_settings().FRONTEND_ORIGIN`:
  - Download brief → `/interview/<interviewId>?brief=<roundId>`
  - Open interview → `/interviews?panel=<interviewId>`
  - Assign caller → `/interviews/callers?round=<interviewId>:<roundId>`

## Review Focus

1. A round moved off today (rescheduled to tomorrow, canceled, deleted) after the 05:00 post: the post must drop it on the next refresh — test in Task 8.
2. A Confirm click from a Slack user with no linked account, or a deactivated one: refused with the exact ephemeral copy, nothing written — test in Task 10.
3. An assigned user caller who was later deactivated: shown by name, not tagged, still listed — test in Task 7.
4. A busy day (30 rounds) must stay within Slack's 50-blocks-per-message limit by splitting into more messages, and a later refresh with fewer rounds deletes the surplus message — tests in Tasks 7 and 8.
5. A round rescheduled after its reminder was sent gets a fresh reminder at the new time, and a round edited within the 30-minute window is not reminded twice — test in Task 9.

---

## Backend (engineers_backend)

### Task 1: Caller directory (external callers + platform users)

**Files:**
- Modify: `app/models/interview.py` (`InterviewCaller`: add `callerKind: Literal["external", "user"] = "external"`)
- Modify: `app/models/caller.py` (`Caller`: add `archivedAt: Optional[datetime] = None`)
- Modify: `app/models/user_extra.py` (add `callerColor: Optional[str] = None`)
- Create: `app/caller_directory.py`
- Test: `tests_unit/test_caller_directory.py`

**Interfaces:**
- Produces:
  - `CallerEntry = TypedDict("CallerEntry", {"_id": str, "kind": Literal["external","user"], "name": str, "color": str, "archived": bool})`
  - `def user_caller_color(user_id: str, override: Optional[str]) -> str`
  - `async def list_directory(*, include_archived: bool = False) -> list[CallerEntry]` (users first, by name; then externals by `nameKey`)
  - `async def resolve_caller(caller_id: str) -> Optional[CallerEntry]` (active user or non-archived external; `None` otherwise)
  - `def user_display_name(user: UserExtra) -> str` (name, else email, else "User")

- [ ] **Step 1: Write the failing tests**

```python
async def test_directory_lists_active_users_then_external(db):
    # users: Ana (active), Bo (isActive=False); externals: Muhammad, Old (archivedAt set)
    entries = await list_directory()
    assert [(e["kind"], e["name"]) for e in entries] == [("user", "Ana"), ("external", "Muhammad")]
    assert [e["name"] for e in await list_directory(include_archived=True)] == ["Ana", "Muhammad", "Old"]

def test_user_color_is_stable_and_override_wins():
    assert user_caller_color("6ad7bb33695b5819321f5785", None) == user_caller_color("6ad7bb33695b5819321f5785", None)
    assert user_caller_color("6ad7bb33695b5819321f5785", "rose") == "rose"
    assert user_caller_color("6ad7bb33695b5819321f5785", "not-a-colour") in CALLER_COLORS

async def test_resolve_finds_users_and_externals_but_not_inactive_or_archived(db):
    assert (await resolve_caller(str(ana.id)))["kind"] == "user"
    assert (await resolve_caller(str(muhammad.id)))["kind"] == "external"
    assert await resolve_caller(str(bo.id)) is None and await resolve_caller(str(old.id)) is None
    assert await resolve_caller("not-an-id") is None
```

Fixture `db`: `AsyncMongoMockClient()["caller-dir"]` + `init_beanie(..., document_models=[UserExtra, Caller])`, as in `tests_unit/test_callers.py`.

- [ ] **Step 2: Run to verify they fail** — `pytest tests_unit/test_caller_directory.py -q` → ImportError on `app.caller_directory`.
- [ ] **Step 3: Implement** the model fields and `app/caller_directory.py`. Colour: `CALLER_COLORS[int(hashlib.sha1(user_id.encode()).hexdigest(), 16) % len(CALLER_COLORS)]` unless `override in CALLER_COLORS`.
- [ ] **Step 4: Run to verify they pass**, plus `pytest tests_unit/test_callers.py tests_unit/test_interview_caller.py -q` still pass (new fields have defaults).
- [ ] **Step 5: Commit** `feat(callers): caller directory of platform users and external callers`

### Task 2: Callers API on the directory (list, edit, archive, delete) + rename follow

**Files:**
- Modify: `app/routers/callers.py` (`list_callers`, `create_caller`, `update_caller`, `delete_caller`, `CallerIn`, `caller_out`, `_rename_on_rounds`)
- Modify: `app/routers/profile.py` and `app/routers/users.py` (call the rename follow when a user's name changes)
- Test: `tests_unit/test_callers.py` (extend)

**Interfaces:**
- Consumes: Task 1 `list_directory`, `user_caller_color`, `user_display_name`.
- Produces:
  - `GET /callers?archived=0|1` → `{"callers": list[CallerEntry], "canManage": bool}`
  - `PUT /callers/{id}` body `CallerIn {name?, color?, archived?: bool}`; for a **user** id only `color` is accepted (stored on `UserExtra.callerColor`), `name`/`archived` → 400
  - `DELETE /callers/{id}`: external with no rounds → 204; external with rounds → 409 (exact copy); user id → 400
  - `async def rename_on_rounds(caller_id: str, name: str) -> None` (public rename of `_rename_on_rounds`, used for users too)

- [ ] **Step 1: Write the failing tests** (in the existing `client` fixture style):
  - `test_list_includes_platform_users` — two active users + one external → `kind`s `["user","user","external"]`, `canManage` false for staff.
  - `test_archive_hides_from_list_and_unarchive_restores` — `PUT {archived: true}` → absent from `GET /callers`, present with `archived: true` in `GET /callers?archived=1`.
  - `test_delete_with_rounds_is_409` — an interview round with `callerId=str(ext.id)` → 409, detail `This caller has rounds — archive them instead.`; without rounds → 204.
  - `test_user_caller_only_takes_colour` — `PUT /callers/{user_id} {color:"teal"}` (as interview manager) → 200 and `UserExtra.callerColor == "teal"`; `{name:"X"}` → 400.
  - `test_user_rename_follows_onto_rounds` — a round with `callerKind="user", callerId=str(u.id), callerName="Ana"`; rename via `PUT /profile` → round's `callerName == "Ana B"`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** "Has rounds" = `Interview.find_one({"$or": [{"stageHistory.caller.callerId": id}, {"caller.callerId": id}]})`. Keep managers-only for writes (`require_caller_manager`).
- [ ] **Step 4: Run** `pytest tests_unit/test_callers.py -q` → pass.
- [ ] **Step 5: Commit** `feat(callers): archive external callers, list platform users, colour for users`

### Task 3: Round caller assignment through the directory

**Files:**
- Modify: `app/interview_caller.py` (`CallerIn`: add `callerKind: Optional[str] = None`; `build_round_caller` carries it; keep previous kind when caller unchanged)
- Modify: `app/routers/interviews.py` (`_resolve_caller`, `assign_round_caller`, `update_interview_stage`, `add_interview_stage`, `create_interview`)
- Test: `tests_unit/test_callers.py` (extend), `tests_unit/test_interview_caller.py` (extend)

**Interfaces:**
- Consumes: Task 1 `resolve_caller`.
- Produces: `async def _resolve_caller(body: Optional[CallerIn], *, owner_id: str) -> Optional[CallerIn]` — sets `callerId`, `callerName`, `callerKind`; 400 with the owner copy when `callerId == owner_id`; 400 `That caller is no longer on the list.` when unresolvable.

- [ ] **Step 1: Write the failing tests:**
  - `test_assign_platform_user_sets_kind_user` — `PUT /interviews/{id}/stages/{rid}/caller {callerId: str(ana.id)}` → round `caller.callerKind == "user"`, `callerName == "Ana"`.
  - `test_owner_cannot_be_own_caller` — owner's own id → 400, detail `You take your own interview as Self — pick someone else as the caller.`
  - `test_changing_caller_clears_confirmed` — round `confirmed=True`, assign another caller → `confirmed is False`; re-assigning the same caller keeps it `True`.
  - `test_build_round_caller_keeps_kind_when_caller_unchanged` (unit, `interview_caller.py`).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Compare `_assignment()` before/after in both `assign_round_caller` and the PATCH caller branch; set `entry.confirmed = False` when it changed.
- [ ] **Step 4: Run** `pytest tests_unit/test_callers.py tests_unit/test_interview_caller.py tests_unit/test_interview_rounds_endpoint.py -q` → pass.
- [ ] **Step 5: Commit** `feat(callers): assign platform users as callers; owner can't be own caller`

### Task 4: Caller statistics and the Callers-tab overview

**Files:**
- Modify: `app/caller_stats.py` (`_caller_rows` from the directory incl. archived externals; new `caller_overview`)
- Modify: `app/routers/callers.py` (`GET /callers/overview`, declared before `/{caller_id}`)
- Test: `tests_unit/test_caller_stats.py` (extend)

**Interfaces:**
- Consumes: Task 1 `list_directory(include_archived=True)`.
- Produces:
  - `async def caller_overview(now: datetime) -> dict[str, dict]` → `{callerId: {"upcoming": int, "thisWeek": int, "passRate": Optional[float], "rounds": int}}` where upcoming = scheduled caller rounds starting after `now`; thisWeek = caller rounds dated in the app-tz Mon–Sun week of `now`; passRate = passed/(passed+rejected) over the 90 days before `now` (`rate()`); rounds = all-time caller rounds (drives "can delete").
  - `GET /callers/overview` → `{"overview": {...}}` (any signed-in user)

- [ ] **Step 1: Write the failing tests:**
  - `test_rows_include_platform_callers` — a round with a user caller appears as a row `{"callerId": str(u.id), "name": "Ana", "color": <user colour>}`.
  - `test_overview_counts` — fixed `now`; one upcoming scheduled round, one passed + one rejected in the last 90 days, one passed 120 days ago → `upcoming == 1`, `passRate == 0.5`, `rounds == 4`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement**, reusing `merged_rounds`, `round_result`, `app_tz`.
- [ ] **Step 4: Run** `pytest tests_unit/test_caller_stats.py -q` → pass.
- [ ] **Step 5: Commit** `feat(callers): platform callers in caller stats; overview for the Callers tab`

### Task 5: Confirm a caller round (service + REST)

**Files:**
- Create: `app/caller_access.py`
- Create: `app/round_confirm.py`
- Modify: `app/routers/interviews.py` (new `POST /{iv_id}/stages/{stage_id}/confirm`)
- Test: `tests_unit/test_round_confirm.py`

**Interfaces:**
- Produces:
  - `caller_access.is_assigned_caller(iv: Interview, user: UserExtra) -> bool` — any round with `caller.enabled`, `callerKind == "user"`, `callerId == str(user.id)`
  - `caller_access.can_confirm(iv: Interview, entry: InterviewStageEvent, user: UserExtra) -> bool` — per Global Constraints
  - `class ConfirmError(Exception): status: int; message: str`
  - `async def confirm_round(iv_id: str, round_id: str, user: UserExtra, confirmed: bool) -> Interview` — 404 missing; 403 `Only the caller or an interview manager can confirm.`; 400 `Only caller rounds are confirmed` / `A canceled or rejected round can't be confirmed` / `Assign a caller first` (confirming a TBD round); saves, then `sync_caller_thread(iv, before)` (existing confirmed/unconfirmed thread event) and `await refresh_today()` (Task 8; until then import a stub that returns None — Task 8 replaces it)
  - `POST /interviews/{iv_id}/stages/{stage_id}/confirm` body `{"confirmed": bool}` → `_interview_out(iv)`

- [ ] **Step 1: Write the failing tests:**
  - `test_assigned_user_caller_can_confirm` → 200, round `confirmed is True`.
  - `test_interview_manager_can_confirm_external` → 200.
  - `test_other_staff_cannot_confirm` → 403 with the exact copy.
  - `test_tbd_round_cannot_be_confirmed` → 400 `Assign a caller first`.
  - `test_confirm_posts_thread_event` — `sync_caller_thread` (patched `AsyncMock`) awaited once with the pre-write snapshot.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Leave the PATCH `confirmed` path as is (owner/admin) so older clients keep working.
- [ ] **Step 4: Run** `pytest tests_unit/test_round_confirm.py tests_unit/test_interview_rounds_endpoint.py -q` → pass.
- [ ] **Step 5: Commit** `feat(interviews): callers and interview managers confirm caller rounds`

### Task 6: Assigned caller can view the interview and download its brief files

**Files:**
- Modify: `app/routers/interviews.py` (`_can_view` → also `is_assigned_caller`)
- Modify: `app/routers/resume.py` (`GET /jobs/{job_id}/download`: optional `interviewId` query)
- Modify: `app/routers/accounts.py` (`GET /{account_id}/resume-files`, `GET /{account_id}/resumes/{resume_id}/file`: optional `interviewId` query)
- Modify: `app/caller_access.py` (add `async def caller_interview(interview_id: Optional[str], user: UserExtra) -> Optional[Interview]` — the interview when `user` is its assigned caller, else None)
- Test: `tests_unit/test_caller_access.py`

**Interfaces:**
- Consumes: Task 5 `is_assigned_caller`.
- Produces: access rule — with `interviewId` given and `caller_interview()` non-None: the job download is allowed iff `str(iv.resumeLink.resumeJobId) == job_id`; account files iff `str(iv.accountId) == account_id`. `POST /resume/jobs/{id}/restore-pdf` unchanged (no caller access).

- [ ] **Step 1: Write the failing tests:**
  - `test_assigned_caller_can_get_interview` → 200; another staff → 403.
  - `test_caller_downloads_linked_resume_only_with_interview_context` → with `?interviewId=` 200 (patch `_fetch_pdf_bytes`); without it 404; a different job id with the same `interviewId` → 404.
  - `test_caller_lists_profile_resume_files_via_interview` → 200 with `interviewId`; for another profile → 404.
  - `test_caller_cannot_restore` → `POST /resume/jobs/{id}/restore-pdf` → 404.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests_unit/test_caller_access.py -q` → pass.
- [ ] **Step 5: Commit** `feat(interviews): assigned caller can open the interview and its brief files`

### Task 7: Day rounds and Block Kit builder (pure)

**Files:**
- Create: `app/interview_day/__init__.py`, `app/interview_day/rounds.py`, `app/interview_day/blocks.py`
- Test: `tests_unit/test_interview_day_blocks.py`

**Interfaces:**
- Consumes: `slack_bot.slack_time`, `_stage_label`, `_METHOD_LABELS`, `_profile_label`, `_slack_escape`, `_text`, `_aware_utc`; `ENDED_STATUSES`.
- Produces:
  - `@dataclass DayRound`: `interview_id: str, round_id: str, start: datetime (aware UTC), company: str, stage: str, profile: str, position: str, round_no: int, method: str, link: str, caller: Literal["self","tbd","external","user"], caller_name: str, caller_user: Optional[UserExtra], confirmed: bool, owner: Optional[UserExtra], thread_link: Optional[str]`
  - `async def load_rounds(start: datetime, end: datetime) -> list[DayRound]` — active rounds in `[start, end)` of every interview, sorted by start; excludes home assessments and ended statuses; `round_no` = 1-based index among the interview's rounds; DB only — `thread_link` stays `None` here and Task 8 fills it with `_caller_daily_permalink` for caller rounds
  - `def round_blocks(r: DayRound, *, managers: Sequence[UserExtra], origin: str, starting_in: Optional[int] = None) -> list[dict]` — a `section` (mrkdwn) + an `actions` block
  - `def day_messages(day: date, rounds: list[DayRound], *, managers, origin) -> list[list[dict]]` — header block + rounds; each message ≤ 50 blocks; no rounds → one message `"No interviews today."`
  - `def fallback_text(day: date, n: int) -> str` — `"Interviews today — Fri Nov 13 · 4 rounds"`

- [ ] **Step 1: Write the failing tests** (`SimpleNamespace` users like `test_caller_daily.py`):
  - `test_self_round_tags_owner_and_has_two_buttons` — text contains `Self · <@U0OWNER001>`; buttons `Download brief` url `https://app.test/interview/IV?brief=R`, `Open interview` url `https://app.test/interviews?panel=IV`.
  - `test_user_caller_tagged_with_owner_cc` — contains `<@U0CALLER01> (caller)` and `owner <@U0OWNER001>`; unconfirmed → a `Confirm` button with `action_id == "interview_confirm"`, `value == "IV:R"`; confirmed → `✅ confirmed`, no Confirm button.
  - `test_tbd_round_flags_managers_and_offers_assign` — contains `No caller yet` and `<@U0MANAGER1>`; `Assign caller` url `https://app.test/interviews/callers?round=IV:R`; no Confirm.
  - `test_external_unconfirmed_tags_managers` — `caller Pietro (external)` and `⏳ not confirmed — <@U0MANAGER1>`.
  - `test_inactive_or_unlinked_people_are_named_not_tagged` — caller user `isActive=False` or `slackUserId=None` → plain name, no `<@`.
  - `test_starting_in_header` — `starting_in=30` → text starts with `⏰ *Starting in 30 min*`.
  - `test_thirty_rounds_split_under_fifty_blocks` — 30 rounds → ≥ 2 messages, each `len(blocks) <= 50`, all 30 round ids present once.
  - `test_load_rounds_skips_home_assessment_and_ended` (mongomock).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Section text lines: `*{time} · {company} · {stage} · {profile}*`, then `{position} · Round {n} · {method}{ (link)}`, then the "on the call" line; append `· <thread_link|thread>` for caller rounds with a thread.
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat(slack): Block Kit rounds for the interview day post`

### Task 8: The day post — posted at 05:00, kept current

**Files:**
- Create: `app/models/interview_day_post.py` (`InterviewDayPost(TimestampedDocument)`: `day: str` (YYYY-MM-DD, unique index), `channel: str`, `ts: list[str]`); register in `app/models/__init__.py` and `app/db.py` `DOCUMENT_MODELS`
- Create: `app/interview_day/post.py`
- Modify: `app/slack_bot.py` (`run_alert_tick`: call `post.run_day_post(now)` instead of `run_caller_daily`; delete `run_caller_daily`, `caller_daily_text`, `_caller_daily_load`, `_caller_daily_people`, `_caller_daily_post`; keep `caller_daily_due`, `caller_daily_bounds`, `_caller_daily_configured`, `_caller_daily_permalink`)
- Modify: `app/round_confirm.py` (replace the Task 5 stub import with `post.refresh_today`)
- Modify: `app/routers/interviews.py` (`_sync_caller_posts`: after the thread sync, `await refresh_today()`; errors logged, never raised)
- Test: `tests_unit/test_interview_day_post.py`; update/remove superseded cases in `tests_unit/test_caller_daily.py`

**Interfaces:**
- Consumes: Task 7 `load_rounds`, `day_messages`, `fallback_text`; `slack_bot._slack_api`, `interview_managers`, `caller_daily_due`, `caller_daily_bounds`, `_caller_daily_configured`.
- Produces:
  - `async def run_day_post(now: datetime) -> str` — `"posted" | "not_due" | "already_sent" | "off" | "failed"`; claims `SlackAlertLog` key `interview-day:<YYYY-MM-DD>` (new key, so today's old `caller-daily:` row doesn't block); stores an `InterviewDayPost`
  - `async def refresh_today(now: Optional[datetime] = None) -> bool` — if today's `InterviewDayPost` exists: `chat.update` each stored ts with the rebuilt messages, `chat.postMessage` extra messages, `chat.delete` surplus ts, save the new ts list; False when there's no post or Slack is off
  - `async def today_parent_ts(now: datetime) -> Optional[str]` — first ts of today's post (Task 9 threads under it)

- [ ] **Step 1: Write the failing tests** (patch `_slack_api` with a recorder returning `{"ts": "<n>"}`):
  - `test_posts_once_per_day_and_stores_ts`.
  - `test_refresh_drops_round_moved_to_tomorrow` — post with 2 rounds, reschedule one to tomorrow, `refresh_today()` → one `chat.update` whose blocks contain only the other round.
  - `test_refresh_deletes_surplus_message` — 30 rounds posted as 2 messages, 25 canceled, refresh → one `chat.delete` and stored `ts` length 1.
  - `test_refresh_without_post_is_noop` → False, no Slack calls.
  - `test_interview_write_refreshes_today` — router PATCH of a round triggers `refresh_today` (patched `AsyncMock`, awaited once).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests_unit/test_interview_day_post.py tests_unit/test_caller_daily.py tests_unit/test_alerts_tick.py -q` → pass.
- [ ] **Step 5: Commit** `feat(slack): one current day post of all interviews in the daily channel`

### Task 9: 30-minute reminders in the day post's thread

**Files:**
- Create: `app/interview_day/reminders.py`
- Modify: `app/slack_bot.py` (`run_alert_tick`: `reminders.run_round_reminders(now)` replaces `run_caller_reminders`; delete `run_caller_reminders`, `caller_reminder_text`, `_reminder_post`, `_reminder_people`, `_reminder_load`, `due_caller_reminders`; keep `_reminder_claim`/`_reminder_release` or move them into `reminders.py`)
- Test: `tests_unit/test_interview_reminders.py`; remove superseded `tests_unit/test_caller_reminder.py` cases

**Interfaces:**
- Consumes: Task 7 `load_rounds`, `round_blocks`; Task 8 `today_parent_ts`.
- Produces: `async def run_round_reminders(now: datetime) -> dict[str, int]` (`{"sent", "failed"}`); key `round-reminder:<roundId>:<startEpoch>`; reply with `thread_ts` = today's parent ts, else top-level in the channel; `starting_in = ceil(minutes)` ≥ 1.

- [ ] **Step 1: Write the failing tests:**
  - `test_self_and_caller_rounds_both_reminded` — two rounds starting in 20 and 25 min → two posts, both with `thread_ts` of the day post.
  - `test_no_day_post_posts_top_level` — no `InterviewDayPost` → post without `thread_ts`.
  - `test_not_reminded_twice` — two ticks 5 min apart → one post.
  - `test_rescheduled_round_reminded_again_at_new_time` — reminded at 10:00 start, moved to 11:00 → reminded again at 10:30.
  - `test_failed_post_releases_claim` — Slack raises → claim released, next tick retries.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests_unit/test_interview_reminders.py tests_unit/test_alerts_tick.py -q` → pass.
- [ ] **Step 5: Commit** `feat(slack): 30-min reminders for every round, threaded under the day post`

### Task 10: Slack Confirm button

**Files:**
- Create: `app/interview_day/actions.py`
- Modify: `app/routers/slack_bot.py` (`slack_interactions`: dispatch `interview_confirm` to `process_confirm_click` in a background task, like transactions)
- Test: `tests_unit/test_interview_confirm_click.py`

**Interfaces:**
- Consumes: Task 5 `confirm_round`, `ConfirmError`; `slack_transactions._post_ephemeral` (or a shared copy).
- Produces: `async def process_confirm_click(value: str, slack_user_id: str, response_url: str) -> None` — maps `slackUserId` → active `UserExtra`; unknown/inactive → ephemeral refusal copy; `ConfirmError` → ephemeral `exc.message`; success → no message (the post refreshes via `confirm_round`).

- [ ] **Step 1: Write the failing tests:**
  - `test_caller_click_confirms` — round confirmed, no ephemeral.
  - `test_unknown_slack_user_refused` and `test_inactive_user_refused` — ephemeral text `Only the caller or an interview manager can confirm.`, round unchanged.
  - `test_router_dispatches_confirm_action` — signed `block_actions` payload with `action_id="interview_confirm"` → 200 and the background task scheduled (patch `process_confirm_click`).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests_unit/test_interview_confirm_click.py -q` → pass.
- [ ] **Step 5: Commit** `feat(slack): Confirm button on interview rounds`

### Task 11: Merge an external caller into a user (Nicholas)

**Files:**
- Create: `scripts/merge_caller_into_user.py` (pattern: `scripts/backfill_interview_resume_links.py`)
- Test: `tests_unit/test_merge_caller_into_user.py`

**Interfaces:**
- Produces: `async def merge_caller_into_user(caller_id: str, user_id: str, *, apply: bool) -> int` — counts rounds (stageHistory + top-level `caller`) with `callerId == caller_id`; with `apply`, sets `callerId=user_id`, `callerKind="user"`, `callerName=user_display_name(user)` and archives the external caller. CLI: `python -m scripts.merge_caller_into_user --caller-name Nicholas --user-name Nicholas [--apply]` (dry run by default, prints the count).

- [ ] **Step 1: Write the failing test** — 2 interviews with external "Nicholas" → dry run returns 2 and changes nothing; apply returns 2, rounds are `user` kind with the user id, the external caller has `archivedAt`.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `chore(callers): script to merge an external caller into a platform user`

---

## Frontend (engineers_app)

### Task 12: API types, caller hook and colours

**Files:**
- Modify: `src/api/endpoints.ts` (`Caller`: add `kind: 'external' | 'user'`, `archived: boolean`; `listCallers(archived?: boolean)`; `updateCaller` body adds `archived?: boolean`; new `getCallerOverview()`, `confirmRound(id, stageId, confirmed)`; `InterviewCaller` adds `callerKind?`; `downloadTailoredResume`, `listAccountResumeFiles`, `getAccountResumeFileUrl` take an optional `interviewId`)
- Modify: `src/lib/useCallers.ts` (return `callers` (active), `people`, `external`, `byId`, `canManage`)
- Modify: `src/lib/callerColors.ts` (add `callerInitials(name: string): string`; chips show initials + name)

**Interfaces:**
- Produces: `type CallerOverview = Record<string, { upcoming: number; thisWeek: number; passRate: number | null; rounds: number }>`; `confirmRound = (id: string, stageId: string, confirmed: boolean) => postJSON<InterviewRecord>(\`/interviews/${id}/stages/${stageId}/confirm\`, { confirmed })`.

- [ ] **Step 1:** Make the type changes; fix every compile error they surface.
- [ ] **Step 2: Verify** `npm run lint` → exit 0.
- [ ] **Step 3: Commit** `feat(callers): API types for platform callers, confirm and overview`

### Task 13: Grouped caller picker (round form, caller dialog)

**Files:**
- Modify: `src/components/interview/CallerFields.tsx`, `src/components/interview/CallerRoundDialog.tsx`
- Modify: `src/components/interview/InterviewForm.tsx` (pass the interview owner's id to `CallerFields`)

**Interfaces:**
- Consumes: Task 12 `useCallers().people / external`.
- Produces: options in order `Not assigned yet` · **People** (excluding the owner; the current user first) · **External callers`; a saved caller that's off the list stays as `<name> (not on the list)`. Group headers as disabled, visually distinct options (the shared `Select` has no optgroup).

- [ ] **Step 1:** Implement.
- [ ] **Step 2: Verify** `npm run lint` → 0; manual (Task 18): the owner is not offered; picking a person saves `callerKind: "user"`.
- [ ] **Step 3: Commit** `feat(callers): pick app users or external callers`

### Task 14: Interviews tabs and the Callers tab

**Files:**
- Modify: `src/components/InterviewTabs.tsx` (`Interviews` `/interviews` · `Caller schedule` `/interviews/callers` · `Callers` `/interviews/callers/list`; match rules so `/interviews/callers/calendar` is Caller schedule)
- Modify: `src/App.tsx` (route `/interviews/callers/list` → `InterviewsPage view="list" directory`)
- Modify: `src/pages/interviews/InterviewsPage.tsx` (`directory` prop renders the new tab; remove `CallersManager` and its state)
- Create: `src/pages/interviews/CallersDirectory.tsx`
- Create: `src/components/interview/CallerEditPanel.tsx` (add/edit external: name + colour; user: colour only)
- Modify: `src/components/interview/CallersLegend.tsx` (remove "Manage callers")
- Delete: `src/components/interview/CallersManager.tsx`
- Modify: `src/pages/Statistics.tsx` (read/write `?tab=` and pass `?caller=` to `CallersTab` as the preselected caller)

**Interfaces:**
- Consumes: Task 12 `listCallers`, `getCallerOverview`, `updateCaller`, `createCaller`, `deleteCaller`.
- Produces: table sections "People in the app" and "External callers" (+ "Show archived"); columns colour, name, Upcoming, This week, Pass rate (`<n>% →` link `/statistics?tab=callers&caller=<id>`), ⋯ (managers only: Edit; externals: Archive/Unarchive, Delete only when `rounds === 0`). Row click → `/interviews/callers?caller=<id>`.

- [ ] **Step 1:** Implement.
- [ ] **Step 2: Verify** `npm run lint && npm run build` → 0; manual (Task 18): staff sees no ⋯; archive hides the caller from the picker; pass-rate link opens Statistics on the Callers tab with that caller.
- [ ] **Step 3: Commit** `feat(callers): Callers tab replaces the callers modal`

### Task 15: Confirmation in the app

**Files:**
- Modify: `src/components/interview/ConfirmedBadge.tsx` (tooltip copy; new `NoCallerBadge` "No caller yet")
- Modify: `src/components/interview/InterviewSummary.tsx`, `src/components/interview/InterviewPanel.tsx`, `src/components/interview/CallerRoundDialog.tsx`, `src/pages/interviews/CallerRoundsList.tsx`, `src/pages/interviews/InterviewsList.tsx`
- Create: `src/lib/canConfirm.ts`: `canConfirm(user, interview, round): boolean` (admin, interview_manager, owner, `round.caller.callerKind === 'user' && round.caller.callerId === user.id`)

- [ ] **Step 1:** Show "No caller yet" instead of "Not confirmed" for TBD rounds; show Mark confirmed / Unconfirm when `canConfirm`, calling `confirmRound`; hide it for TBD rounds.
- [ ] **Step 2: Verify** `npm run lint` → 0; manual (Task 18): an interview manager confirms from the Caller schedule dialog.
- [ ] **Step 3: Commit** `feat(interviews): callers and managers confirm from the app`

### Task 16: Brief deep link, caller view, and returning after sign-in

**Files:**
- Modify: `src/auth/AuthGuard.tsx` (`<Navigate to="/login" state={{ from: location.pathname + location.search }} replace />`)
- Modify: `src/pages/Login.tsx` (after sign-in navigate to `state.from` when it starts with `/` and not `//`, else `/dashboard`)
- Modify: `src/pages/InterviewFocus.tsx` (on load with `?brief=<roundId>`, run the brief download for that round once, then drop the param)
- Modify: `src/components/interview/useBriefDownload.tsx` (pass `interviewId` to the resume calls; when the viewer is the assigned caller and the file is gone, show the missing-file copy instead of the restore action)
- Modify: `src/pages/interviews/InterviewsPage.tsx` (on Caller schedule, `?round=<iv>:<round>` opens `CallerRoundDialog` for that round)

- [ ] **Step 1:** Implement.
- [ ] **Step 2: Verify** `npm run lint && npm run build` → 0; manual (Task 18): signed out, open `/interview/<id>?brief=<round>` → sign in → lands there and the brief downloads; as the assigned caller (staff) the interview opens read-only.
- [ ] **Step 3: Commit** `feat(interviews): Download brief link from Slack, open for the assigned caller`

### Task 17: Account settings — time zone always visible

**Files:**
- Modify: `src/pages/Profile.tsx` (render the time zone field outside the "Slack linked" branch; when no Slack member ID, show "Add your Slack member ID to be tagged in interview reminders.")

- [ ] **Step 1:** Implement.
- [ ] **Step 2: Verify** `npm run lint` → 0.
- [ ] **Step 3: Commit** `fix(profile): time zone setting shown without Slack`

---

### Task 18: Verify end to end, then ship

- [ ] **Step 1:** Backend: `pytest tests_unit -q` on the branch and on `dev`; the failure sets must match except for intentionally removed tests.
- [ ] **Step 2:** Frontend: `npm run lint && npm run build` → 0.
- [ ] **Step 3:** Run both apps locally against a throwaway Mongo (never prod; see the test-environment notes) with Slack settings blank; click through Tasks 13–17's manual checks.
- [ ] **Step 4:** Slack check without the live channel: point `SLACK_CALLER_DAILY_CHANNEL_ID` at a private test channel the bot is in, run `python -c "import asyncio, datetime; from app.interview_day.post import run_day_post; print(asyncio.run(run_day_post(datetime.datetime.now(datetime.timezone.utc))))"`, check the post, click Confirm, reschedule a round and see the post update.
- [ ] **Step 5:** Open PRs (backend → `dev`, app → `main`) only when the user asks; after deploy, run the Nicholas merge: dry run, show the count to the user, then `--apply` with their OK.
