import os

# app.config refuses an AUTH_SECRET under 32 chars. Set before any test module imports the app, so the
# per-module os.environ.setdefault("AUTH_SECRET", "unit-test-secret") lines are no-ops.
if len(os.environ.get("AUTH_SECRET", "")) < 32:
    os.environ["AUTH_SECRET"] = "unit-test-secret-" + "x" * 32
