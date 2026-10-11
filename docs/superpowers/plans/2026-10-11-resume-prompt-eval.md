# Resume Prompt Evaluator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Score a profile's resume prompt (and model) against a library of role-tagged sample job descriptions: generate one tailored resume per JD in parallel (no PDF/S3), have a stronger judge model score each for job match (location ignored) and believability, keep runs for side-by-side comparison with a per-role radar chart, and cache every expensive result in Redis for 30 days.

**Architecture:** New `judge` task in the model catalog. `resume_service.generate_resume` is split so the LLM + finalize step runs without rendering. `app/redis_cache.py` is an optional read-through tier in front of the Mongo resume cache and around judge verdicts. `app/prompt_eval/` holds the judge prompt/parser, the parallel runner and the seed JDs; `app/routers/prompt_eval.py` serves `/resume/eval`. Frontend: a Prompt Eval tab under Resume plus a block on the profile edit page.

**Tech Stack:** FastAPI + Beanie/Motor, redis-py asyncio, pytest; React 18 + TS + Vite + Tailwind + SWR + recharts.

**Spec:** `docs/superpowers/specs/2026-10-11-resume-prompt-eval-design.md`

## Global constraints

- Backend `C:\Users\Administrator\Downloads\engineer\engineers_backend`, frontend `...\engineers_app`. Deps not installed locally: `python -m pip install -r requirements.txt -r requirements-dev.txt` (Python 3.13) and `npm install`. Unit tests: `python -m pytest tests_unit -q` (pytest.ini `testpaths=tests`, so name the dir). Integration tests need `MONGODB_URI_TEST` (unset): mark **(integration)** and report "not run". Frontend has no test runner: `npm run lint` (tsc) + `npm run build`.
- New Beanie docs: subclass `TimestampedDocument` (`app/models/base.py`), import in `app/models/__init__.py` (+ `__all__`), add to `DOCUMENT_MODELS` in `app/db.py`. Serialize with `doc_to_dict`. Validate at the router with 400s. Seeds idempotent.
- Frontend conventions: SWR inline fetchers + function-form `refreshInterval` (see `src/pages/JobApplies.tsx`), `notify`, `PageHeader`, index.css classes (`panel`, `table-wrap`, `table-head`, `table-row`, `badge-*`, `btn*`, `input`, `hint`, `text-muted`, `text-faint`, `stat-card*`), `ModelSelect` + `useModelChoice`, `Select`, `Tabs`, `Switch`, `SidePanel`, `ConfirmDialog`; API types in `src/api/endpoints.ts`; lucide icons `aria-hidden`; icon-only buttons `aria-label`; dark-mode class pairs; recharts via `useChartTheme`.
- Cost guardrail `MAX_JDS_PER_RUN = 12`; concurrency setting `PROMPT_EVAL_CONCURRENCY = 4`; stale rule: `running` with `startedAt` > 20 min → `failed`.
- Redis is **optional** like Slack: `REDIS_URL` unset or unreachable → every cache call is a logged no-op and the app behaves exactly as today. Add `redis>=5,<6` (asyncio client) to `requirements.txt`. Settings: `REDIS_URL: Optional[str] = None`, `LLM_CACHE_TTL_SECONDS: int = 2_592_000` (30 days). `.env.example` gets commented `REDIS_URL=` and `LLM_CACHE_TTL_SECONDS=2592000`. The real URL is written only to the local `.env`.
- Before writing the radar chart code, load the `dataviz` skill.

## Review focus

