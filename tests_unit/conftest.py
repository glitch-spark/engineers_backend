import os

# app.config refuses an AUTH_SECRET under 32 chars. Set before any test module imports the app, so the
# per-module os.environ.setdefault("AUTH_SECRET", "unit-test-secret") lines are no-ops.
if len(os.environ.get("AUTH_SECRET", "")) < 32:
    os.environ["AUTH_SECRET"] = "unit-test-secret-" + "x" * 32

# Unit tests never talk to Redis: a blank URL (env wins over .env) turns app.redis_cache into a no-op.
os.environ["REDIS_URL"] = ""

# Unit tests never reach Slack: a developer .env holds the live bot token and channels, so a saved daily plan, a
# transaction or a caller round would post to the real workspace. Tests that need Slack patch it themselves.
for _key in (
    "SLACK_BOT_TOKEN", "SLACK_DAILY_PLAN_CHANNEL_ID", "SLACK_TRANSACTIONS_CHANNEL_ID", "SLACK_CALLER_CHANNEL_ID",
    "SLACK_CALLER_DAILY_CHANNEL_ID", "SLACK_BIDS_CHANNEL_ID", "SLACK_CALLER_ASKS_FROM",
    "SLACK_CALLERS_BOT_TOKEN",
):
    os.environ[_key] = ""

# Profile metrics measure every completed week unless a test sets a start date (a developer .env can't move it).
os.environ["PROFILE_METRICS_SINCE"] = "0001-01-01"
