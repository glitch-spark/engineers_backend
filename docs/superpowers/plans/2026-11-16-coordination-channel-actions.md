# Coordination Channel Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The coordination channel (`SLACK_CALLER_CHANNEL_ID`) becomes a place to act. Each interview has one short post, edited in place, with Choose caller, Confirm, Download brief and Open interview. After a caller round ends, its owner is asked how it went (Completed / Canceled / Rescheduled…). A completed round offers Passed / Rejected. Results are chased by a 7-day reminder and a Friday 18:00 Chicago roundup. Interview managers can update status, reason and time on any interview, in Slack and in the app.

**Architecture:** A new package `app/coordination/` holds the Block Kit builders (`blocks.py`), the modal views and their parsing (`modals.py`), the click and form handlers (`actions.py`) and the cron work (`chase.py`). Every Slack action calls the same router functions the app calls: `update_interview_stage`, `assign_round_caller` and `confirm_round`. Each Slack action therefore saves the interview and calls `_sync_caller_posts` exactly as an app write does, and that call re-renders the post. The signed `POST /integrations/slack/interactions` endpoint gains coordination `block_actions` routing and `view_submission` handling. Modals open with `views.open` and the click's `trigger_id`. The 5-minute `run_alert_tick` gains one isolated step, `run_coordination_chase`. Claims for once-only messages reuse `SlackAlertLog` through `_reminder_claim`. The only new collection holds the Friday roundup's message so it can be edited in place.

**Tech Stack:** FastAPI + Beanie (Python 3.13), pytest + mongomock-motor (`tests_unit/`), Slack Block Kit and Web API (`chat.postMessage`, `chat.update`, `chat.postEphemeral`, `views.open`); React 18 + TS + vitest for the app gating (one task).

**Spec:** `docs/superpowers/specs/2026-11-16-coordination-channel-actions-design.md`

## Before starting

- **Order and branches:** this plan runs **after the Bids page plan** (`wt/app-reorg/docs/superpowers/plans/2026-11-16-bids-page-redesign.md`), on the same branches: backend `wt/backend-reorg` and app `wt/app-reorg`, both on `feat/app-reorg`. Precondition: in `wt/backend-reorg`, `grep -c '"/clock"' app/routers/bids.py` prints at least `1`, which shows the Bids page work is in. If it does not, stop and tell the controller. Both ship in the same release.
- This plan and the spec live in `wt/backend-reorg/docs/superpowers/`. Tasks 1 and 3–10 touch only the backend worktree. Task 2 touches only the app worktree. Commit each task in the repo it touched.
- **Backend tests**, from `wt/backend-reorg`: `"C:/Users/Administrator/Downloads/engineer - Copy/engineers_backend/.venv/Scripts/python.exe" -m pytest tests_unit/<file> -q -p no:cacheprovider`. The worktree's `.env` changes the results of two tests: `tests_unit/test_bidder_ext.py::test_upload_url_without_storage_is_503` and `tests_unit/test_resume_prompts.py::test_apply_prompts_rejects_bad_lists[prompts5-new-1]`. Move it aside for every suite run and put it back afterwards: `mv .env ../backend-reorg.env.off` … `mv ../backend-reorg.env.off .env`.
- **App checks**, from `wt/app-reorg`: `npx vitest run <files>`, `npm run lint`, `npm test`, `npm run build`.
- **Baseline:** in Task 1 Step 1, run the full backend suite (`tests_unit/`) and `npm test` once before any change. Write the failure list into Task 1's commit message. Every later full run must show the same set.
- Tests never reach Slack. Patch `app.slack_bot._slack_api` with a recorder, patch `_post_ephemeral` where it is looked up (`app.coordination.actions._post_ephemeral`), and patch `interviews.refresh_today` as `tests_unit/test_interview_confirm_click.py` does. Its fixtures (`db`, `make_user`, `make_interview`, `RESPONSE_URL`) are the model for the new test files. The signed-request helpers (`signed`, `click_body`) come from `tests_unit/test_slack_interactions.py`.

## Global Constraints

- **Channel and post.** Each interview has one post in `SLACK_CALLER_CHANNEL_ID`, stored as `Interview.slackThread` (`channel`, `ts`). It describes the interview's latest live round and is rewritten with `chat.update` (`text` + `blocks`) on every change. **No change-log thread replies.** The thread holds only the after-call ask and the 7-day result reminder. Old text-only posts become block posts on their next change; there is no migration.
- **Action ids** (constants in `app/coordination/blocks.py`):
  - `coord_choose_caller` (static select)
  - `coord_confirm`
  - `coord_open_brief` and `coord_open_interview` (URL buttons; the server acknowledges them and does nothing)
  - `coord_after_completed`, `coord_after_canceled`, `coord_after_rescheduled`
  - `coord_result_passed`, `coord_result_rejected`

  `COORD_ACTIONS` is the set of these ids minus the two URL buttons.