1. Judge JSON that is fenced / prose-wrapped / Python-literal / missing keys: lenient parse; only missing `matchScore` or `believabilityScore` fails, and that fails the **row**, never the run.
2. Resume LLM cache hit (`ResumeLLMCache`): row shows `genCached=true`, `$0` gen cost, a "cached" badge and a hint that identical reruns show no variance.
3. >12 JDs, unknown resume/judge model, profile without template, another user's profile → 400/404 before any DB write.
4. Server restart mid-run → run shows `failed` ("timed out") after 20 min, never `running` forever; delete works in any status.
5. `generate_resume` behaviour unchanged after extraction (existing unit tests pass unmodified).
6. Redis down, URL unset, or a corrupt/old-schema value: expect a warning and a normal LLM call, never a 500 or a failed row. Identical prompt + model lane → Redis hit (`cached=true`, `$0`), with no Mongo or LLM call; the same for an identical judge input.

---

## Backend

### Task 0: Branches, deps, spec + plan docs
- Both repos: `git checkout -b feat/resume-prompt-eval feat/new-feature`. Install deps; get green baselines (`python -m pytest tests_unit -q`, `npm run lint`).
- Append `REDIS_URL=<redis connection url>` to the backend's local `.env` only (git-ignored). Verify reachability once with `redis-cli -u "$REDIS_URL" ping` → `PONG`.
- Write spec + plan docs (paths above). Commit `docs(prompt-eval): design spec and plan for the resume prompt evaluator`.

### Task 1: `judge` task in the model catalog
**Files:** `app/model_catalog.py`, `app/config.py`, `.env.example`; tests `tests_unit/test_model_catalog.py`, `tests_unit/test_resume_models_endpoint.py`.
- `Task` literal gains `"judge"`; `TASK_EFFORT["judge"]="high"`; `TASK_TOKEN_PROFILE["judge"]=(7000,1200)`.
- `PRESETS[("judge","anthropic")]`: opus-5-5 "Recommended", fable-5-1 "Max quality", sonnet-5-5 "Lower cost". `PRESETS[("judge","openai")]`: gpt-6-astra "Max quality", gpt-5.6-sol, gpt-5.5 "Older flagship".
- `_configured_default` → lazy `getattr(settings, _DEFAULT_SETTING[task])` map (existing `SimpleNamespace` fixtures lack the new attr; eager dict would break them).
- `config.py`: `JUDGE_DEFAULT_MODEL: str = "anthropic:claude-opus-5-5"` after `SCREENING_DEFAULT_MODEL` (line 55); `PROMPT_EVAL_CONCURRENCY: int = 4` near `RESUME_LLM_CONCURRENCY` (line 67). `.env.example` commented entries.
- `GET /resume/models?task=judge` already works (`task: Task` Literal at `app/routers/resume.py:616`).
- Tests: add `JUDGE_DEFAULT_MODEL` to both `settings()` helpers; judge defaults/presets/cache-lane `high`; endpoint lists strong models (opus default, fable present, no haiku). Commit `feat(prompt-eval): judge task in the model catalog`.

### Task 2: Extract `generate_resume_html` + `plain_text_from_html`; `cached` flag
**Files:** `app/resume_service.py`, `app/llm_router.py`; test `tests_unit/test_resume_generate_html.py`, extend `tests_unit/test_resume_model_routing.py`.
- `plain_text_from_html(html) -> str` (the BeautifulSoup + blank-line collapse from `generate_resume`).
- `async generate_resume_html(account, job_description, prompt_body=None, owner=None, model_id=None) -> tuple[str, LlmResult]` = today's lines 567–589 (template 400, `wrap_body_for_llm(body_html_for_llm(...))`, `_build_resume_prompt`, `_resume_llm_output`, `_finalize_resume_html`, `_validate_llm_html`). `generate_resume` calls it then PDF/S3 as today. Module names (`_resume_llm_output`, `complete`, `ResumeLLMCache`) stay patchable.
- `LlmResult` gains `cached: bool = False`; cache-hit branch of `_resume_llm_output` sets it.
- Commit `refactor(resume): split generate_resume_html from PDF/S3; mark cache hits on LlmResult`.

