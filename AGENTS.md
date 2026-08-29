# engineers_backend

FastAPI + Beanie API for the Engineers SPA. Cursor rules: `.cursor/rules/`.

- Push/PRs: `spark` → `glitch-spark/engineers_backend`. Do not push to `bcano-office`.
- Stack: Python 3.10+, MongoDB collection names preserved, JWT bearer.
- Cron: `python -m app.alerts_tick` on a Railway sibling service; HTTP fallback `POST /internal/alerts/tick`.
