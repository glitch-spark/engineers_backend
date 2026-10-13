# engineers_backend

FastAPI backend for the engineersapp split. Connects to the existing MongoDB Atlas
cluster and exposes a Bearer-token API consumed by the Vite SPA in `engineersapp/`.

## Requirements

- Python 3.10+
- Access to the existing MongoDB Atlas cluster (URI must include the default DB, e.g. `/EngineerDB`)
- The same `AUTH_SECRET` the Next.js app uses today, so existing JWTs remain valid (see `docs/adr/ADR-0002`)

## Quick start

```bash
cp .env.example .env
# fill MONGODB_URI and AUTH_SECRET

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn app.main:app --reload
```

- Health check: `GET http://localhost:8080/healthz` → `{"status":"ok"}`
- Swagger UI: `http://localhost:8080/docs`

## Environment variables

| Name | Required | Notes |
|---|---|---|
| `MONGODB_URI` | yes | Same Atlas cluster the Next app uses; URI must include default DB (`/EngineerDB`) |
| `AUTH_SECRET` | yes | **Must match** the Next app's secret so existing tokens stay valid (ADR-0002) |
| `FRONTEND_ORIGIN` | no | CORS allowlist; default `http://localhost:5173` |
| `APP_BASE_URL` | no | Used in Slack message links; default `http://localhost:8080` |
| `APP_TIMEZONE` | no | IANA zone for Slack timestamps; default `America/New_York` |
| `SLACK_BOT_TOKEN` | no | Slack bot token; Slack messages are no-ops when unset (setup in `docs/slack-interview-bot.md`) |
| `JWT_EXPIRES_MIN` | no | JWT lifetime in minutes; default 720 (12 h) |
| `OPENAI_API_KEY` | no* | Required for `/ai-review/*` and `/resume/generate`; those routes 503 without it |
| `RESUME_OPENAI_MODEL` | no | OpenAI model for the interview features and job extraction (not the resume/cover letter/screening selector); default `gpt-5-mini` |
| `ANTHROPIC_API_KEY` | no | Enables the Anthropic presets in the resume / cover letter / screening model picker; hidden without it. `OPENAI_API_KEY` enables the OpenAI presets the same way |
| `ANTHROPIC_WORKSPACE_ID` | no | Only for user-level Anthropic keys (`sk-ant-usr-…`), which are not tied to a workspace: the workspace id (`wrkspc_…`) sent with every request |
| `RESUME_DEFAULT_MODEL`, `COVER_LETTER_DEFAULT_MODEL`, `SCREENING_DEFAULT_MODEL` | no | Default model per task when a request doesn't pick one (provider-prefixed ids); defaults `anthropic:claude-sonnet-5-5`, `anthropic:claude-opus-5-5`, `anthropic:claude-sonnet-5-5` |
| `JUDGE_DEFAULT_MODEL` | no | Judge for the resume prompt evaluator (Resumes → Prompt Eval): scores each generated resume against a JD in a fresh context; default `anthropic:claude-opus-5-5` |
| `PROMPT_EVAL_CONCURRENCY` | no | Job descriptions evaluated at once per eval run (each holds one slot for its generate + judge calls); default 4 |
| `REDIS_URL`, `LLM_CACHE_TTL_SECONDS` | no | Optional Redis cache for prompt-eval judge verdicts, keyed by content hash + model lane. Generated resumes are never stored in Redis; they use only the 7-day Mongo cache. Blank = off, and a dead Redis is skipped with a warning. TTL default 2592000 (30 days) |
| `AWS_REGION` | no | Enables S3 archiving of generated resumes when set with `AWS_S3_BUCKET` |
| `AWS_S3_BUCKET` | no | S3 bucket for archived resumes; uploads silently skip if unset |

## Layout

**Resume prompt evaluator** (`app/prompt_eval/`, `app/routers/prompt_eval.py`, `/resume/eval`): scores a profile's resume prompt against a shared library of role-tagged sample job descriptions (`eval_job_descriptions`, seeded once on boot with five postings per role, including Senior and Staff SWE, editable in-app; seeds are never overwritten). A run (`resume_prompt_evals`) generates one resume per JD in parallel without rendering a PDF, then the judge model scores job match (location and work authorization ignored) and believability; rows and per-role averages are kept so runs can be compared. Runs still `running` after 20 minutes are reported failed.

```
app/
├── main.py        # FastAPI app + CORS + lifespan (init_beanie)
├── config.py      # pydantic-settings
├── db.py          # Motor + init_beanie
├── security.py    # bcrypt + HS256 JWT
├── deps.py        # get_current_user, require_admin
├── slack_bot.py   # Slack bot: #caller threads, reminders, digests (no-op if SLACK_BOT_TOKEN unset)
├── slack_transactions.py  # transaction posts with Approve/Reject buttons
└── models/        # Beanie Documents (5)

docs/adr/          # architecture decision records
```

Routers (`app/routers/`) land in a follow-up commit.

## Auth model

JWT (HS256, signed with `AUTH_SECRET`) is returned by `/auth/login` in the response body. The SPA stores it in `localStorage` and sends it as `Authorization: Bearer <token>` on every request. CORS is allowlist-only with `allow_credentials=False` — there is no cookie session. See ADR-0002 for the trade-offs vs. `HttpOnly` cookies.

## Data model

5 Beanie `Document` classes, one per Mongoose model. `Settings.name` is overridden on each so the existing collections (`userextras`, `transactions`, `accounts`, `cardlinks`, `weeklyplans`) are addressed verbatim — no data migration. See ADR-0001 and ADR-0003.

## Deploy notes

This service expects to be deployed where the SPA can reach it over HTTPS (Render, Railway, Fly, etc.). Set:
- `FRONTEND_ORIGIN` to the deployed SPA origin (Vercel URL).
- `APP_BASE_URL` to the public URL of this API (used in Slack action buttons).

`uvicorn app.main:app --host 0.0.0.0 --port $PORT` is the typical container entrypoint.

### Railway (resume generator)

The repo ships a `Dockerfile` based on `mcr.microsoft.com/playwright/python` so Chromium and its system dependencies are pre-installed — required for `/resume/generate`'s PDF rendering. On Railway:

1. Connect the repo; Railway auto-detects the Dockerfile.
2. Bump the service to **1 GB RAM minimum** — Chromium peaks around 400 MB per request.
3. Set the envs above (`OPENAI_API_KEY`, optional `AWS_REGION`/`AWS_S3_BUCKET`/AWS credentials).