### Task 2b: Redis result cache (1-month TTL)
**Files:** create `app/redis_cache.py`; modify `app/config.py`, `requirements.txt`, `.env.example`, `app/resume_service.py` (`_resume_llm_output`), `app/main.py` (close client on lifespan shutdown); tests `tests_unit/test_redis_cache.py`, extend `tests_unit/test_resume_model_routing.py`.
- `app/redis_cache.py` (small, pure-ish, all failures swallowed with `log.warning`):
  - `def enabled() -> bool` (URL set), `def _client()` lazily builds `redis.asyncio.from_url(url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3)`; `async def close()`.
  - `def cache_key(namespace: str, *parts: str) -> str` → `f"llm:{namespace}:{sha256('\x00'.join(parts))}"`.
  - `async def get_json(key) -> dict | None` (`json.loads`; corrupt value → `None` + warning), `async def set_json(key, value: dict, ttl: int | None = None)` (`SET key value EX ttl`, default `LLM_CACHE_TTL_SECONDS`), `async def delete(key)`.
  - Module-level `_disabled_until` back-off: after a connection error, skip Redis for 60 s so a dead instance doesn't add a timeout to every call.
- `_resume_llm_output` becomes a three-tier read-through: **Redis** (`cache_key("resume", lane, prompt)`) → Mongo `ResumeLLMCache` (unchanged, 7-day) → LLM. On a Mongo hit, backfill Redis. On an LLM answer, write Redis (under the answering model's lane) and Mongo as today. Stored value: `{"output", "model", "model_id", "provider", "fallback": bool}`. A hit returns `LlmResult(..., cached=True, usage zeros, cost_usd=0.0)`. This also gives the normal Generator a 30-day cache for free.
- Judge cache lives in Task 4 (`judge_resume`), keyed `cache_key("judge", JUDGE_PROMPT_VERSION, lane, prompt)` where `lane = cache_lane(preferred_spec, "judge")` and the prompt already embeds resume text + JD + role. Stored value: the verdict dict + `model_id/model/provider`. `JUDGE_PROMPT_VERSION` in the key means a rubric change invalidates old verdicts automatically.
- `app/main.py` lifespan: `await redis_cache.close()` on shutdown next to the other teardown.
- Tests (fake client via `monkeypatch.setattr("app.redis_cache._client", lambda: Fake)`): disabled when URL unset (no client built); get/set round-trip with TTL argument == `LLM_CACHE_TTL_SECONDS`; corrupt JSON → None; connection error → None and back-off (`_disabled_until` set, second call never touches the fake); `_resume_llm_output` Redis hit skips Mongo and `complete`; Mongo hit backfills Redis; LLM answer stored in both.
- Commit `feat(cache): optional Redis read-through for resume LLM output (30-day TTL)`.

### Task 3: Documents + seed library
**Files:** create `app/models/prompt_eval.py`, `app/prompt_eval/__init__.py`, `app/prompt_eval/seed_jds.py`; modify `app/models/__init__.py`, `app/db.py`; test `tests_unit/test_prompt_eval_seed.py`.
- `EvalRole = Literal["fullstack","frontend","backend","ai","ml","llm","data","test"]`, `EVAL_ROLES`, `MAX_JDS_PER_RUN = 12`.
- `EvalJobDescription` (collection `eval_job_descriptions`): `role, title, company, text, seedKey: Optional[str]` (`"seed:<role>"`, unique sparse index), `createdBy: Optional[PydanticObjectId]`, `archived`, `archivedAt`; index `(role, archived)`.
- `JudgeVerdict(BaseModel)`: `matchScore, believabilityScore: int`, `matchNotes, believabilityNotes, missingRequirements, redFlags, strengths: List[str]`, `summary: str`.
- `EvalRow(BaseModel)`: `jdId, role, jdTitle, jdCompany, status: Literal["pending","generating","judging","done","failed"], error`, gen fields (`genModelId, genProvider, genModel, genFallbackUsed, genCached, genInputTokens, genOutputTokens, genCostUsd`), judge fields (same shape with `judge*`, including `judgeCached`), `resumeText, resumeHtml, verdict: Optional[JudgeVerdict], durationMs`.
- `ResumePromptEval` (collection `resume_prompt_evals`): `userId, accountId, accountName, label, promptBody` (snapshot), `promptSource: Literal["custom","profile","global","none"]`, `resumeModelId, judgeModelId, status: Literal["running","done","failed"], error, startedAt, finishedAt, durationMs, rows: List[EvalRow], counts: {total,done,failed}, avgMatch, avgBelievability, roleScores: dict[str, {match: float|None, believability: float|None, n: int}]` (feeds the radar), `totalCostUsd, cachedRows`; indexes `(userId, createdAt desc)`, `(accountId, createdAt desc)`, `(status)`.
- `seed_jds.py`: `SeedJd(role,title,company,text)`, `SEED_JDS` (8 entries, 400–650 words each, realistic sections incl. a Location/work-authorization line so the ignore-location rule is exercised): fullstack "Senior Full-Stack Engineer (React/Node/PostgreSQL)" Lumen Health; frontend "Frontend Engineer, Design Systems" Northwind Commerce; backend "Backend Engineer, Payments Platform (Go/Postgres/Kafka)" Fjord Payments; ai "AI Engineer, Applied Agents (Python/RAG/tool use)" Harbor Analytics; ml "ML Engineer, Recommendations (PyTorch/Spark)" Streamline Media; llm "LLM Engineer, Evaluation & Fine-Tuning" Cobalt Labs; data "Senior Data Engineer (Airflow/dbt/Snowflake)" Meridian Logistics; test "Senior QA Automation Engineer (Playwright/API testing)" Atlas Fintech. `async seed_eval_jds() -> int` upserts with `$setOnInsert` on `{"seedKey": key}` (never overwrites edits). Called from `init_db()` in a guarded try/except mirroring `app/db.py:335-342`.
- Tests: one per role, realistic length, idempotent upsert (fake collection), models registered. Commit `feat(prompt-eval): eval JD + run documents, seeded role library`.

### Task 4: Judge prompt + parser
**Files:** create `app/prompt_eval/judge.py`; test `tests_unit/test_prompt_eval_judge.py`.
- `build_judge_prompt(*, resume_text, job_description, role) -> str`: single user message; "Reply with a single JSON object only, no prose, no markdown fences" (style of `app/job_apply/extract.py`). Rubric: **matchScore 0–100** with an explicit rule to IGNORE location/geo/time zone/remote policy/visa/work authorization (must not affect score or appear in `missingRequirements`), required > nice-to-have, bands 90+/70–89/50–69/<50. **believabilityScore 0–100**: timeline consistency, plausible scope/metrics, keyword stuffing, fabrication signals (every job uses exactly the posting's stack, generic bullets), natural phrasing; same bands. Don't reward length; don't penalize missing contact info/formatting. Required JSON keys: `matchScore, believabilityScore, matchNotes, believabilityNotes, missingRequirements, redFlags, strengths, summary`. JD via `trim_jd`, resume capped at 20k chars.
- `parse_verdict(raw) -> JudgeVerdict`: `_try_parse_jsonish`; coerce scores (int/float/"87%"), clamp 0–100, key aliases (`match_score`, …); `ValueError` when not an object or a score is missing; string lists capped (10 items × 300 chars), summary 600.
- `async judge_resume(*, resume_text, job_description, role, user, model_id) -> tuple[JudgeVerdict, LlmResult]`: build prompt; `preferred = fallback_chain("judge", model_id, user)[0]`; Redis lookup `cache_key("judge", JUDGE_PROMPT_VERSION, cache_lane(preferred, "judge"), prompt)` → on hit return `(JudgeVerdict(**value["verdict"]), LlmResult(cached=True, cost 0))`; on miss `complete("judge", prompt, user, model_id=..., validate=parse_verdict)` so garbage walks the fallback chain, then `set_json` under the answering model's lane. Fresh context by construction (one user message, no history).
- Tests: prompt contains ignore-location rule, both keys, JD/resume/role; parse plain/fenced/python-literal/embedded; clamps & coercions; missing score raises; `judge_resume` passes task `judge` + model id; Redis hit skips `complete` and returns `cached=True`; miss stores the verdict with the TTL. Commit `feat(prompt-eval): judge prompt, lenient verdict parser, Redis-cached verdicts`.

### Task 5: Parallel runner
**Files:** create `app/prompt_eval/runner.py`; test `tests_unit/test_prompt_eval_runner.py`.
- `is_stale(status, started_at, now)`, `aggregate(rows) -> dict` (`avgMatch`, `avgBelievability`, `roleScores` per role over done rows, `totalCostUsd` gen+judge, `cachedRows`, `counts`), `_slots()` semaphore from `PROMPT_EVAL_CONCURRENCY` (pattern of `resume_service._render_slots`), `_persist_row(run_id, index, row)` via `update_one({"_id"}, {"$set": {f"rows.{i}": ...}})` (no upsert), `_finish(run_id, rows, started, *, error=None)`.
- `_run_row(run, index, row, account, owner, jd_text)`: `generating` → `generate_resume_html(...)` → fill gen fields (`genCached=gen.cached`), `resumeHtml`, `resumeText=plain_text_from_html` → `judging` → `judge_resume(...)` → fill judge fields (`judgeCached=jr.cached`) → `done`; any exception → `failed` with `error`, keeping resume text if generation succeeded. Persist after each step. `aggregate` counts `cachedRows` as rows where `genCached` is true and adds `cachedJudgeRows`.
- `run_eval(run_id)`: load run/account/owner; JD texts in one `$in` query (missing → row failed); `gather(*(one(i,r)), return_exceptions=True)` under the semaphore; `_finish`. `start_run(run_id)` = `asyncio.create_task` + `_tasks` set with done-callback discard (as `app/job_apply/worker.py:552-554`). `mark_stale_runs(runs)` for list/get.
- Tests: stale rule, aggregate (ignores failed rows, role averages, all-failed → None), row happy path persists `generating/judging/done`, judge failure keeps resume text, concurrency ≤ N, missing account fails run. Commit `feat(prompt-eval): parallel generate+judge runner with per-row progress`.

### Task 6: Router `/resume/eval` + registration
**Files:** create `app/routers/prompt_eval.py`; modify `app/routers/_util.py` (add `load_owned_account`), `app/routers/resume.py` (replace `_load_owned_account` body at lines 60–65 with an import alias from `_util`), `app/main.py`; tests `tests_unit/test_prompt_eval_router.py`, `tests/test_prompt_eval.py` **(integration)**.
- `router = APIRouter(prefix="/resume/eval", tags=["prompt-eval"])`, all `Depends(get_current_user)`:
  - `GET /jds?role=&includeArchived=` → `{roles, jds}` (shared library, sorted role order then title).
  - `POST /jds` (201) `{role, title(1..200), company(1..200), text(1..40000)}`; `PUT /jds/{id}` partial (≥1 field else 400); `POST /jds/{id}/archive|unarchive`.
  - `POST /runs` (202) `{accountId, promptBody?, resumeModel?, judgeModel?, roles?: [], jdIds?: [], label?(≤120)}` → `{runId, status, jdCount}`. Validation order, all before any write: `require_model("resume"/"judge")`, `load_owned_account`, template present (400), prompt resolution `custom → profile → global → none` (pure helper `resolve_prompt`), JD selection (`jdIds` → must exist & be active; else `roles`; else all active), ≥1 and ≤12 (400). Insert run with rows sorted by role order, `start_run`.
  - `GET /runs?accountId=&page=&limit=` → `{runs: [slim], pagination}` own runs only (admin: all), stale marked first; `slim` strips `resumeHtml`/`resumeText` from rows.
  - `GET /runs/{id}` full (owner/admin else 404); `DELETE /runs/{id}` → `{ok: true}`.
- `main.py`: import + `include_router(prompt_eval.router)` after `resume.router`.
- Unit tests (bare FastAPI + overridden `get_current_user`, pattern of `tests_unit/test_resume_models_endpoint.py`): bad models 400, >12 ids 400, bad role 422, slim strips text, `resolve_prompt` order. Integration (not run here): seed present/idempotent, JD CRUD, create run persists + starts runner (patched), stale marking, other user's profile 404, delete. Commit `feat(prompt-eval): /resume/eval API (JD library, runs) and router registration`.

---

## Frontend

### Task 7: API client
**Files:** `src/api/endpoints.ts` (`ModelTask` at line 1124 gains `'judge'`; new section after `// ---------- async resume jobs ----------`).
- Types: `EvalRole`, `EVAL_ROLES`, `EvalJobDescription`, `JudgeVerdict`, `PromptEvalRowStatus`, `PromptEvalRunStatus`, `PromptEvalRow`, `PromptEvalRun` (incl. `roleScores: Record<EvalRole, {match: number|null; believability: number|null; n: number}>`).
- Functions: `listEvalJds`, `createEvalJd`, `updateEvalJd`, `archiveEvalJd`, `unarchiveEvalJd`, `createPromptEvalRun`, `listPromptEvalRuns`, `getPromptEvalRun`, `deletePromptEvalRun` using `qs`/`postJSON`/`putJSON`/`del`.
- `useModelChoice('judge')` works unchanged. Commit `feat(prompt-eval): API client types and helpers, judge model task`.

### Task 8: Shared pieces
**Files:** create `src/components/promptEval/format.ts`, `JdPicker.tsx`, `NewEvalPanel.tsx`, `RoleRadar.tsx`.
- `format.ts`: `ROLE_LABEL` (Fullstack, Frontend, Backend, AI, ML, LLM, Data, Test), status label/badge maps, `scoreClass(n)` (≥80 success, ≥65 info, ≥50 warning, else danger, null neutral), `isRunActive`, `MAX_JDS_PER_RUN`, `modelShort(id)`; reuse `formatDateTime` (`components/jobApplies/format.ts`) and `formatUsd` (`lib/modelCost.ts`).
- `JdPicker` `{value, onChange, max}`: role chips toggling all active JDs of a role; grouped checkbox list (title · company, "seed" badge); `n / 12 selected`; disables at cap.
- `NewEvalPanel` `{open, onClose, accountId?, onStarted(runId), prefill?: {promptBody, jdIds}}` on `SidePanel wide`: profile `Select` (owned, `hasTemplate`, not archived; locked when `accountId` given) → `api.getAccount` prefill prompt (fallback `api.getProfile().user.resumePromptBody`, hint states the source); `ResumePromptField` (`label="Prompt to evaluate"`, `defaultSource="global"`); "Save to profile" `btn-outline btn-sm` (`api.updateAccount`); `ModelSelect` "Resume model" (`useModelChoice('resume')`) and "Judge model" (`useModelChoice('judge')`, hint: fresh context, resume text + JD only); `JdPicker`; label input; footer estimate `selected × (resume est + judge est)` and `btn` "Run eval". `dirty` when prompt differs from prefill.
- `RoleRadar` `{series: {id, label, color, scores: Record<EvalRole, number|null>}[], metric: 'match'|'believability'}` using recharts `RadarChart/PolarGrid/PolarAngleAxis/PolarRadiusAxis(domain [0,100], ticks 20..100)/Radar/Tooltip/Legend` in `ResponsiveContainer` (height ~320), `useChartTheme` for grid/axis/tooltip colors, axes in fixed `EVAL_ROLES` order labelled via `ROLE_LABEL`, missing roles plotted as null; one filled `Radar` per series (`fillOpacity` ~0.25), series palette with dark-mode pairs; a `Segmented`-style toggle (match / believability) rendered by the parent. Load the `dataviz` skill before writing it.
- `npm run lint`. Commit `feat(prompt-eval): new-eval panel, JD picker, role radar and formatting helpers`.

### Task 9: Eval page, routes, tab
**Files:** create `src/pages/ResumePromptEval.tsx`, `src/components/promptEval/RunsTable.tsx`, `RunDetail.tsx`, `RowDetailPanel.tsx`, `CompareView.tsx`, `JdLibrary.tsx`; modify `src/App.tsx`, `src/components/ResumeTabs.tsx`.
- `ResumeTabs`: `{ to: '/resume/eval', label: 'Prompt Eval', icon: FlaskConical, exact: false }` between Generated Resumes and Prompts.
- `App.tsx`: `/resume/eval`, `/resume/eval/compare`, `/resume/eval/:runId` → `<Protected><ResumePromptEval/></Protected>` (RoleGuard already allows `/resume`).
- Page: `PageHeader title="Resume Prompt Eval" action={New eval btn}`, `<ResumeTabs/>`; branch on `runId` → `RunDetail`; `/compare?ids=a,b` → `CompareView`; else `Tabs` (`runs` | `jds`, `?tab=`). `NewEvalPanel` state here; `onStarted` navigates to the run.
- `RunsTable`: SWR `['prompt-eval-runs', accountId]` with 3 s polling while any run is active; profile filter; columns: compare checkbox, date, profile, label, resume/judge model (mono), JDs done/total (+failed), avg match, avg believability (score badges), cost, status (+ "cached n" badge w/ tooltip), Open, Delete (`ConfirmDialog`). Sticky "Compare n runs" bar (2–4 selected) → `/resume/eval/compare?ids=`.
- `RunDetail`: SWR `['prompt-eval-run', id]` polling while running; stat tiles (avg match, avg believability, cost, duration, done/total, status, models, profile link); **`RoleRadar` with one series (this run) + metric toggle**; collapsible "Prompt used" `<pre>` with source badge; rows table (role, JD title · company, gen model + cached/fallback badges, judge model + cached badge, match, believability, status/error, duration) → click opens `RowDetailPanel`; "Re-run with same settings" (opens `NewEvalPanel` prefilled) and "Compare with…".
- `RowDetailPanel` (`SidePanel wide`): summary, two score badges, lists (Strengths, Missing requirements, Red flags, Match notes, Believability notes), model/tokens/cost lines, "Preview resume" → `openTemplatePreview(row.resumeHtml, ...)` (`lib/templatePreview.ts`, try/catch → `notify.error`), "Copy text", plain text `<pre>`.
- `CompareView` `{ids}` (max 4): `Promise.all(getPromptEvalRun)`; **`RoleRadar` with one series per run overlaid** + metric toggle; column headers per run (date, label, models, avg match/believability, cost, collapsible prompt); matrix rows keyed by `jdId` (union) with `match / believability` badges, "—" when absent, "failed" badge on error, best per row `font-semibold`; "Prompt differences" side-by-side `<pre>` columns.
- `JdLibrary`: role filter `Select`, "Show archived" `Switch`, "Add job description" `btn`; table (role, title, company, length, seed/archived badges, Edit / Archive (`ConfirmDialog` tone default) / Unarchive); `SidePanel` editor (role `Select`, title/company `input`, textarea rows 18, maxLength 40000 + counter) → create/update, `mutate(['prompt-eval-jds', …])`.
- `npm run lint`, `npm run build`, manual check. Commit `feat(prompt-eval): Prompt Eval tab with runs, detail, radar, compare and JD library`.

### Task 10: Profile edit page block
**Files:** create `src/components/promptEval/PromptEvalBlock.tsx`; modify `src/pages/AccountEdit.tsx` (render after `<PromptsBlock accountId={id!} />`, line 245).
- Copy the collapsible pattern of `PromptsBlock` (AccountEdit.tsx:260–342: header button, `ChevronDown/ChevronRight`, `aria-expanded`, open state in localStorage `profileEdit.promptEvalOpen`). Header summary "Last run: 82 match · 74 believability · Oct 11" or "No evals yet". Body: `btn` "Run eval for this profile" (opens `NewEvalPanel` with `accountId`; `onStarted` → `navigate('/resume/eval/'+runId)`), small `RoleRadar` of the latest done run, compact table of 5 most recent runs (SWR `['prompt-eval-runs', accountId]`, `limit: 5`, polling while active), `link-inline` "All runs" → `/resume/eval`.
- `npm run lint`, `npm run build`. Commit `feat(prompt-eval): eval launcher, radar and recent runs on the profile page`.

---

## Wrap-up (Task 11)
- Backend `readme.md`: `JUDGE_DEFAULT_MODEL`, `PROMPT_EVAL_CONCURRENCY`, `REDIS_URL` + `LLM_CACHE_TTL_SECONDS` (optional; resume output and judge verdicts cached 30 days, keyed by content hash and model lane), `/resume/eval` routes, seed library note (inserted once, never overwritten).
- `python -m pytest tests_unit -q` all green; `tests/` only if `MONGODB_URI_TEST` set, otherwise report "integration not run". `npm run lint` + `npm run build`.
- Commit `docs(prompt-eval): settings and API notes`. Do not push (remotes per `AGENTS.md`) unless asked.

## Verification (end to end)
1. Backend: `.env` already has `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `MONGODB_URI`. Run `python -m uvicorn app.main:app --port 8080`; check logs for "seeded 8 eval job descriptions" on first boot and nothing on the second.
2. `GET /resume/models?task=judge` lists Opus 5.5 as default.
3. Frontend `npm run dev`; open a profile with a template → "Prompt eval" block → Run eval with 2 JDs (e.g. frontend + data), Sonnet 5.5 for resume, Opus 5.5 judge. Watch the run page poll to `done`; rows show scores, cached badge appears on an identical rerun; preview opens the sanitized resume; radar shows the two roles.
4. Edit the prompt in the panel, run again, select both runs → Compare: radar overlays both, matrix shows per-JD deltas, prompts side by side.
4b. Redis: rerun step 3 unchanged → every row shows "cached" for both generation and judge, run cost `$0`, and `redis-cli -u "$REDIS_URL" --scan --pattern 'llm:*'` lists `llm:resume:*` and `llm:judge:*` keys with `TTL` ≈ 2,592,000 s. Stop Redis access (set a wrong `REDIS_URL`) and rerun: a warning is logged, the run still completes via the LLM.
5. JD library: add a JD, archive it, confirm it disappears from the picker; unarchive.

## Risks / notes
- Caches hide variance for identical prompt+JD+model (shown as "cached" per step; by design, to avoid recomputation; a "force fresh" nonce could be added later). The judge cache key includes `JUDGE_PROMPT_VERSION`, so editing the rubric invalidates old verdicts without a flush.
- Redis credentials: the pasted URL contains the password. It lives only in the local `.env` (and Railway service variables when deployed); never in commits, docs, memory or chat output.
- Judge scores from a single call vary by a few points; compare for direction, not single-point differences (documented in the spec). "Judge N times and average" is out of scope.
- `_configured_default` must become lazy `getattr` or existing SimpleNamespace fixtures break.
- Document size: 12 rows × ~150 KB HTML is fine; list endpoint strips text/html.
- Shared JD library editable by any staff user (same model as Job Applies corrections); seeds use `$setOnInsert`.
