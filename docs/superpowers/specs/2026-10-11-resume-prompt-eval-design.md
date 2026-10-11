# Resume prompt evaluator: score a profile's resume prompt against sample job descriptions

Date: 2026-10-11 · Branch: `feat/resume-prompt-eval` (backend and frontend, from `feat/new-feature`)

## Goal

Tell, with numbers, whether a change to a profile's resume prompt (or resume model) makes the tailored resumes
better. One eval run generates a resume per sample job description in parallel, then a stronger "judge" model in a
fresh context scores each one for **job match** (location ignored) and **believability**. Runs are kept so prompt
and model variants can be compared side by side, including a radar chart with one axis per role.

## Context

- A profile is an `Account` (`app/models/account.py`): `resumePromptBody` (falls back to `UserExtra.resumePromptBody`)
  and the HTML template `styleTemplate` that carries the candidate's data and CSS.
- `resume_service.generate_resume` builds one prompt (`_build_resume_prompt`), calls the router through a content-hash
  cache (`_resume_llm_output` → `ResumeLLMCache`, 7 days), splices the body back into the template
  (`_finalize_resume_html`), then renders a PDF with Playwright and uploads to S3. The eval needs everything up to
  the HTML and nothing after it.
- Models come from `app/model_catalog.py` (tasks `resume`, `cover_letter`, `screening`); `llm_router.complete` walks a
  fallback chain and takes a `validate` callback. JSON from models is parsed leniently with `_try_parse_jsonish`.
- Existing scorers (`app/job_apply/extract.py`, `app/cover_letter.py`) ask for "a single JSON object only" and
  coerce field by field.

## Decisions (user)

- The eval launches from the **profile edit page** and also has its own **Prompt Eval** tab under Resume
  (`/resume/eval`): all runs, run detail, compare, and the job-description library.
- Sample JDs live in Mongo, are edited in-app, and are **seeded** once with one realistic JD per role
  (fullstack, frontend, backend, ai, ml, llm, data, test).
- The eval form prefills the profile's prompt; the text can be edited and run **without saving**. "Save to profile"
  is a separate action. Every run snapshots the exact prompt it used.
- Match and believability per role are shown as a **radar chart** (axes = roles); compare overlays runs.
- **Redis** (30-day TTL) caches generated resume output and judge verdicts so identical inputs are never recomputed.
  `REDIS_URL` is optional; without it, or when Redis is down, the app behaves as before.

## 1. Model catalog: `judge` task

- `Task` gains `judge`; effort `high`; token profile `(7000, 1200)` for the cost estimate.
- Presets: Anthropic `opus-5-5` (Recommended), `fable-5-1` (Max quality), `sonnet-5-5` (Lower cost);
  OpenAI `gpt-6-astra` (Max quality), `gpt-5.6-sol`, `gpt-5.5` (Older flagship).
- Default `JUDGE_DEFAULT_MODEL=anthropic:claude-opus-5-5`. `GET /resume/models?task=judge` lists them.

## 2. Resume generation without the PDF

`resume_service.generate_resume_html(account, job_description, prompt_body, owner, model_id) -> (html, LlmResult)`
is the first half of `generate_resume` (template check, prompt, cached LLM call, finalize, validate).
`generate_resume` calls it and then renders/uploads as before. `plain_text_from_html(html)` is the shared text
extraction. `LlmResult.cached` says whether a cache answered.

## 3. Redis result cache

`app/redis_cache.py`: `enabled()`, `cache_key(namespace, *parts)` (`llm:<ns>:<sha256>`), `get_json`, `set_json`
(`EX` = `LLM_CACHE_TTL_SECONDS`, default 30 days), `close()`. Every failure is a warning and a miss; after a
connection error Redis is skipped for 60 s.

- Resume output: `_resume_llm_output` reads Redis (`resume`, model lane, full prompt) → Mongo `ResumeLLMCache` →
  LLM; a Mongo hit backfills Redis; an LLM answer is written to both under the lane of the model that answered.
- Judge verdict: keyed by (`judge`, `JUDGE_PROMPT_VERSION`, judge model lane, full judge prompt). Changing the
  rubric bumps the version and invalidates old verdicts.

## 4. Data model

`EvalJobDescription` (`eval_job_descriptions`): `role`, `title`, `company`, `text`, `seedKey` (`seed:<role>`,
unique sparse; `None` for user-added), `createdBy`, `archived`, `archivedAt`. Seeds are inserted with
`$setOnInsert` so edits and archiving survive restarts.

`ResumePromptEval` (`resume_prompt_evals`): `userId`, `accountId`, `accountName`, `label`, `promptBody` (snapshot),
`promptSource` (`custom|profile|global|none`), `resumeModelId`, `judgeModelId`, `status` (`running|done|failed`),
`error`, `startedAt`, `finishedAt`, `durationMs`, `rows[]`, `counts {total, done, failed}`, `avgMatch`,
`avgBelievability`, `roleScores {role: {match, believability, n}}`, `totalCostUsd`, `cachedRows`,
`cachedJudgeRows`.

