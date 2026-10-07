# Slack Notifications Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One message style for every Slack notification, working transaction posts with Approve/Reject buttons for admins, and coworkers, the application-outcome ping, dead Slack code and OAuth leftovers removed.

**Architecture:** Message text stays in pure builder functions in `app/slack_bot.py` (interviews, plans, digest) and a new `app/slack_transactions.py` (transactions). Transactions post with the bot token and store their post (`slackChannel`/`slackTs`) like daily plans. A new signed endpoint `POST /integrations/slack/interactions` handles button clicks through one shared `set_transaction_status`.

**Tech Stack:** FastAPI, Beanie/Motor (mongomock-motor in unit tests), httpx, pytest; React + TypeScript (Vite).

**Spec:** `docs/superpowers/specs/2026-10-07-slack-notifications-cleanup-design.md` (exact message copy lives there; tests below quote it).

## Global Constraints

- Repos: backend `engineers_backend` and frontend `engineers_app`, both on branch `feat/cleanup`. Do not push.
- Python: `.venv/Scripts/python.exe`. Unit tests: `.venv/Scripts/python.exe -m pytest tests_unit --continue-on-collection-errors -q`. Baseline outside this work: `test_bidder_ext.py::test_upload_url_without_storage_is_503` fails and 3 files fail to collect; nothing else may fail.
- `tests/` needs `MONGODB_URI_TEST` (not configured); edit those tests to match but do not point them at the live `MONGODB_URI`.
- Local `.env` points at the live database. Never run a backend that posts to Slack or writes data while testing; fake `_slack_api` in tests.
- Plain mrkdwn text; first line `emoji [@owner —] *Title*[ · facts]`; details `*Label:* value`; last line `cc …`. Times via `slack_time`. Mentions via `_mention`. Transactions alone use blocks.
- Interview managers = `interview_managers()` (active `interview_manager` role, by name). With none, a cc line naming only managers is omitted.
- User-entered text in Slack (descriptions, notes, names typed by users) goes through `_slack_escape`.
- New settings: `SLACK_TRANSACTIONS_CHANNEL_ID: Optional[str] = None`, `SLACK_SIGNING_SECRET: Optional[str] = None`.
- Frontend checks: `npx tsc -b` (only the 2 known `InterviewPrepLibrary.tsx` errors allowed) and `npx vite build --outDir <scratchpad>`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- Interviews saved with `caller.coworkerIds` before this change must still load and save (Beanie ignores the unknown field) — test in Task 1.
- A transaction description like `<!channel> pay me` must render as text, not ping the channel — test in Task 5.
- Owner, payer or admin without a Slack ID shows their name and no broken `<@>` tag — test in Task 5.
- Two clicks on the same pending transaction (double click, two admins): the second gets "Already approved by …" and no second thread reply — test in Task 6.
- A request whose body was altered after signing is rejected 401 even with a fresh timestamp — test in Task 6.

---

### Task 0: Commit the earlier, already-approved work

The interview manager role, Users-page Slack ID column and admin "All users" interview default are uncommitted in both repos and touch files this plan edits.

- [ ] **Step 1:** Backend: commit everything except this plan as `feat(users): interview manager role replaces per-user managers; drop accountant`.
- [ ] **Step 2:** Frontend: commit `src/pages/Users.tsx`, `src/api/endpoints.ts`, `src/auth/RoleGuard.tsx`, `src/App.tsx`, the deleted `src/pages/Accountants.tsx` and the two Job Applies files as `feat(users): interview manager role, Slack ID column; drop accountant`; then `src/pages/interviews/useInterviewFilters.ts` as `feat(interviews): admins default to all users`.
- [ ] **Step 3:** Commit this plan (backend) as `docs(plan): Slack notifications cleanup`.

### Task 1: Remove coworkers from the backend

