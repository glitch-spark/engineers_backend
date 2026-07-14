# engineers_backend tests

Integration tests that hit a real MongoDB database via Beanie/Motor and
exercise the FastAPI app in-process via httpx + ASGI transport. **No mocks.**

## Prerequisites

1. A MongoDB cluster (Atlas or local).
2. **A different database name from production.** The suite calls
   `delete_all()` on `userextras`, `transactions`, `accounts`, `cardlinks`,
   `weeklyplans` between *every* test — pointing this at the prod db will
   destroy production data.

## Setup

```bash
# inside engineers_backend/ with the venv active
pip install -r requirements-dev.txt
```

## Running

```bash
# Required — tests refuse to start without this
export MONGODB_URI_TEST="mongodb+srv://USER:PASS@cluster.mongodb.net/EngineerDB_test"
# (Windows PowerShell: $env:MONGODB_URI_TEST = "...")

pytest tests/ -v
```

If `MONGODB_URI_TEST` is unset, conftest raises `RuntimeError` rather than
risk touching the prod db.

## What's covered

- `test_auth.py` — register/login/me, first-user-becomes-admin, name-or-email
  case-insensitive match, duplicate handling, missing-token 401
- `test_profile.py` — get/update profile, password change + login round-trip
- `test_users.py` — admin-only CRUD, 403 for staff, search filter, no
  password leak in responses
- `test_accounts.py` — owner-vs-admin scoping, ownership 404 on cross-user
  edit, admin `userId` filter
- `test_transactions.py` — CRUD, status change with `approvedBy`/`approvedAt`,
  cross-user edit 403, `/summary` shape (monthly + statusBreakdown + stats)
- `test_cardlinks.py` — CRUD with `from`/`to` alias round-trip, owner-vs-admin,
  cross-user edit 403
- `test_weekly_plans.py` — CRUD, `(userId, year, weekNumber)` duplicate guard,
  team-wide read (staff can list/get others' plans), cross-user edit/delete 403

## Slack

`SLACK_WEBHOOK_URL` is forced to `""` in `conftest.py`. `slack._post_to_slack`
returns early when the URL is empty, so `notify_*` calls become no-ops without
mocking. No real network.

## bcrypt compat

Cross-impl password compatibility (passlib reading bcryptjs `$2a$` hashes) is
covered separately by `verify_compat.py` at the repo root.