Each row: JD identity (`jdId`, `role`, `jdTitle`, `jdCompany`), `status`
(`pending|generating|judging|done|failed`), `error`, generation facts (`genModelId`, `genProvider`, `genModel`,
`genFallbackUsed`, `genCached`, tokens, `genCostUsd`), judge facts (same shape with `judge*`), `resumeText`,
`resumeHtml`, `verdict`, `durationMs`.

`JudgeVerdict`: `matchScore`, `believabilityScore` (0–100), `matchNotes[]`, `believabilityNotes[]`,
`missingRequirements[]`, `redFlags[]`, `strengths[]`, `summary`.

## 5. Judge

One user message, no history. The prompt states both rubrics:

- **matchScore**: skills, depth, seniority, domain and accomplishments vs. the posting. Location, time zone,
  remote policy, relocation, citizenship, visa and work authorization are **ignored** entirely and never listed as
  missing. Required qualifications weigh far more than nice-to-haves. 90–100 shortlist; 70–89 strong with gaps;
  50–69 partial; below 50 wrong role or level.
- **believabilityScore**: does it read like a real engineer's own resume? Timeline consistency, plausible scope
  and metrics, keyword stuffing, fabrication signals (every job uses exactly the posting's stack; generic bullets),
  natural phrasing. Same bands.
- Length is not rewarded; missing contact details or formatting are not penalised.

Output: a single JSON object with the eight keys above. `parse_verdict` accepts fenced JSON, prose-wrapped JSON and
Python literals, coerces `"87%"`/`72.6` to ints, clamps to 0–100, tolerates missing lists, and raises only when a
score is missing. It is passed as `validate` to `complete("judge", ...)`, so a bad answer walks the fallback chain.

## 6. Runner

`POST /resume/eval/runs` validates everything (models, profile ownership, template, JD selection, cap of 12),
inserts the run with one `pending` row per JD and starts `run_eval` as a background task. Rows run under a
semaphore (`PROMPT_EVAL_CONCURRENCY`, default 4): generate → judge, persisting the row after each step. A row
failure (including a judge that never returns usable JSON) marks that row `failed` and keeps whatever it produced;
the run still finishes `done`. Aggregates (averages, per-role averages, cost, cache counts) are computed at the
end. A run still `running` 20 minutes after `startedAt` is reported `failed` ("timed out").

## 7. API (`/resume/eval`, authenticated)

| Route | Purpose |
|---|---|
| `GET /jds?role=&includeArchived=` | Library (shared by all users), sorted by role order then title |
| `POST /jds`, `PUT /jds/{id}`, `POST /jds/{id}/archive`, `POST /jds/{id}/unarchive` | Edit the library |
| `POST /runs` | Start a run: `accountId`, `promptBody?`, `resumeModel?`, `judgeModel?`, `roles?`, `jdIds?`, `label?` → 202 `{runId, status, jdCount}` |
| `GET /runs?accountId=&page=&limit=` | Own runs (admins: all), rows without resume text/HTML |
| `GET /runs/{id}` | Full run (owner or admin) |
| `DELETE /runs/{id}` | Delete in any status |

Prompt resolution for a run: body `promptBody` (even empty) → profile prompt → user global prompt → none.

## 8. Frontend

- `ResumeTabs` gains **Prompt Eval** (`/resume/eval`). Sub-tabs: Runs (table, profile filter, multi-select →
  Compare) and Job descriptions (library CRUD in a side panel).
- Run detail: stat tiles, role radar (match / believability toggle), prompt used, rows table; a row opens a side
  panel with the verdict lists, a sanitized preview of the generated resume (`openTemplatePreview`) and the plain
  text. "Re-run with same settings" and "Compare with…".
- Compare (2–4 runs): overlaid radar, per-JD matrix with the best cell per row emphasised, prompts side by side.
- Profile edit page: a collapsible **Prompt eval** block with "Run eval for this profile", the latest run's radar
  and the five most recent runs.
- New-eval side panel: profile, prefilled editable prompt with "Save to profile", resume model, judge model
  (`useModelChoice('judge')`), JD picker by role (cap 12), label, cost estimate.

## Out of scope

- Judging the same resume several times and averaging; a "force fresh" flag that bypasses the caches.
- Scoring PDFs or layout; the judge sees plain text only.
- Per-user Redis or provider keys.

## Risks

- Caches hide run-to-run variance for identical inputs; rows show "cached" per step so this is visible.
- A single judge call varies by a few points; read compare results for direction, not single-point differences.
- Resume HTML per row keeps preview possible; list responses strip it to stay small.