- **Callback ids** (in `app/coordination/modals.py`): `coord_cancel`, `coord_reschedule`, `coord_reject`, collected in `COORD_VIEWS`.
- **Values.** A button's value is `"<interviewId>:<roundId>"` (the "ref"). A caller select option's value is `"<interviewId>:<roundId>:<callerId>"`, or `"<interviewId>:<roundId>:self"` for "No caller (Self)". Round ids are 32 hex characters and object ids are 24, so a value is at most 82 characters (Slack's limit is 150).
- **Modal `private_metadata`:** JSON `{"ref": "<iv>:<round>", "channel": "<C…>", "ts": "<clicked message ts>", "tz": "<IANA zone or empty>"}`.
- **Who may do what.** The clicker is the active `UserExtra` whose `slackUserId` equals the Slack user id. If there is none, the reply is **"Link your Slack in Account settings first."**
  - **Choose caller:** `is_caller_manager(user)` (admin or interview manager) uses `assign_round_caller`. Otherwise the interview's owner uses `update_interview_stage` with a `caller` body. Anyone else gets **"Only the owner or an interview manager can assign the caller."**
  - **Confirm:** `confirm_round` and its own rule. The refusal is `NOT_ALLOWED`, "Only the caller or an interview manager can confirm."
  - **After-call answers and Passed / Rejected:** `can_update_round(iv, user)` (owner, admin, interview manager; active). Anyone else gets **"Only the owner or an interview manager can update this round."** The round's caller is not asked and may not answer unless they also hold one of those roles.
- **Other fixed copy:**
  - `TRY_AGAIN` = "Something went wrong. Try again from the app."
  - `FORM_FAILED` = "Couldn't open the form — click again."
  - `ROUND_GONE` = "This round is gone."
  - `ALREADY` = "Already updated: {Label}." (Label is the status capitalized)
  - `NEEDS_COMPLETED` = "Mark the round completed first."
  - An `HTTPException` raised by a router function reaches the clicker as its `detail` text.
- **Every click gets a visible result.** Either the clicked message changes (post re-render, `replace_original`, or `chat.update`), or the clicker gets an ephemeral reply. Handlers never raise; an unexpected error replies `TRY_AGAIN`.
- **Caller rounds only.** All three chasers (after-call ask, 7-day reminder, roundup) require all of the following:
  - `caller.enabled` is true
  - the stage is not `home_assessment`
  - the interview has a `slackThread`
  - the round's end (`round_ends_at(start, endsAt)`, which is `endsAt` when it is after the start, else start + 1 h) is **after** the release cutoff
- **Release cutoff setting:** `SLACK_CALLER_ASKS_FROM`, optional, `"YYYY-MM-DDTHH:MM"` in `America/Chicago`. When it is unset, all three chasers are off, so deploying changes nothing until it is set. At release it is set to the deploy time.
- **Claim keys** (`slack_bot._reminder_claim` / `_reminder_release`, claim before posting, release on failure):
  - `after-call:<roundId>:<startEpoch>` (a rescheduled round is asked again after its new end)
  - `result-reminder:<roundId>`
  - `result-roundup:<YYYY-MM-DD>` (the Friday's local date)
- **Timing.**
  - The after-call ask is due once `end <= now`.
  - The result reminder is due once `end + 7 days <= now` and the status is still `completed`.
  - The roundup is due on Friday from 18:00 `America/Chicago` until that local day ends.
- **Times in Slack** are `slack_time(...)` date tags. The reschedule form reads date and time in the **owner's** zone (`resolve_user_tz(owner)`), and its label names that zone.
- **Slack limits:**
  - ≤ 3,000 characters per section (pass every section text through `interview_day.blocks._cap`), and ≤ 50 blocks per message.
  - Caller select: `option_groups`, each ≤ 100 options; option text ≤ 75 characters (cut with "…").
  - Modal title ≤ 24 characters; `private_metadata` ≤ 3,000 characters.
- **Imports.** `app/slack_bot.py` imports `app.coordination.*` only inside functions (as `run_alert_tick` already does for `interview_day`), because `app/coordination/blocks.py` imports `app/interview_day/blocks.py`, which imports `app/slack_bot.py`. `app/coordination/actions.py` imports `app.routers.interviews` at call time, as `round_confirm.py` does.
- The interview model is unchanged. Statuses stay `scheduled | completed | passed | rejected | canceled`, and "Rescheduled" is a time change that keeps `scheduled`. The reschedule note and the cancel / reject reasons are stored in `statusNote`.

## Review Focus

1. An interview manager can change only `status`, `statusNote`, `scheduledAt` and `endsAt` on someone else's round, and a staff non-owner can change nothing. Tests: Task 1 `test_manager_cannot_touch_other_round_fields` and `test_staff_still_cannot_edit_others_rounds`.
2. An owner's Slack caller pick keeps the round's method and link; `build_round_caller` would otherwise wipe them. Test: Task 6 `test_owner_picks_a_caller_through_the_round_field`.
3. Each caller round gets one ask, only after its end and only past the cutoff. Tests: Task 7 `test_after_call_ask_once_after_the_end` and `test_not_asked`.
4. The reschedule form reads the owner's zone, across DST too, and the round moves exactly as the app's reschedule does. Tests: Task 8 `test_read_reschedule_in_the_owners_zone` and `test_reschedule_across_dst`, and Task 9 `test_reschedule_submission_moves_the_round`.
5. The roundup stays within 50 blocks and drops answered rounds by editing in place, never re-posting. Tests: Task 10 `test_roundup_block_limit` and `test_answered_round_drops_off`.

---

### Task 1: Who may edit a round (backend permissions)

**Files:**
- Modify: `app/caller_access.py` (add `can_update_round`)
- Modify: `app/routers/interviews.py`:
  - `_load_editable` (`:499`) stays for every other caller
  - `update_interview_stage` (`:1315`): its loader, and the caller-change 403 at `:1372-1377`
  - `CallerAssignIn` and `assign_round_caller` (`:1278-1312`)
- Create: `tests_unit/test_interview_manager_round_edit.py` (fixtures as `tests_unit/test_callers.py`: `db`, `client(user)`, `make_user`, `make_interview`, `rid`)
- Modify: `tests_unit/test_callers.py`

**Interfaces:**
- Produces: `def can_update_round(iv: Interview, user: UserExtra) -> bool`. It is false for `user.isActive is False`. Otherwise it is true for `user.role in MANAGER_ROLES` or for the interview's owner.
- Produces: `MANAGER_ROUND_FIELDS: frozenset[str] = frozenset({"status", "statusNote", "scheduledAt", "endsAt"})`.
- Produces: `MANAGER_FIELDS_ONLY = "Interview managers can change only the status, its reason and the time of someone else's round."`
- Produces: `async def _load_round_editable(iv_id: str, user: UserExtra, fields: set[str]) -> Interview`. An admin or the owner passes as today. An active `interview_manager` passes when `fields <= MANAGER_ROUND_FIELDS`, and gets a 403 with `MANAGER_FIELDS_ONLY` otherwise. Anyone else gets a 403 `"forbidden"`, and a missing interview a 404 `"not found"`. `update_interview_stage` calls it with `body.model_fields_set`.
- Changes: in `update_interview_stage`, the owner may change the caller assignment. Delete the `"Only Admin or an interview manager can change the caller."` check: everyone who can now send `caller` is an owner or an admin.
- Changes: `class CallerAssignIn(BaseModel): callerId: str = ""; enabled: Optional[bool] = None`. Old clients do not send `enabled`; for them the behaviour is unchanged, including the 400 "This round has no caller request." on a round whose caller request is off.
  - `enabled=False` turns the request off. The caller sub-document is kept with `enabled=False`, `callerId` is ignored, and `confirmed` becomes `False`.
  - `enabled=True` on a round with no caller request, or with one that is off, turns the request on with the picked caller. The new caller is `(previous or InterviewCaller()).model_copy(update={enabled: True, startsAt: entry.scheduledAt, callerId, callerName, callerKind})` from `_resolve_caller`, and `confirmed` becomes `False`.
- Unchanged: add round, delete round, re-extract and `PUT /interviews/{id}` keep `_load_editable` (owner or admin only).

- [ ] **Step 1:** Baseline. Run the full backend suite with `.env` moved aside, and `npm test` in `wt/app-reorg`. Note the failures for the commit message.
- [ ] **Step 2: Failing tests** in `test_interview_manager_round_edit.py`. Puma is `role="interview_manager"`, the owner is staff, and the round starts `2026-10-09T15:00:00Z` with `endsAt` 16:00Z.
  - `test_manager_sets_status_and_note_on_anyones_round`: Puma PATCHes `{"status": "canceled", "statusNote": "Recruiter canceled"}`. Expect 200, stored status `canceled` and statusNote `"Recruiter canceled"`.
  - `test_manager_reschedules_anyones_round`: on a confirmed round, Puma PATCHes `{"scheduledAt": "2026-10-10T16:00:00Z"}`. Expect 200, a start of 2026-10-10 16:00, `endsAt` 17:00 (the duration is kept) and `confirmed is False`.
  - `test_manager_cannot_touch_other_round_fields`: Puma PATCHes `{"note": "x"}`, then `{"status": "passed", "caller": {"enabled": False}}`, then `{"stage": "tech_round_1"}`. Each returns 403 with detail `MANAGER_FIELDS_ONLY`, and the stored round is unchanged.
  - `test_staff_still_cannot_edit_others_rounds`: another staff user PATCHes `{"status": "completed"}`. Expect 403 `"forbidden"`.
  - `test_inactive_manager_refused`: Puma with `isActive=False` PATCHes `{"status": "completed"}`. Expect 403.
  - `test_manager_still_cannot_add_or_delete_rounds`: Puma `POST /interviews/{id}/stages` gets 403, and `DELETE /interviews/{id}/stages/{rid}` gets 403.
  - `test_can_update_round`: true for the owner, an admin and an interview manager; false for a staff stranger, for the round's user caller and for an inactive manager.
- [ ] **Step 3: Failing tests** in `test_callers.py`:
  - Replace `test_creator_cannot_change_an_assigned_caller_but_can_edit_the_round` with `test_owner_changes_the_caller_in_the_round_form`. The owner PATCHes `{"caller": {"enabled": True, "callerId": jamil}}` on a confirmed Pietro round. Expect 200, callerName `"Jamil's group"` and `confirmed is False`. Keep its second half: a body without `callerId` keeps the assignment.
  - `test_manager_turns_the_caller_request_off_and_on`:
    - Puma PUTs `{"callerId": "", "enabled": False}`. Expect 200, `caller.enabled is False`, callerName still `"Pietro"` and `confirmed is False`.
    - Puma PUTs `{"callerId": muhammad, "enabled": True}` on the same round. Expect 200, enabled, callerName `"Muhammad"` and `startsAt == scheduledAt`.
    - Puma PUTs `{"callerId": muhammad}` (no `enabled`) on the `caller=False` interview. Expect 400 `"This round has no caller request."`.
- [ ] **Step 4:** Run both files and confirm they FAIL.
- [ ] **Step 5:** Implement.
- [ ] **Step 6:** Run both files, plus `test_interview_stages.py`, `test_interview_rounds_endpoint.py` and `test_round_confirm.py`. Expect PASS.
- [ ] **Step 7:** Commit `feat(interviews): interview managers set status, reason and time on any round; owners reassign their caller` (body: the baseline failure list).

### Task 2: App gating matches (app worktree)

**Files (all in `wt/app-reorg/src`):**
- Modify: `lib/canConfirm.ts` (add `canUpdateRound`)
- Create: `lib/canConfirm.test.ts`
- Modify: `components/interview/InterviewPanel.tsx` (`canEdit` at `:86`, `setRoundStatus`, the `InterviewSummary` props at `:196-201`)
- Modify: `components/interview/InterviewSummary.tsx` (the round status select at `:316` and the round `ActionMenu` at `:330`)
- Modify: `pages/interviews/InterviewsList.tsx` (`statusCell` at `:128`)
- Modify: `components/interview/InterviewForm.tsx` (`callerEditable` at `:79`)
- Create: `components/interview/RescheduleDialog.tsx`, `components/interview/RescheduleDialog.test.tsx`

**Interfaces:**
- Produces: `export function canUpdateRound(user: Who, interview: Pick<Interview, 'createdBy' | 'summaryOnly'>): boolean`. It is false without a user or for `summaryOnly`. It is true for `admin`, for `interview_manager`, and for the owner.
- `InterviewSummary` gains the props `canSetStatus: boolean` and `onReschedule?: (roundId: string) => void`.
  - The round status `<select>` is shown when `canSetStatus` (was `canEdit`).
  - The round `ActionMenu` adds `{ label: 'Change time', onSelect: () => onReschedule(e.id) }` when `canSetStatus && !canEdit`. Owners and admins keep "Edit round".
- `InterviewPanel`: `const canSetStatus = !!iv && canUpdateRound(user, iv)`. It opens `RescheduleDialog` for `onReschedule`.
- `InterviewsList.statusCell` uses `canUpdateRound(user, iv)` instead of `canEdit(iv)`. The row menu (Add next round / Edit / Delete) stays owner and admin only.
- `RescheduleDialog({ open, interview, roundId, tz, onClose, onSaved }: { open: boolean; interview: Interview; roundId: string; tz: string; onClose: () => void; onSaved: (saved: Interview) => void })`:
  - It has a date input and a time input, initialised with `zonedDateKey(parseServerTime(entry.scheduledAt), tz)` and `zonedTime(...)`.
  - Save sends `api.updateInterviewStage(interview._id, roundId, { scheduledAt: fromZoned(date, time, tz).toISOString() })`, then calls `notify.success('Time updated')` and `onSaved`. Errors go through `notify.error(err, 'Failed to change the time')`.
- `InterviewForm`: `callerEditable = mode !== 'editRound' || user?.role === 'admin' || user?.role === 'interview_manager' || ownerId === user?.id`. Move the `ownerId` computation above it. The comment becomes "The owner, Admin and interview managers assign the caller."

- [ ] **Step 1: Failing tests:**
  - `canConfirm.test.ts` `canUpdateRound`: admin true; interview manager on someone else's interview true; owner (as a string `createdBy` and as `{ _id }`) true; staff stranger false; `summaryOnly` false; `null` user false.
  - `RescheduleDialog.test.tsx` `sends the new time in the given zone`: with `tz='America/Chicago'`, type date `2026-11-20` and time `14:30`, then Save. `updateInterviewStage` is called with `('iv1', 'r1', { scheduledAt: '2026-11-20T20:30:00.000Z' })`.
  - `RescheduleDialog.test.tsx` `opens on the round's current time`: `scheduledAt` `2026-11-16T18:30:00Z` with `tz='America/New_York'` shows `2026-11-16` and `13:30`.
- [ ] **Step 2:** `npx vitest run src/lib/canConfirm.test.ts src/components/interview/RescheduleDialog.test.tsx` should FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `npm run lint && npm test && npm run build` should exit 0, with the baseline failure set only.
- [ ] **Step 5:** Commit (app repo) `feat(interviews): interview managers set status and time on any interview; owners reassign their caller`.

### Task 3: The short post (pure blocks)

**Files:**
- Create: `app/coordination/__init__.py` (empty docstring module), `app/coordination/blocks.py`
- Modify: `app/interview_day/rounds.py`: add `DayRound.status`, and extract `day_round` from the loop in `load_rounds`. `load_rounds` then calls it, with no behaviour change.
- Test: `tests_unit/test_coordination_blocks.py`

**Interfaces:**
- Produces in `rounds.py`:
  - `DayRound.status: str = "scheduled"`, set to `interview_status_value(entry.status) or "scheduled"`.
  - `def day_round(iv: Interview, no: int, entry: InterviewStageEvent, *, owner: Optional[UserExtra], account: Optional[Account], caller_user: Optional[UserExtra]) -> DayRound`.
- Produces in `blocks.py`: the action id constants from Global Constraints, plus `COORD_ACTIONS`, `SELF = "self"`, `MAX_GROUP_OPTIONS = 100` and `MAX_OPTION_TEXT = 75`.
- `def caller_select(r: DayRound, directory: Sequence[CallerEntry]) -> dict`:
  - Element: `{"type": "static_select", "action_id": "coord_choose_caller", "placeholder": "Choose caller", "option_groups": [...]}`.
  - The groups, in order:
    - "People": `kind == "user"` entries, without the interview's owner (`r.owner.id`), at most 100.
    - "External callers": `kind == "external"` entries, at most 100.
    - "No caller": one option, "No caller (Self)", with value `f"{ref}:self"`.
  - An empty group is left out. Option texts go through `_short(name, 75)`.
  - `initial_option` is the option whose value equals the current caller's, or the Self option when `r.caller == "self"`. It is absent when the current caller is not listed (archived, deactivated, or "tbd").
- `def post_blocks(r: DayRound, *, directory: Sequence[CallerEntry], managers: Sequence[UserExtra], origin: str) -> tuple[str, list[dict]]`. It returns `(fallback text, [section, actions])`.
  - Section line 1:
    - `f"📞 *{company} · {_stage_label(stage)} · {slack_time(start)} · {profile}*"`
    - for status `canceled` or `rejected`: `f"❌ ~{company} · {stage} · {time} · {profile}~"`
  - Section line 2: `f"Owner {_who(owner, '—')} · {caller part}"`. The caller part is:
    - user or external caller: `f"Caller: {_who(caller_user, caller_name)} ✅ confirmed"` or `"… ⏳ not confirmed"`, then `f" · {_method_part(r)}"` when there is a method or link
    - tbd: `f"⚠️ No caller yet — {_tags(managers)}"`, or just `"⚠️ No caller yet"` when there are no managers
    - self: `"No caller (Self)"`
  - Section line 3, only when the status is not `scheduled`:
    - completed: `"Completed · waiting for the result"`
    - passed: `"🎉 Passed"`
    - rejected: `"❌ Rejected"`
    - canceled: `"❌ Canceled"`
  - Actions:
    - scheduled: `[select, Confirm (only when caller in {"user", "external"} and not confirmed; style primary; value ref), Download brief, Open interview]`
    - completed: `[Passed (coord_result_passed, primary), Rejected (coord_result_rejected, danger), Download brief, Open interview]`
    - passed, rejected or canceled: `[Download brief, Open interview]`
  - URL buttons:
    - Download brief: `f"{origin}/interview/{iv}?brief={round}"`, action_id `coord_open_brief`
    - Open interview: `f"{origin}/interviews?panel={iv}"`, action_id `coord_open_interview`
  - Fallback text: `f"📞 {company} · {stage label}"`.
  - Reuse `_who`, `_short`, `_cap`, `_tags`, `_method_part` and `_button` from `app/interview_day/blocks.py`.

- [ ] **Step 1: Failing tests.** The fixture `rnd(**kw)` builds a `DayRound`: IV `"a"*24`, round `"b"*32`, company "Lumin Digital", stage "intro", start `datetime(2026,11,16,18,30,tzinfo=utc)`, profile "Brandon Mayer", owner Justin `U0JUSTIN01`, caller user Nicholas `U0NICK0001` confirmed, method video, link `https://meet.x/abc`. `origin="https://app.x"`.
  - `test_post_reads_like_the_sketch`:
    - line 1 is `f"📞 *Lumin Digital · Intro · {slack_time(START)} · Brandon Mayer*"`
    - line 2 is `"Owner <@U0JUSTIN01> · Caller: <@U0NICK0001> ✅ confirmed · Video meeting (<https://meet.x/abc|link>)"`
    - there is no line 3
    - the action ids are `["coord_choose_caller", "coord_open_brief", "coord_open_interview"]`
    - the brief URL is `https://app.x/interview/aaa…?brief=bbb…`
  - `test_unconfirmed_caller_gets_confirm`: the ids are `[choose, coord_confirm, brief, open]`, and the Confirm value is `f"{IV}:{R}"`.
  - `test_tbd_tags_managers`: line 2 is `"Owner <@U0JUSTIN01> · ⚠️ No caller yet — <@U0PUMA0001>"`.
  - `test_self_round`: line 2 is `"Owner <@U0JUSTIN01> · No caller (Self)"`, and `initial_option["value"] == f"{IV}:{R}:self"`.
  - `test_completed_offers_the_result`: line 3 is `"Completed · waiting for the result"`, and the ids are `[coord_result_passed, coord_result_rejected, coord_open_brief, coord_open_interview]`.
  - `test_ended_round_has_only_links`: a canceled round's line 1 starts with `"❌ ~"`, line 3 is `"❌ Canceled"`, and the ids are `[coord_open_brief, coord_open_interview]`.
  - `test_select_groups_and_limits`. The directory has 130 users (one is the owner) and 120 externals, one of them named with 90 characters.
    - The group labels are `["People", "External callers", "No caller"]`, and every group has ≤ 100 options.
    - The owner's id is in no option value.
    - The long name's option text has length 75 and ends with "…".
    - Every value is ≤ 150 characters.
    - `initial_option` is the Nicholas option.
  - `test_post_escapes_user_text`: company `"<!channel> & Co"` shows as `"&lt;!channel&gt; &amp; Co"`, and `"<!channel>"` is not in the section.
  - `test_section_capped`: with a 5,000-character profile, the section text is ≤ 3,000 characters.
  - `test_load_rounds_unchanged` (in `test_interview_day_post.py` or the existing rounds test): the existing `load_rounds` tests pass, and `DayRound.status` is `"scheduled"` for a scheduled round.
- [ ] **Step 2:** Run and confirm FAIL. **Step 3:** Implement. **Step 4:** Run `test_coordination_blocks.py`, `test_interview_day_blocks.py`, `test_interview_day_post.py` and `test_interview_reminders.py`. Expect PASS.
- [ ] **Step 5:** Commit `feat(coordination): short coordination post with caller select and actions (blocks)`.

### Task 4: The post is edited in place, and the thread loses change logs

**Files:**
- Modify: `app/slack_bot.py`:
  - `sync_caller_thread`, `_post_main`
  - replace `_main_post_text` with `_main_post`
  - delete `caller_round_text`, `caller_thread_reply`, `_change_lines`, `_snapshot_label`, `cc_line` and the now-unused imports `caller_change_lines` and `caller_thread_event`
  - module docstring: the #caller paragraph
- Modify: `tests_unit/test_caller_post_sync.py`, `tests_unit/test_caller_thread_text.py`

**Interfaces:**
- Consumes: Task 3 `post_blocks`, `day_round`; `caller_directory.list_directory`; `interview_managers`.
- Produces: `async def _main_post(interview: Interview, latest: InterviewStageEvent) -> tuple[str, list[dict]]`. It loads the owner, the account, the round's user caller (when `callerKind == "user"`) and the directory, builds `day_round(interview, <1-based index>, latest, ...)`, and returns `post_blocks(...)` with `origin=get_settings().FRONTEND_ORIGIN.rstrip("/")`.
- Changes: `_post_main(channel: str, text: str, blocks: list[dict]) -> Optional[str]`, and `chat.update` now sends `{"channel", "ts", "text", "blocks"}`.
- `sync_caller_thread` keeps its signature, its return value, the creation and adoption rules and the gone-post repost. The `touched` test stays (snapshots or added or deleted). The block that picks an event and posts a thread reply is deleted.

- [ ] **Step 1: Failing tests** in `test_caller_post_sync.py`. Patch `slack_bot._main_post` with `AsyncMock(return_value=("MAIN", BLOCKS))`, where `BLOCKS = [{"type": "section", "text": {"type": "mrkdwn", "text": "MAIN"}}]`.
  - `test_first_caller_round_creates_thread`: one `chat.postMessage` with `text == "MAIN"` and `blocks == BLOCKS`.
  - Rename the confirm, reschedule, cancel, next-round and round-deleted tests to `test_confirm_edits_the_post_without_a_reply`, `test_reschedule_edits_the_post_without_a_reply`, `test_cancel_edits_the_post_without_a_reply`, `test_next_round_edits_the_post_without_a_reply` and `test_round_deleted_edits_the_post_without_a_reply`. Each asserts exactly one `chat.update` with `blocks == BLOCKS` and **no** `chat.postMessage`.
  - `test_main_post_describes_the_latest_round` (mongomock, real `_main_post`): an interview has two caller rounds. The Confirm button value ends with the second round's id, and the select's option values start with `f"{iv.id}:{second.id}:"`.
  - `test_gone_main_post_is_reposted`: the repost sends `blocks == BLOCKS`.
- [ ] **Step 2:** In `test_caller_thread_text.py`, delete every test that uses `caller_round_text`, `caller_thread_reply`, `cc_line` or `_method_text`; their escaping cases moved to Task 3. Keep `test_event_*` and the `_mention` test.
- [ ] **Step 3:** Run and confirm FAIL. **Step 4:** Implement. **Step 5:** Run `test_caller_post_sync.py`, `test_caller_thread_text.py`, `test_callers.py`, `test_interview_caller.py` and `test_round_confirm.py`. Expect PASS.
- [ ] **Step 6:** `grep -rn "caller_round_text\|caller_thread_reply\|_main_post_text" app tests_unit` prints nothing.
- [ ] **Step 7:** Commit `feat(coordination): edit the coordination post in place; no change-log replies`.

### Task 5: Interactivity plumbing (dispatch, forms, replies)

**Files:**
- Modify: `app/routers/slack_bot.py` (`slack_interactions`, `:121-158`)
- Create: `app/coordination/actions.py` (click model, dispatcher, Slack reply helpers)
- Create: `app/coordination/modals.py`. This task adds only the constants and the stubs `submission_errors` / `process_coordination_submission`; Tasks 8 and 9 fill them.
- Test: `tests_unit/test_coordination_interactions.py`

**Interfaces:**
- Produces in `actions.py`:
  - `@dataclass(frozen=True) class Click: action_id: str; value: str; slack_user_id: str; response_url: str; trigger_id: str; channel: str; message_ts: str`
  - `def click_from_payload(payload: dict) -> Click`:
    - `value` is `action["value"]`, else `action["selected_option"]["value"]`
    - `channel` is `payload["channel"]["id"]`, else `payload["container"]["channel_id"]`
    - `message_ts` is `payload["container"]["message_ts"]`, else `payload["message"]["ts"]`
    - a missing piece is `""`
  - `NOT_LINKED`, `TRY_AGAIN` and `FORM_FAILED` with the Global copy.
  - `async def slack_platform_user(slack_user_id: str) -> Optional[UserExtra]`: an active user with that `slackUserId`; None for a blank id.
  - `async def reply(response_url: str, text: str) -> None`: ephemeral through `slack_transactions._post_ephemeral`, with `{"response_type": "ephemeral", "replace_original": False, "text": text}`.
  - `async def replace(response_url: str, text: str, blocks: list[dict]) -> None`: `{"response_type": "in_channel", "replace_original": True, "text", "blocks"}`.
  - `async def open_modal(trigger_id: str, view: dict) -> bool`: `slack_bot._slack_api("views.open", {"trigger_id", "view"})`. It returns False on any error and logs it.
  - `async def tell(channel: str, slack_user_id: str, text: str) -> None`: `chat.postEphemeral {"channel", "user", "text"}`. It never raises.
  - `HANDLERS: dict[str, Callable[[Click, UserExtra], Awaitable[None]]] = {}`, filled in Tasks 6 and 9.
  - `async def process_coordination_click(click: Click) -> None`:
    - resolves the user; when there is none, replies `NOT_LINKED`
    - otherwise calls `HANDLERS[click.action_id]`
    - an unknown id is logged and ignored
    - any exception is logged and replies `TRY_AGAIN`
    - never raises
- Produces in `modals.py`:
  - `CANCEL_VIEW = "coord_cancel"`, `RESCHEDULE_VIEW = "coord_reschedule"`, `REJECT_VIEW = "coord_reject"`, `COORD_VIEWS`
  - `def submission_errors(view: dict, now: datetime) -> dict[str, str]` (stub: `{}`)
  - `async def process_coordination_submission(view: dict, slack_user_id: str) -> None` (stub: log)
- Router:
  - `block_actions` with `action_id in COORD_ACTIONS` adds `background.add_task(process_coordination_click, click_from_payload(payload))`.
  - `coord_open_brief` and `coord_open_interview` fall through to the plain `{"ok": True}`.
  - `view_submission` with `payload["view"]["callback_id"] in COORD_VIEWS` computes `errors = submission_errors(view, datetime.now(timezone.utc))`.
    - When there are errors, it returns `JSONResponse({"response_action": "errors", "errors": errors})`.
    - Otherwise it adds `background.add_task(process_coordination_submission, view, slack_user_id)` and returns `Response(status_code=200)` with an empty body, which closes the modal.
  - Every other payload is unchanged.
  - The signature check runs first for both types (no change).
  - Update the docstring to cover forms.

- [ ] **Step 1: Failing tests** (signed bodies as in `test_slack_interactions.py`; patch `slack_router.process_coordination_click` / `process_coordination_submission` / `submission_errors`):
  - `test_coord_click_is_acked_and_dispatched`: a `coord_confirm` click has value `"IV:R"`, `trigger_id "T.1"`, `container {"channel_id": "C1", "message_ts": "1.2"}` and user `U0PUMA0001`. Expect 200, and the dispatcher called once with `Click("coord_confirm", "IV:R", "U0PUMA0001", RESPONSE_URL, "T.1", "C1", "1.2")`.
  - `test_select_value_comes_from_selected_option`: `selected_option.value "IV:R:C9"` gives `click.value == "IV:R:C9"`.
  - `test_url_buttons_are_acked_without_work`: `coord_open_brief` gives 200, and the dispatcher is not called.
  - `test_view_submission_errors_are_returned_inline`: `submission_errors` returns `{"time": "Pick a time in the future."}`. The response JSON is `{"response_action": "errors", "errors": {"time": "Pick a time in the future."}}`, and the submission handler is not called.
  - `test_view_submission_ok_closes_the_modal`: the response is 200 with `res.content == b""`, and the handler is called with `(view, "U0PUMA0001")`.
  - `test_unsigned_view_submission_is_401`.
  - `test_unlinked_clicker_is_told_to_link`: `process_coordination_click` for an unknown Slack id makes exactly one ephemeral reply, `"Link your Slack in Account settings first."`.
  - `test_handler_crash_says_try_again`: `HANDLERS["coord_confirm"]` raises `RuntimeError`, and the reply is `TRY_AGAIN`.
  - `test_open_modal`: `_slack_api` is recorded as `("views.open", {"trigger_id": "T.1", "view": {"type": "modal"}})` and the call returns True. When `_slack_api` raises `RuntimeError("expired_trigger_id")`, it returns False.
- [ ] **Step 2:** Run and confirm FAIL. **Step 3:** Implement. **Step 4:** Run this file, `test_slack_interactions.py` and `test_interview_confirm_click.py`. Expect PASS.
- [ ] **Step 5:** Commit `feat(slack): coordination click dispatch and view_submission handling`.

### Task 6: Choose caller and Confirm

**Files:**
- Modify: `app/coordination/actions.py` (handlers registered in `HANDLERS`)
- Test: `tests_unit/test_coordination_clicks.py`

**Interfaces:**
- Consumes: Task 1 `assign_round_caller` with `CallerAssignIn(callerId, enabled)`; `update_interview_stage` with `StagePatch(caller=CallerIn(...))`; `is_caller_manager`; `confirm_round`.
- Produces: `CHOOSE_REFUSED = "Only the owner or an interview manager can assign the caller."`
- Produces: `async def choose_caller(click: Click, user: UserExtra) -> None`. It splits `click.value` into `iv_id, round_id, pick`; a malformed value or a missing interview or round replies `ROUND_GONE`.
  - When `is_caller_manager(user)`:
    - `await ivr.assign_round_caller(iv_id, round_id, CallerAssignIn(callerId="", enabled=False), user)` for `pick == "self"`
    - otherwise `CallerAssignIn(callerId=pick, enabled=True)`
  - Otherwise, when the user owns the interview:
    - `await ivr.update_interview_stage(iv_id, round_id, StagePatch(caller=CallerIn(enabled=False)), user)` for self
    - otherwise `StagePatch(caller=CallerIn(enabled=True, callerId=pick, method=prev.method if prev else None, methodValue=prev.methodValue if prev else ""))`, where `prev` is the round's current caller
  - Anyone else gets `CHOOSE_REFUSED`.
  - Success replies ephemeral `f"Caller set: {callerName}."`, with the name read from the returned dict's round. For self, the reply is `"No caller (Self) — the caller request is off."`.
  - An `HTTPException` replies its `detail`.
- Produces: `async def confirm(click: Click, user: UserExtra) -> None`. It calls `await confirm_round(iv_id, round_id, user, True)`; a `ConfirmError` replies its `message`. On success there is no reply: `confirm_round` syncs the post, which then shows "✅ confirmed".

- [ ] **Step 1: Failing tests.** Use the mongomock fixtures from `test_interview_confirm_click.py`, spy on `interviews.sync_caller_thread`, and patch `actions._post_ephemeral`. Justin (staff, `U0JUSTIN01`) owns a TBD caller round with method video and link `https://meet.x/abc`. Puma is the interview manager (`U0PUMA0001`). Ana is staff (`U0ANA00001`). Pietro is an external caller.
  - `test_owner_picks_a_caller_through_the_round_field`:
    - Justin picks Ana.
    - The stored caller is `("Ana", str(ana.id), "user")`, the method is `"video"`, the methodValue is `"https://meet.x/abc"` and `confirmed is False`.
    - A spy on `interviews.update_interview_stage` was called and `assign_round_caller` was not.
    - The reply is `"Caller set: Ana."`, and `sync_caller_thread` was called.
  - `test_manager_picks_through_the_manager_endpoint`: Puma picks Pietro. The `assign_round_caller` spy was called, the stored callerName is `"Pietro"`, and the kind is `"external"`.
  - `test_self_turns_the_caller_request_off`, parametrized over Justin and Puma: `caller.enabled is False`, and the reply is `"No caller (Self) — the caller request is off."`.
  - `test_pick_on_a_self_round_turns_it_back_on`, parametrized over Justin and Puma: the round is enabled with the caller set.
  - `test_stranger_is_refused`: Ana picks Pietro on Justin's round. The reply is `CHOOSE_REFUSED`, and the caller is unchanged.
  - `test_owner_cannot_pick_themselves`: the reply is `"You take your own interview as Self — pick someone else as the caller."`.
  - `test_confirm_from_the_post`: Ana is the assigned user caller and clicks `coord_confirm`. `confirmed is True`. A stranger gets `"Only the caller or an interview manager can confirm."`.
  - `test_confirm_tbd_round`: the reply is `"Assign a caller first"`.
- [ ] **Step 2:** Run and confirm FAIL. **Step 3:** Implement. **Step 4:** Run this file and `test_coordination_interactions.py`. Expect PASS.
- [ ] **Step 5:** Commit `feat(coordination): choose caller and confirm from the coordination post`.

### Task 7: Release cutoff, after-call ask and 7-day result reminder

**Files:**
- Modify: `app/config.py`: add `SLACK_CALLER_ASKS_FROM: Optional[str] = None` next to the Slack caller settings (`:29-34`), plus a validator. Blank becomes None; otherwise it must parse with `"%Y-%m-%dT%H:%M"`, else `ValueError("SLACK_CALLER_ASKS_FROM must be YYYY-MM-DDTHH:MM")`.
- Modify: `app/coordination/blocks.py` (ask, reminder and answered builders)
- Create: `app/coordination/chase.py`
- Modify: `app/slack_bot.py` (`run_alert_tick`: a new isolated step after the reminders; module docstring: the new claim keys)
- Modify: `app/alerts_tick.py`: module docstring only, which now mentions the coordination asks.
- Test: `tests_unit/test_coordination_chase.py`

**Interfaces:**
- Produces in `chase.py`:
  - `CHASE_TZ = ZoneInfo("America/Chicago")`, `RESULT_REMINDER_AFTER = timedelta(days=7)`
  - `def asks_from() -> Optional[datetime]`: the setting read in `CHASE_TZ`, as aware UTC; None when unset
  - `@dataclass class ChaseRound: interview: Interview; entry: InterviewStageEvent; start: datetime; end: datetime; owner: Optional[UserExtra]`, with times aware UTC
  - `async def caller_rounds(status: str, *, ended_after: datetime, ended_by: datetime) -> list[ChaseRound]`:
    - the rounds that satisfy the Global "caller rounds only" rule, with the normalized status equal to `status` and `ended_after < end <= ended_by`
    - query: `Interview.find({"slackThread": {"$ne": None}, "stageHistory": {"$elemMatch": {"caller.enabled": True, "status": status, "scheduledAt": {"$lte": ended_by naive}}}})`, then filter in Python
  - `def after_call_key(c: ChaseRound) -> str` returns `f"after-call:{c.entry.id}:{int(c.start.timestamp())}"`
  - `def result_reminder_key(c: ChaseRound) -> str` returns `f"result-reminder:{c.entry.id}"`
  - `async def run_after_call_asks(now: datetime) -> dict[str, int]`: `{"sent", "failed"}`. It looks at `caller_rounds("scheduled", ended_after=asks_from(), ended_by=now)`.
  - `async def run_result_reminders(now: datetime) -> dict[str, int]`: it looks at `caller_rounds("completed", ended_after=asks_from(), ended_by=now - RESULT_REMINDER_AFTER)`.
  - Both chasers return zeros when there is no channel, no bot token, or no `asks_from()`. Before claiming, they read the existing `SlackAlertLog` kinds for the candidate keys (one query), so already-sent rounds cost no insert. Each send is a thread reply, `chat.postMessage` with `thread_ts = interview.slackThread.ts` and `channel = interview.slackThread.channel`. On `RuntimeError` in `_MESSAGE_GONE | {"thread_not_found"}`, they post in the channel without `thread_ts`. Any other failure releases the claim.
  - `async def run_coordination_chase(now: datetime) -> dict[str, Any]` returns `{"after_call_asks": int, "result_reminders": int}`. Each part is isolated in try/except. Task 10 adds `"result_roundup"`.
- Produces in `blocks.py`:
  - `def ask_blocks(ref: str, *, owner: Optional[UserExtra], company: str, stage: str, end: datetime) -> tuple[str, list[dict]]`:
    - The text is `f"{_who(owner, 'Owner')} — {company} {_stage_label(stage)} ended at {slack_time(end, with_day=False)}. How did it go?"`.
    - The buttons are Completed (`coord_after_completed`, primary), Canceled (`coord_after_canceled`) and "Rescheduled…" (`coord_after_rescheduled`), each with value ref.
  - `def result_reminder_blocks(ref: str, *, owner: Optional[UserExtra], company: str, stage: str, start: datetime) -> tuple[str, list[dict]]`:
    - The text is `f"{_who(owner, 'Owner')} — {company} {_stage_label(stage)} on {slack_time(start)} has no result yet. How did it end?"`.
    - The buttons are Passed (`coord_result_passed`, primary) and Rejected (`coord_result_rejected`, danger).
  - `def answered_blocks(line: str) -> list[dict]`: one mrkdwn section with `_cap(line)`.
- `run_alert_tick` adds `"after_call_asks"` and `"result_reminders"` to its result dict, from `await run_coordination_chase(datetime.now(timezone.utc))`. A crash logs and reports zeros.

- [ ] **Step 1: Failing tests.** Use mongomock with `SlackAlertLog`, `Interview`, `UserExtra`, `Account` and, from Task 10 on, `ResultRoundupPost`. Patch `get_settings` with `SLACK_BOT_TOKEN="x"`, `SLACK_CALLER_CHANNEL_ID="C1"` and `SLACK_CALLER_ASKS_FROM="2026-11-02T00:00"`. Record `slack_bot._slack_api`. The round starts 2026-11-16 18:30Z with `endsAt` 19:30Z, is a caller round with status `scheduled`, has `slackThread("C1", "100.1")`, and its owner is Justin `U0JUSTIN01`.
  - `test_asks_from_parses_and_validates`: `"2026-11-23T00:00"` gives `datetime(2026,11,23,6,0,tzinfo=utc)`. `Settings(SLACK_CALLER_ASKS_FROM="2026-11-23")` raises `ValidationError`.
  - `test_asks_off_without_the_setting`: with the setting None, the result is `{"sent": 0, "failed": 0}` and there are no Slack calls.
  - `test_after_call_ask_once_after_the_end`:
    - At 19:29Z, nothing is sent.
    - At 19:31Z, exactly one `chat.postMessage` goes out with `channel "C1"` and `thread_ts "100.1"`. Its action ids are `["coord_after_completed", "coord_after_canceled", "coord_after_rescheduled"]`, every value is `f"{iv.id}:{rid}"`, and the text starts with `"<@U0JUSTIN01> — Acme Intro ended at <!date^"`.
    - At 19:36Z, nothing more is sent.
    - The `SlackAlertLog` kind is `f"after-call:{rid}:{int(START.timestamp())}"`.
  - `test_no_end_means_one_hour`: with `endsAt=None`, nothing is sent at 19:29Z and one ask is sent at 19:31Z.
  - `test_not_asked`, parametrized over:
    - no caller
    - caller off
    - status canceled
    - status completed
    - ended 2026-11-01 20:00Z (before the cutoff)
    - stage `home_assessment`
    - no `slackThread`

    In each case, nothing is sent at 19:31Z.
  - `test_rescheduled_round_is_asked_again`: after the first ask, move the start to 2026-11-17 18:30Z and the end to 19:30Z. A second ask goes out at 2026-11-17 19:31Z.
  - `test_failed_post_releases_the_claim`: `_slack_api` raises `RuntimeError("ratelimited")` once. The result is `{"sent": 0, "failed": 1}` with no log row, and the next tick sends.
  - `test_gone_thread_posts_in_the_channel`: the first post raises `RuntimeError("thread_not_found")`. The second is posted without `thread_ts`.
  - `test_result_reminder_seven_days_after`: a completed round ending 2026-11-16 19:30Z. Nothing is sent at 2026-11-23 19:29Z. At 19:31Z, one thread reply goes out with action ids `["coord_result_passed", "coord_result_rejected"]`. Nothing is sent at 19:36Z. A `passed` round gets nothing.
  - `test_tick_reports_the_chase`: `run_alert_tick()` with the other steps patched to no-ops contains `"after_call_asks"` and `"result_reminders"`.
- [ ] **Step 2:** Run and confirm FAIL. **Step 3:** Implement. **Step 4:** Run this file, `test_alerts_tick.py`, `test_interview_reminders.py` and `test_slack_bot.py`. Expect PASS.
- [ ] **Step 5:** Commit `feat(coordination): after-call ask and 7-day result reminder for caller rounds`.

### Task 8: The three forms (pure views and parsing)

**Files:**
- Modify: `app/coordination/modals.py`
- Test: `tests_unit/test_coordination_modals.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Origin: ref: str; channel: str; ts: str; tz: str = ""`
    - `def to_metadata(self) -> str`: `json.dumps` with sorted keys and the Global key names
    - `@classmethod def from_metadata(cls, raw: str) -> "Origin"`: raises `ValueError` when the JSON or the `ref` is malformed
  - `def cancel_view(origin: Origin, title_line: str) -> dict`, `def reject_view(origin: Origin, title_line: str) -> dict`, and `def reschedule_view(origin: Origin, title_line: str, *, start: datetime) -> dict`
  - Every view has:
    - `type "modal"`, `callback_id` from Global, `private_metadata = origin.to_metadata()`
    - title "Canceled", "Rejected" or "Rescheduled"; submit "Save"; close "Back"
    - a context block with `_short(title_line, 200)`
    - an input block `block_id "note"`, element `plain_text_input`, `action_id "note_input"`, `multiline: true`, `max_length: 2000`, `optional: true`
    - note labels "Reason (optional)", "Feedback (optional)" and "Note (optional)"
  - `reschedule_view` adds, before the note:
    - `block_id "date"`: `datepicker`, `action_id "date_input"`, `initial_date = start in tz, ISO date`, label "Date"
    - `block_id "time"`: `timepicker`, `action_id "time_input"`, `initial_time = start in tz, "%H:%M"`, label `f"Time ({origin.tz})"`
  - `@dataclass(frozen=True) class Answer: kind: Literal["canceled", "rescheduled", "rejected"]; origin: Origin; note: str; new_start: Optional[datetime]`, with `new_start` aware UTC and `note` stripped
  - `def read_submission(view: dict) -> Answer`: reads `view["state"]["values"][block][action]["value" | "selected_date" | "selected_time"]`; `ValueError` when it is malformed
  - `def submission_errors(view: dict, now: datetime) -> dict[str, str]`, which replaces the Task 5 stub. For reschedule:
    - no date gives `{"date": "Pick a date."}`
    - no time gives `{"time": "Pick a time."}`
    - `new_start <= now` gives `{"time": "Pick a time in the future."}`
    - a note over 2000 characters, on any form, gives `{"note": "At most 2000 characters."}`
    - malformed metadata gives `{}`; Task 9's background handler reports it

- [ ] **Step 1: Failing tests:**
  - `test_metadata_round_trip`: `Origin("IV:R", "C1", "200.2", "America/Chicago")` survives `to_metadata` and `from_metadata`. `from_metadata("{}")` raises `ValueError`.
  - `test_cancel_view_shape`: the callback is `"coord_cancel"`, the title is `"Canceled"`, and the note block is optional with `max_length == 2000`. `json.loads(private_metadata)["ts"] == "200.2"` and `len(private_metadata) <= 3000`.
  - `test_reschedule_view_defaults_to_the_current_time_in_the_owners_zone`: start 2026-11-16 18:30Z with tz `America/New_York` gives `initial_date "2026-11-16"`, `initial_time "13:30"` and the label `"Time (America/New_York)"`.
  - `test_read_reschedule_in_the_owners_zone`: date `"2026-11-18"`, time `"09:15"`, tz `America/Chicago` and note `"  Moved by recruiter "` give `new_start == datetime(2026,11,18,15,15,tzinfo=utc)` and note `"Moved by recruiter"`.
  - `test_reschedule_across_dst`: `"2027-03-14"`, `"09:00"` in Chicago gives 14:00Z; `"2026-11-01"`, `"09:00"` gives 15:00Z.
  - `test_submission_errors`: a past time gives `{"time": "Pick a time in the future."}`, a missing date gives `{"date": "Pick a date."}`, and a valid future time gives `{}`.
  - `test_cancel_and_reject_read_the_note`: a blank note gives `""`, and the kind is right for each callback.
- [ ] **Step 2:** Run and confirm FAIL. **Step 3:** Implement. **Step 4:** Run the file. Expect PASS.
- [ ] **Step 5:** Commit `feat(coordination): cancel, reschedule and reject forms`.

### Task 9: After-call and result answers (buttons and forms)

**Files:**
- Modify: `app/coordination/actions.py` (handlers and `show_answer`)
- Modify: `app/coordination/modals.py` (`process_coordination_submission`)
- Test: `tests_unit/test_coordination_answers.py`

**Interfaces:**
- Consumes: Task 1 `can_update_round` and the manager PATCH path; Task 5 helpers; Task 7 `answered_blocks`; Task 8 views and `read_submission`.
- Produces in `actions.py`:
  - `UPDATE_REFUSED`, `ROUND_GONE`, `NEEDS_COMPLETED` (Global copy)
  - `async def load_round(ref: str) -> tuple[Interview, InterviewStageEvent]`: raises `LookupError` when the interview or round is gone
  - `async def set_round(iv_id: str, round_id: str, user: UserExtra, **fields: Any) -> None`: `await ivr.update_interview_stage(iv_id, round_id, StagePatch(**fields), user)`, so `model_fields_set` is exactly the sent keys
  - `def answer_line(kind: str, user: UserExtra, *, note: str = "", new_start: Optional[datetime] = None) -> str`:
    - completed: `"✅ Completed — by <@U…>"`
    - canceled: `"❌ Canceled — by <@U…>"`, plus `f" · {_short(note, 200)}"` when there is a note
    - rescheduled: `f"🔁 Rescheduled to {slack_time(new_start)} — by <@U…>"`
    - passed: `"🎉 Passed — by <@U…>"`
    - rejected: `"❌ Rejected — by <@U…>"`
    - the "by" part uses `_who(user)`
  - `async def show_answer(iv: Interview, *, channel: str, ts: str, response_url: str, line: str) -> None`:
    - When `ts == iv.slackThread.ts`, it does nothing: the write already re-rendered the post.
    - Otherwise, with a `response_url`, it calls `replace(response_url, line, answered_blocks(line))`.
    - Otherwise it calls `slack_bot._slack_api("chat.update", {"channel", "ts", "text": line, "blocks": answered_blocks(line)})`.
    - Task 10 adds the roundup branch before these.
  - Handlers registered in `HANDLERS`:
    - `after_completed(click, user)`: `can_update_round` or `UPDATE_REFUSED`. When the status is not `scheduled`, it replies `ALREADY.format(Label=...)` and calls `show_answer` with the line `f"Already updated: {Label}"`. Otherwise it calls `set_round(..., status="completed")` and then `show_answer` with `answer_line("completed", user)`.
    - `after_canceled(click, user)` and `after_rescheduled(click, user)`: the same checks. Then `open_modal(click.trigger_id, cancel_view(...) | reschedule_view(..., start=entry.scheduledAt))`. Their Origin is `Origin(ref, click.channel, click.message_ts, tz=resolve_user_tz(owner).key)`, and the title line is `f"{company} · {_stage_label(stage)}"`. When `open_modal` returns False, the reply is `FORM_FAILED`.
    - `result_passed(click, user)`: `can_update_round`. A status of `passed` or `rejected` replies `f"Result already set: {Label}."`; a status other than `completed` replies `NEEDS_COMPLETED`. Otherwise it calls `set_round(status="passed")` and then `show_answer` with the passed line.
    - `result_rejected(click, user)`: the same checks, then `open_modal(reject_view(...))`.
- Produces in `modals.py`: `async def process_coordination_submission(view: dict, slack_user_id: str) -> None`. It never raises. Its messages go through `tell(origin.channel, slack_user_id, text)`.
  - `read_submission` (ValueError: `TRY_AGAIN`) → `slack_platform_user` (`NOT_LINKED`) → `load_round` (`ROUND_GONE`) → `can_update_round` (`UPDATE_REFUSED`) → status check: `scheduled` for canceled or rescheduled, `completed` for rejected, else `ALREADY`.
  - Then the write:
    - canceled: `set_round(status="canceled", statusNote=note)`
    - rescheduled: `set_round(scheduledAt=new_start.isoformat(), **({"statusNote": note} if note else {}))`
    - rejected: `set_round(status="rejected", statusNote=note)`
  - Then `show_answer(iv, channel=origin.channel, ts=origin.ts, response_url="", line=answer_line(...))`.
  - An `HTTPException` tells the user its `detail`.

- [ ] **Step 1: Failing tests.** Fixtures as in Task 6. Justin owns the interview, Puma is the manager, Ana is the round's user caller and Bob is a staff stranger. Record `slack_bot._slack_api` and spy on `interviews.refresh_today` and `sync_caller_thread`. The ask message is `channel "C1"`, `ts "200.2"`, and the post ts is `"100.1"`.
  - `test_owner_marks_completed_from_the_ask`: the status becomes `completed`. One `replace` goes to RESPONSE_URL with `replace_original True` and text `"✅ Completed — by <@U0JUSTIN01>"`. `sync_caller_thread` was called.
  - `test_manager_marks_completed_on_someone_elses_round`: Puma's click works the same way, which exercises Task 1's manager path.
  - `test_caller_and_stranger_are_refused`, parametrized over Ana and Bob: the reply is `UPDATE_REFUSED` and the status stays `scheduled`.
  - `test_stale_ask_says_already_updated`: the status is already `canceled`. The reply is `"Already updated: Canceled."`, and the ask is replaced with `"Already updated: Canceled"`. No PATCH happens.
  - `test_canceled_opens_the_form`: one `views.open` with `trigger_id "T.1"`, `view.callback_id "coord_cancel"` and metadata `{"ref": f"{iv}:{rid}", "channel": "C1", "ts": "200.2", ...}`.
  - `test_rescheduled_opens_the_form_in_the_owners_zone`: Justin's `slackTimezone` is New York. The view's time label is `"Time (America/New_York)"`.
  - `test_form_does_not_open_for_strangers`: no `views.open` happens, and the reply is `UPDATE_REFUSED`.
  - `test_expired_trigger`: `views.open` raises `RuntimeError("expired_trigger_id")`, and the reply is `"Couldn't open the form — click again."`.
  - `test_cancel_submission`: the status becomes `canceled` with statusNote `"Recruiter canceled"`. One `chat.update` goes out with `channel "C1"`, `ts "200.2"` and text `"❌ Canceled — by <@U0JUSTIN01> · Recruiter canceled"`.
  - `test_reschedule_submission_moves_the_round`, with new_start 2026-11-18 15:15Z:
    - the start is 15:15Z and `endsAt` is 16:15Z (the 1 h duration is kept)
    - the status is `scheduled`, `confirmed is False` and statusNote is `"Moved by recruiter"`
    - `chat.update` text is `f"🔁 Rescheduled to {slack_time(NEW)} — by <@U0JUSTIN01>"`
    - `refresh_today` was called
  - `test_manager_reschedule_uses_the_manager_path`: a Puma submission succeeds.
  - `test_passed_from_the_post_relies_on_the_rerender`: `message_ts == "100.1"`. The status becomes `passed`, there is no `replace` call and no `chat.update` to `"100.1"` from `show_answer`, and `sync_caller_thread` was called.
  - `test_passed_before_completed`: the reply is `"Mark the round completed first."`.
  - `test_rejected_submission_sets_feedback`: the status becomes `rejected` with statusNote `"Went with another candidate"`. The reminder message is updated to `"❌ Rejected — by <@U0PUMA0001>"` when Puma submits.
  - `test_submission_refused_tells_the_user`: Bob submits with forged metadata. One `chat.postEphemeral` goes out with `channel "C1"`, `user "U0BOB00001"` and text `UPDATE_REFUSED`, and the round is unchanged.
- [ ] **Step 2:** Run and confirm FAIL. **Step 3:** Implement. **Step 4:** Run the `test_coordination_*.py` files. Expect PASS.
- [ ] **Step 5:** Commit `feat(coordination): completed, canceled, rescheduled and result answers from Slack`.

### Task 10: Friday result roundup

**Files:**
- Create: `app/models/result_roundup_post.py`
- Modify: `app/models/__init__.py` (export it), `app/db.py` (`DOCUMENT_MODELS`)
- Modify: `app/coordination/blocks.py` (`roundup_blocks`), `app/coordination/chase.py` (roundup functions, and the call in `run_coordination_chase`), `app/coordination/actions.py` (the roundup branch in `show_answer`)
- Test: `tests_unit/test_coordination_roundup.py`

**Interfaces:**
- Produces: `class ResultRoundupPost(TimestampedDocument): friday: str; channel: str; ts: str; refs: list[str]; rendered: str`.
  - `friday` is `YYYY-MM-DD`.
  - `refs` holds the `"<iv>:<round>"` refs listed when posted.
  - `rendered` is the sha1 of `json.dumps(blocks, sort_keys=True)`.
  - `Settings.name = "resultroundupposts"`, with `IndexModel([("friday", 1)], unique=True)`.
- Produces in `blocks.py`:
  - `@dataclass class RoundupRow: ref: str; company: str; stage: str; start: datetime; profile: str`
  - `def roundup_blocks(groups: Sequence[tuple[Optional[UserExtra], Sequence[RoundupRow]]], *, friday: date, origin: str) -> tuple[str, list[dict]]`:
    - first a section `f"*📋 Results still open — {friday:%a} {friday:%b} {friday.day}*"`
    - per owner, a section `f"{_who(owner, 'Unknown owner')} — {n} round{'s' if n != 1 else ''}"`
    - per row, a section `f"{company} · {_stage_label(stage)} · {slack_time(start)} · {profile}"` and an actions block with Passed (`coord_result_passed`, primary) and Rejected (`coord_result_rejected`, danger), value ref
    - Rows are added greedily while the total stays ≤ 49. The rest are summarised in a context block, `f"…and {k} more without a result — <{origin}/statistics|Statistics>"`.
    - With no rows, the only section is `"✅ All results are in — thanks."`.
    - The fallback text is the header without the asterisks.
- Produces in `chase.py`:
  - `ROUNDUP_WEEKDAY = 4`, `ROUNDUP_AT = time(18, 0)`
  - `def roundup_due(now: datetime) -> Optional[date]`: the local Friday date when `now` in `CHASE_TZ` is a Friday at or after 18:00; else None
  - `async def run_result_roundup(now: datetime) -> str`, which returns `"off"` without a channel, token or `asks_from()`, and `"skipped"` when it is not due or already claimed. Otherwise:
    - It claims `result-roundup:<friday>` and loads `caller_rounds("completed", ended_after=asks_from(), ended_by=now)`.
    - With no rows it returns `"none"` and keeps the claim.
    - Otherwise it groups by owner (by display name) and sorts rows by start. It posts with `chat.postMessage` in `SLACK_CALLER_CHANNEL_ID`, without a thread, saves a `ResultRoundupPost` and returns `"posted"`.
    - On failure it releases the claim and returns `"failed"`.
  - `async def refresh_roundup(post: ResultRoundupPost) -> bool`. It re-renders from `post.refs`, keeping refs whose round still exists, still has its caller on, and is still `completed`. When the hash differs, it calls `chat.update` and saves the new hash. It returns whether it updated.
  - `async def refresh_latest_roundup() -> None`: the post with the greatest `friday`.
  - `run_coordination_chase` adds `"result_roundup": await run_result_roundup(now)` and then `await refresh_latest_roundup()`. Each is isolated, so an app-side answer leaves the roundup within 5 minutes.
- `show_answer` adds a first branch: `post = await ResultRoundupPost.find_one({"ts": ts})`. When it exists, the branch calls `await refresh_roundup(post)` and returns; there is no replace.

- [ ] **Step 1: Failing tests** (settings and recorder as in Task 7; `chi(y, m, d, hh, mm)` builds a Chicago time):
  - `test_roundup_due_only_friday_evening`: Fri 2026-11-20 17:59 gives None, 18:00 gives `date(2026,11,20)`, 23:59 gives the same date, and Sat 2026-11-21 09:00 gives None.
  - `test_roundup_lists_open_results_by_owner`. The data:
    - Justin has 2 completed caller rounds.
    - Ana has 1.
    - There is 1 passed round, 1 completed round without a caller and 1 completed round ending 2026-11-01 20:00Z.

    Then `run_result_roundup(chi(2026,11,20,18,0))`:
    - returns `"posted"`, with one `chat.postMessage` to `"C1"` and no `thread_ts`
    - the sections contain `"<@U0ANA00001> — 1 round"` and `"<@U0JUSTIN01> — 2 rounds"`, in that order
    - there are 3 actions blocks, each with ids `["coord_result_passed", "coord_result_rejected"]`
    - `ResultRoundupPost.friday == "2026-11-20"` and `len(refs) == 3`
    - the `SlackAlertLog` kind is `"result-roundup:2026-11-20"`
  - `test_roundup_once_per_friday`: a second run at 18:05 returns `"skipped"`, with no new post.
  - `test_nothing_to_list_no_post`: it returns `"none"`, and there are no Slack calls.
  - `test_answered_round_drops_off`: set one round `passed` in the DB, then call `refresh_latest_roundup()`. One `chat.update` goes out with the stored ts and 2 actions blocks. A second refresh makes no Slack call.
  - `test_all_answered_says_thanks`: the update text is `"✅ All results are in — thanks."`.
  - `test_open_rounds_carry_to_next_friday`: on 2026-11-27 18:00, a new post still lists the unanswered round.
  - `test_roundup_block_limit`: 30 open rounds for one owner give `len(blocks) <= 50`. The last block is a context block starting with `"…and "`, and shown + k = 30.
  - `test_click_on_the_roundup_refreshes_it`: a `coord_result_passed` click with `message_ts == post.ts` sets the status to `passed`. One `chat.update` of the roundup goes out without that row, and there is no `replace`.
  - `test_roundup_off_without_the_setting`: it returns `"off"`.
- [ ] **Step 2:** Run and confirm FAIL. **Step 3:** Implement. **Step 4:** Run the `test_coordination_*.py` files and `test_alerts_tick.py`. Expect PASS.
- [ ] **Step 5:** Commit `feat(coordination): Friday result roundup, edited in place`.

### Task 11: Full suite and release (controller and user; no new code)

- [ ] **Step 1:** Run the full backend suite with `.env` moved aside. The failure set must equal Task 1's baseline. In `wt/app-reorg`, run `npm run lint && npm test && npm run build`; the failure set must equal the baseline.
- [ ] **Step 2:** `grep -rn "SLACK_CALLER_ASKS_FROM" app` finds only `app/config.py` and `app/coordination/chase.py`. `grep -rn "coord_" app | grep -v "app/coordination\|app/routers/slack_bot.py"` prints nothing.
- [ ] **Step 3:** Ask the user before pushing. The release goes with the Bids page work on the same branches, in the same release, through the same merge to `dev` / `main`. The user merges and approves the Railway deploy in the dashboard.
- [ ] **Step 4: Release notes**, for the user:
  - **Slack app → Interactivity & Shortcuts:**
    - Interactivity on.
    - Request URL `https://<engineers_backend public domain>/integrations/slack/interactions`. Read the domain from Railway → `engineers_backend` → Settings → Networking.
    - This same URL already serves the daily channel's Confirm and the transaction buttons. Verify it, don't add a second one.
    - Bot scopes: `chat:write` (posts, updates, ephemerals). `views.open` needs no extra scope.
    - The bot must be a member of the coordination channel.
  - **Railway, both services** (`engineers_backend` serves the click URL; `keen-spirit` runs the cron tick):
    - New: `SLACK_CALLER_ASKS_FROM` = the deploy time in Chicago, `YYYY-MM-DDTHH:MM`. Set it after the deploy is live. Until it is set, no ask, reminder or roundup is sent.
    - Must already be set (verify, don't change): `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CALLER_CHANNEL_ID`, `FRONTEND_ORIGIN`, `SLACK_BOT_CRON_SECRET`.
  - **Behaviour the team will notice:**
    - Coordination posts become short and get buttons on their next change.
    - Threads stop getting change logs.
    - Owners can now reassign their round's caller in the app too.
    - Interview managers get the status select and "Change time" on every interview.
- [ ] **Step 5: Smoke check with the user, on one real caller interview:**
  - Choose caller → the post updates.
  - Confirm → "✅ confirmed".
  - After the end (≤ 5 min), the ask appears in the thread. Answer Completed → the ask reads "✅ Completed — by …" and the post shows [Passed] [Rejected].
  - Rescheduled… opens a form in the owner's zone.
  - A non-linked Slack user clicking gets "Link your Slack in Account settings first."
  - On the first Friday after 18:00 Chicago, one roundup post appears.