**Files:**
- Modify: `app/models/interview.py` (drop `InterviewCaller.coworkerIds`; drop the `caller.coworkerIds` and `stageHistory.caller.coworkerIds` indexes)
- Modify: `app/interview_caller.py` (drop `CallerBody.coworkerIds`, `is_coworker`, `valid_coworker_ids` parameter and checks, the `("coworkers", "Coworkers")` change field and `"coworkers"` snapshot key)
- Modify: `app/routers/interviews.py` (no coworker id lookup when building the caller; `_can_view` = admin, interview manager or owner; delete `_populate_coworkers` and its three call sites)
- Modify: `app/slack_bot.py` (`caller_round_text` loses `coworkers`; `_main_post_text` stops loading coworkers; `_change_lines` stops resolving coworker mentions; `_snapshot_label` loses the `coworkers` branch)
- Test: `tests_unit/test_interview_caller.py`, `tests_unit/test_interview_round_helpers.py`, `tests_unit/test_caller_thread_text.py`, `tests/test_caller.py`, `tests/test_interviews.py` (remove coworker cases), new cases in `tests_unit/test_interview_caller.py`

**Interfaces:**
- Produces: `build_caller(body: CallerBody, ...) -> InterviewCaller` without `valid_coworker_ids`; `round_caller_snapshot(entry) -> Optional[dict]` with keys `time, stage, status, method, link, caller, confirmed`; `caller_round_text(interview, entry, *, account, owner, managers) -> str` (no `coworkers`); `_can_view(iv, user) -> bool`.

- [ ] **Step 1: Write the failing tests** in `tests_unit/test_interview_caller.py`:

```python
def test_snapshot_has_no_coworkers():
    assert "coworkers" not in round_caller_snapshot(entry_with_caller())

def test_caller_body_ignores_coworker_ids():
    caller = build_caller(CallerBody(enabled=True, coworkerIds=["507f1f77bcf86cd799439011"]), ...)
    assert not hasattr(caller, "coworkerIds")

async def test_old_interview_with_coworker_ids_loads_and_saves(db):
    raw = {...minimal interview..., "stageHistory": [{"id": "r1", "stage": "intro", "caller": {"enabled": True, "coworkerIds": [ObjectId()]}}]}
    await db["interviews"].insert_one(raw)
    iv = await Interview.get(raw["_id"])
    iv.companyName = "Acme"
    await iv.save()
```

and in `tests_unit/test_interview_round_helpers.py` replace the coworker `_can_view` assertions with: owner True, interview manager True, admin True, unrelated staff False, former coworker staff False.

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests_unit/test_interview_caller.py tests_unit/test_interview_round_helpers.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement** the removals listed under Files; delete coworker cases from the other listed tests and drop `coworkers=` arguments in `test_caller_thread_text.py`.
- [ ] **Step 4: Run** the full unit suite — Expected: only baseline failures; `grep -rn coworker app` returns nothing.
- [ ] **Step 5: Commit** `refactor(interviews): remove coworkers from caller rounds`.

### Task 2: Remove coworkers from the frontend

**Files:**
- Modify: `engineers_app/src/components/interview/CallerFields.tsx` (delete the Coworkers picker and `coworkerOptions`; help text → `A caller joins at this round's start time. Posts to the #caller channel on Slack.`)
- Modify: `engineers_app/src/lib/interviewForm.ts` (drop `callerCoworkerIds` from type, defaults, load and payload)
- Modify: `engineers_app/src/api/endpoints.ts` (drop `coworkerIds`, `coworkers` from the caller types)

**Interfaces:** Consumes Task 1 (API no longer accepts or returns coworkers).

- [ ] **Step 1: Implement** the removals; drop any import left unused.
- [ ] **Step 2: Run** `npx tsc -b` and `npx vite build --outDir <scratchpad>/fe-build` — Expected: only the 2 known errors; build succeeds; `grep -rni coworker src` returns nothing.
- [ ] **Step 3: Commit** (frontend repo) `refactor(interviews): remove coworkers picker`.

### Task 3: #caller posts, thread replies, 30-minute reminder and daily list in the new style

