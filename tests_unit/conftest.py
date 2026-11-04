import os

# app.config refuses an AUTH_SECRET under 32 chars. Set before any test module imports the app, so the
# per-module os.environ.setdefault("AUTH_SECRET", "unit-test-secret") lines are no-ops.
if len(os.environ.get("AUTH_SECRET", "")) < 32:
    os.environ["AUTH_SECRET"] = "unit-test-secret-" + "x" * 32

# Unit tests never talk to Redis: a blank URL (env wins over .env) turns app.redis_cache into a no-op.
os.environ["REDIS_URL"] = ""

# Profile metrics measure every completed week unless a test sets a start date (a developer .env can't move it).
os.environ["PROFILE_METRICS_SINCE"] = ""