**Files:**
- Modify: `app/slack_bot.py` (`caller_round_text`, `caller_thread_reply`, `_managers_text`, `caller_reminder_text`, `_reminder_people`, `run_caller_reminders`, `caller_daily_text`, `_caller_daily_people`, `run_caller_daily`)
- Test: `tests_unit/test_caller_thread_text.py`, `tests_unit/test_caller_reminder.py`, `tests_unit/test_caller_daily.py`, `tests_unit/test_caller_post_sync.py`

**Interfaces:**
- Consumes: Task 1 `caller_round_text` signature.
- Produces: `cc_line(*users: Optional[UserExtra]) -> Optional[str]` (`"cc " + mentions`, de-duplicated, None when empty); `caller_reminder_text(interview, entry, *, now, owner, managers, account) -> str`; `_reminder_people(interview, entry) -> tuple[Optional[UserExtra], list[UserExtra], Optional[Account]]` (owner, managers, the interview's account for the Profile line; `run_caller_reminders` passes all three to `caller_reminder_text`); `caller_daily_text(day, rows, managers) -> str`, rows keep keys `start, company, stage, profile, callerName, method, mentions, link` where `mentions` is the owner only; `_caller_daily_people` returns `{"owner", "profile"}`.

- [ ] **Step 1: Write the failing tests** (copy per spec §1):

```python
def test_main_post_owner_first_managers_cc_last():
    lines = caller_round_text(IV, entry(), account=None, owner=OWNER, managers=[MANAGER]).splitlines()
    assert lines[0] == ":telephone_receiver: <@U0OWNER001> — *Caller needed*"
    assert lines[-2:] == [":hourglass_flowing_sand: Not confirmed yet", "cc <@U0MANAGER1>"]
    assert not any(l.startswith(("*Coworkers:*", "*Interview manager:*")) for l in lines)

def test_main_post_without_managers_has_no_cc():
    assert "cc " not in caller_round_text(IV, entry(), account=None, owner=OWNER, managers=[])

def test_canceled_and_no_caller_heads():
    assert caller_round_text(IV, entry(status="canceled"), account=None, owner=OWNER, managers=[]).startswith(
        ":x: <@U0OWNER001> — *Canceled* ~Caller needed~")
    assert caller_round_text(IV, entry(caller=False, stage="tech_round_1"), account=None, owner=OWNER, managers=[]
        ).splitlines()[0] == ":telephone_receiver: <@U0OWNER001> — *No caller needed* for Tech Round 1"

def test_reply_owner_first_cc_managers():
    text = reply("confirmed", entry(confirmed=True), snap())
    assert text.splitlines()[0] == f":white_check_mark: <@U0OWNER001> — *Confirmed* — Intro · {slack_time(START)} · caller Sam"
    assert text.splitlines()[-1] == "cc <@U0MANAGER1>"
    assert reply("confirmed", entry(confirmed=True), snap(), managers=[]).splitlines()[-1].startswith(":white_check_mark:")

def test_reminder_tags_owner_and_managers():
    text = caller_reminder_text(IV, rnd(), now=..., owner=OWNER, managers=[MANAGER], account=None)
    assert text.splitlines()[0].startswith(":alarm_clock: *Starting in ")
    assert "*Profile:* —" in text and text.splitlines()[-1] == "cc <@U0OWNER001> <@U0MANAGER1>"

def test_daily_list_owner_per_line_managers_once():
    text = caller_daily_text(DAY, [row(mentions="<@U0OWNER001>")], [MANAGER])
    assert text.splitlines()[0] == f":alarm_clock: *Caller interviews today* — {day_title}"
    assert text.splitlines()[1].startswith("• ") and text.splitlines()[1].endswith("— <@U0OWNER001> · <https://slack.example/p1|thread>")
    assert text.splitlines()[-1] == "cc <@U0MANAGER1>"
    assert caller_daily_text(DAY, [], []) == f":alarm_clock: *Caller interviews today* — {day_title} · none confirmed."
```

Update the remaining existing expectations in these four files to the spec's reply table (every first line gains `<@U0OWNER001> — ` after the emoji; `details` becomes `:pencil2: <@U0OWNER001> — *Caller details updated*`).

- [ ] **Step 2: Run** the four test files — Expected: FAIL.
- [ ] **Step 3: Implement.** `caller_daily_text` takes `managers` from one `interview_managers()` call in `run_caller_daily` (new `managers` callable parameter defaulting to `interview_managers`, faked in tests like `people`). Owner of a missing user stays `someone`.
- [ ] **Step 4: Run** the full unit suite — Expected: only baseline failures.
- [ ] **Step 5: Commit** `feat(slack): owner-first #caller posts, managers cc'd, reminder and daily list restyled`.

### Task 4: Digest DM and daily plan heading

**Files:**
- Modify: `app/slack_bot.py` (`daily_plan_text` first line; replace `split_digest_interviews`, `digest_summary`, `_digest_line`, `digest_blocks` with `digest_text`; `_digest_for_user` queries only `createdBy == user.id`, sends `text` with no blocks)
- Test: `tests_unit/test_slack_daily_plan.py`, `tests_unit/test_slack_bot.py`

**Interfaces:**
- Produces: `digest_text(day: date, interviews: list[Interview], *, profiles: dict[str, str], tz: ZoneInfo, why: Optional[str]) -> str`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_daily_plan_heading():
    assert daily_plan_text(plan, owner).splitlines()[0] == ":spiral_note_pad: <@U0OWNER001>'s *Daily Plan*"

def test_digest_rows():
    text = digest_text(date(2026, 10, 7), [iv_with_caller, iv_without_caller], profiles=..., tz=NY, why="Ship it")
    lines = text.splitlines()
    assert lines[0] == ":alarm_clock: *Today's interviews* — Wed Oct 7 · 2 interviews"
    assert lines[1].startswith("• 10:30 AM") and lines[2].startswith("• Time TBD")
    assert "<https://jobs.example/1|Acme>" in lines[1]
    assert lines[-2].endswith("|Open interviews>") and lines[-1] == "_Know your why: Ship it_"

def test_digest_empty():
    assert digest_text(date(2026, 10, 7), [], profiles={}, tz=NY, why=None) == \
        ":alarm_clock: *Today's interviews* — Wed Oct 7 · none today. Have a rest."

def test_digest_singular():
    assert digest_text(date(2026, 10, 7), [iv_with_caller], profiles={}, tz=NY, why=None).splitlines()[0].endswith("· 1 interview")
```

Delete tests of the removed digest helpers.

- [ ] **Step 2: Run** both test files — Expected: FAIL.
- [ ] **Step 3: Implement.** Row time is the caller start formatted with `fmt_caller_time(..., with_day=False)` in the user's zone (DM, so no date tag needed), else `Time TBD`; row order as today (`_by_start`). Day title format `Wed Oct 7` (reuse `_day_title`).
- [ ] **Step 4: Run** the full unit suite — Expected: only baseline failures.
- [ ] **Step 5: Commit** `feat(slack): plain-text digest and Daily Plan heading`.

### Task 5: Transaction posts with the bot

**Files:**
- Create: `app/slack_transactions.py`
- Modify: `app/models/transaction.py` (add `slackChannel: Optional[str] = None`, `slackTs: Optional[str] = None`)
- Modify: `app/config.py` (add `SLACK_TRANSACTIONS_CHANNEL_ID`, `SLACK_SIGNING_SECRET`), `.env.example`
- Modify: `app/routers/transactions.py` (create → `sync_transaction_post`; pending-field edit → `sync_transaction_post`; status change → `set_transaction_status`; delete → `delete_transaction_post`; stop importing `app.slack`)
- Modify: `app/bids/slack_format.py` (import `fmt_usd` from `app.slack_transactions`)
- Test: `tests_unit/test_slack_transactions.py`

**Interfaces:**
- Produces (in `app/slack_transactions.py`):
  - `fmt_usd(n: float) -> str` (`$1,234.50`)
  - `transaction_text(tx, owner, payer) -> str`
  - `transaction_blocks(tx, owner, payer, decided_by: Optional[UserExtra]) -> list[dict]` — a section with `transaction_text`, then either an `actions` block (buttons `action_id` `tx_approve` / `tx_reject`, `value` = tx id, styles `primary` / `danger`; only when `tx.status == "pending"` and `SLACK_SIGNING_SECRET` is set) or a section `:white_check_mark: *Approved* by {mention}` / `:no_entry: *Rejected* by {mention}`
  - `async sync_transaction_post(tx) -> None` — never raises; saves `slackChannel`/`slackTs` when a new post is made
  - `async announce_decision(tx, by: UserExtra) -> None` — never raises
  - `async delete_transaction_post(tx) -> None` — never raises
  - `async set_transaction_status(tx, status: Literal["approved", "rejected"], by: UserExtra) -> None` — sets `status`, `approvedBy=by.id`, `approvedAt=utcnow`, saves, then `announce_decision`

- [ ] **Step 1: Write the failing tests** (Slack faked by patching `app.slack_transactions._slack_api`; mongomock for `Transaction`/`UserExtra`):

```python
def test_text_card_monthly():
    text = transaction_text(tx(amount=120, payMethod="card", cardLast4="1234", cardLabel="Ops card", billingCycle="monthly",
                               description="Hosting"), OWNER, PAYER)
    assert text.splitlines() == [
        ":money_with_wings: <@U0OWNER001> — *New transaction* · $120.00",
        "*Payer:* <@U0PAYER001>", "*Date:* Oct 7", "*Method:* Card ••1234 (Ops card)", "*Billing:* Monthly",
        "*Description:* Hosting"]

def test_text_coin_one_time_no_description_with_notes():
    lines = transaction_text(tx(payMethod="coin", billingCycle="one_time", description="", notes="n1"), OWNER, OWNER).splitlines()
    assert "*Method:* Coin" in lines and "*Billing:* One-time" in lines and "*Notes:* n1" in lines
    assert not any(l.startswith("*Description:*") for l in lines)

def test_text_escapes_user_text():
    assert "&lt;!channel&gt;" in transaction_text(tx(description="<!channel> pay me"), OWNER, OWNER)

def test_names_without_slack_id():
    assert transaction_text(tx(), user("Pedro"), user("Maria")).splitlines()[:2] == [
        ":money_with_wings: Pedro — *New transaction* · $120.00", "*Payer:* Maria"]

def test_buttons_only_when_pending_and_secret(monkeypatch): ...  # pending+secret → actions; no secret → none; approved → "Approved by"

async def test_sync_posts_then_updates_then_reposts_gone(db): ...  # postMessage stores ts; second call chat.update; message_not_found → new post
async def test_set_status_updates_post_and_replies_in_thread(db): ...  # chat.update + thread reply ":white_check_mark: <@U0OWNER001> — *Approved* by <@U0ADMIN001> · $120.00"
async def test_decision_without_post_is_standalone(db): ...  # one chat.postMessage without thread_ts
async def test_no_channel_configured_is_noop(db): ...
```

- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests_unit/test_slack_transactions.py -q` — Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the module, model fields, settings and router hooks. The PUT route keeps "admin or payer" and calls `set_transaction_status` only when the status actually changes; edits to a still-pending transaction call `sync_transaction_post` after saving. Reuse `_slack_api`, `_mention`, `_slack_escape`, `_MESSAGE_GONE` from `app.slack_bot`.
- [ ] **Step 4: Run** the full unit suite — Expected: only baseline failures.
- [ ] **Step 5: Commit** `feat(slack): transaction posts via the bot with Approve/Reject buttons`.

### Task 6: Interactions endpoint

**Files:**
- Modify: `app/routers/slack_bot.py` (add `POST /integrations/slack/interactions`)
- Modify: `app/slack_transactions.py` (add `verify_slack_signature`, `handle_transaction_action`)
- Test: `tests_unit/test_slack_interactions.py`

**Interfaces:**
- Consumes: Task 5 `set_transaction_status`, `transaction_blocks`.
- Produces: `verify_slack_signature(secret: str, timestamp: str, body: bytes, signature: str, now: float) -> bool`; `async handle_transaction_action(action_id: str, tx_id: str, slack_user_id: str) -> Optional[str]` (None on success, else the ephemeral text).

- [ ] **Step 1: Write the failing tests** (httpx ASGI client; sign requests with a test secret):

```python
def test_signature_valid_wrong_secret_stale_and_tampered():
    body, ts = b"payload=%7B%7D", "1760000000"
    sig = "v0=" + hmac.new(b"s3cret", f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    assert verify_slack_signature("s3cret", ts, body, sig, now=1760000060) is True
    assert verify_slack_signature("other", ts, body, sig, now=1760000060) is False
    assert verify_slack_signature("s3cret", ts, body, sig, now=1760000000 + 301) is False
    assert verify_slack_signature("s3cret", ts, body + b"x", sig, now=1760000060) is False

async def test_endpoint_401_on_bad_signature_and_503_without_secret(client): ...
async def test_admin_approves(client, db): ...      # 200; tx.status == "approved"; approvedBy == admin.id
async def test_admin_rejects(client, db): ...
async def test_non_admin_refused(client, db): ...   # ephemeral "Only admins can approve or reject transactions in Slack."; status pending
async def test_unlinked_slack_user_refused(client, db): ...
async def test_second_click_already_decided(client, db): ...  # "Already approved by Root."; only one thread reply posted
async def test_unknown_transaction(client, db): ...  # "This transaction no longer exists."
async def test_unrelated_action_ignored(client, db): ...  # 200, nothing changes
```

Ephemeral replies are POSTed to the payload's `response_url` with `{"response_type": "ephemeral", "replace_original": False, "text": ...}`; fake that HTTP call.

- [ ] **Step 2: Run** the test file — Expected: FAIL.
- [ ] **Step 3: Implement.** Read the raw body before parsing; timestamp window 300 s; `hmac.compare_digest`. The clicker is `UserExtra.find_one({"slackUserId": id, "role": "admin", "isActive": {"$ne": False}})`. Approve/reject sets the status only if still `pending`.
- [ ] **Step 4: Run** the full unit suite — Expected: only baseline failures.
- [ ] **Step 5: Commit** `feat(slack): approve or reject transactions from Slack`.

### Task 7: Remove dead code, the outcome ping and OAuth leftovers; docs

**Files:**
- Delete: `app/slack.py`, `tests_unit/test_slack.py`
- Modify: `app/routers/applications.py` (remove the `notify_new_interview` call, `notify_terminal`, the import)
- Modify: `app/slack_bot.py` (delete `notify_interview_dm`, `_interview_blocks`, `_interview_fields` and any helper left unused)
- Modify: `app/config.py`, `.env.example`, local `.env` (remove `SLACK_WEBHOOK_URL` and its validator, `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_OAUTH_REDIRECT_URI`)
- Modify: `tests/conftest.py`, `tests/README.md` if they mention the removed settings
- Modify: `docs/slack-interview-bot.md` (new formats; transactions channel; Interactivity setup and env vars per spec §4)

- [ ] **Step 1: Implement** the removals.
- [ ] **Step 2: Verify** `grep -rn "SLACK_WEBHOOK\|SLACK_CLIENT\|SLACK_OAUTH\|app.slack import\|notify_interview_dm\|notify_new_interview" app tests tests_unit .env.example` returns nothing, and `.venv/Scripts/python.exe -c "import app.main"` succeeds.
- [ ] **Step 3: Run** the full unit suite — Expected: only baseline failures.
- [ ] **Step 4: Commit** `chore(slack): remove webhook module, outcome ping and OAuth settings`.

### Task 8: Final check

- [ ] **Step 1: Run** the backend unit suite and the frontend `tsc -b` + `vite build` — Expected: as in Global Constraints.
- [ ] **Step 2: Restart** the local backend on :8080 with `RESUME_QUEUE_DISABLED=1 JOB_APPLY_WORKER_DISABLED=1` and `--env-file .env` (no `--reload`); confirm `/openapi.json` lists `/integrations/slack/interactions` and the frontend loads on :3000.
- [ ] **Step 3: Report** the manual rollout steps from spec §4 to the user.
