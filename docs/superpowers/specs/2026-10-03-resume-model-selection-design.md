# Resume model selection, cover letter quality, screening model choice

Branch: `feat/resumes` (backend `engineers_backend-resumes`, frontend `engineers_app-resumes`).
Base: backend `dev`, frontend `main`.

## Goal

Let the user choose the LLM for each of three tasks on the Resumes page, with quality and cost as the guiding tradeoff:

1. **Resume** generation (HTML body rewrite).
2. **Cover letter** generation, where application quality matters most.
3. **Screening answers** for job screening questions.

Providers: Anthropic, OpenAI, and the user's existing NVIDIA free-tier presets.

## Current state

- `app/llm_router.py`: `complete_with_fallback` tries the user's NVIDIA free model, then falls back to a single server-configured OpenAI model (`RESUME_OPENAI_MODEL`, default `gpt-5-mini`). There is no per-request model choice.
- Cover letter: opt-in checkbox on `Resume.tsx`; `resume_service.generate_cover_letter` is a single call hard-wired to gpt-5-mini at low reasoning.
- Screening: `/resume/screening-answers` and `/resume/jobs/{id}/ask` both use `complete_screening`, which already has lenient output parsing and validation (kept as is).
- No Anthropic SDK in the backend.

## Design

### 1. Model catalog

New `app/model_catalog.py` with a static list of `ModelSpec`:

| field | meaning |
|---|---|
| `id` | provider-prefixed stable id, e.g. `anthropic:claude-sonnet-5-5` |
| `provider` | `anthropic` \| `openai` \| `free` |
| `api_model` | model name sent to the provider |
| `label`, `tier` | display name; tier is `free` \| `budget` \| `balanced` \| `premium` |
| `tasks` | subset of `resume`, `cover_letter`, `screening` |
| `price_in`, `price_out` | USD per 1M tokens, verified against provider pricing before ship |

Defaults per task: resume `anthropic:claude-sonnet-5-5`, cover letter `anthropic:claude-opus-5-5`, screening `anthropic:claude-sonnet-5-5`. Overridable by env (`RESUME_DEFAULT_MODEL`, `COVER_LETTER_DEFAULT_MODEL`, `SCREENING_DEFAULT_MODEL`).

Availability filtering: Anthropic models require `ANTHROPIC_API_KEY`; OpenAI models require `OPENAI_API_KEY`; free entries are derived from `FREE_LLM_PRESETS` and appear only when the user has saved an NVIDIA key. The UI is never offered a model that cannot run.

`GET /resume/models?task=...` returns the available models for the task, the flagged default, and an estimated cost per call.

### 2. Routing and providers

- New `app/llm_providers/` package, one adapter per provider behind `complete(prompt, model) -> (text, usage)`: `anthropic.py` (new), `openai.py` (extracted from `_openai_responses`), `free.py` (extracted from `_free_chat_completion`).
- `complete_with_fallback` becomes `complete(task, prompt, model_id=None, user, validate)`: resolve `model_id` through the catalog, dispatch to the adapter. Unknown or unavailable id is a 400 up front, never a silent substitution.
- On provider error or failed validation, fall back to the task default (not a fixed gpt-5-mini). `LlmResult` records `fallback_used` and the reason, as today.
- API: `GenerateBody` gains optional `resumeModel`, `coverLetterModel`, `screeningModel`. `JobAskBody` and `ScreeningBody` gain `model`. Job Applies tailor endpoints get the same optional fields. All optional, so existing callers keep default behavior.
- `ResumeApplication` gains `coverLetterLlmProvider`, `coverLetterLlmModel`, `coverLetterLlmFallbackUsed`, and `estimatedCostUsd` (computed from usage and the price table).

### 3. Cover letter pipeline

- Inputs: JD, tailored resume plain text, profile cover-letter prompt, company name, optional user "hook" line.
- Draft, critique, revise. The critique checks the draft against a rubric: specific to this JD, does not repeat resume bullets, no filler phrases, correct company and role, no fabricated claims, appropriate length. The revise step applies the critique. All steps use the selected cover-letter model.
- Deterministic post-check: no placeholder brackets, correct company name, word-count range, long dashes normalized via `_normalize_dashes`. A failure triggers one retry.
- `POST /resume/jobs/{id}/cover-letter` takes a model, regenerates the letter, and keeps the previous version for comparison.

### 4. Frontend (`engineers_app-resumes`)

- Reusable `ModelSelect` fed by `GET /resume/models`: label, tier badge, "best for" note, estimated cost, "Recommended" tag on the default.
- Placed in three spots: the resume form above Generate; the cover-letter toggle (revealed when checked, defaulting to premium); the screening Q&A box on the generated-job view.
- Last choice per task is saved in localStorage per user, falling back to the server default.
- Generated page shows which model produced each output plus cost, and a cover-letter "Regenerate with another model" action.
- If the model list fails to load, the form still works and the server picks the default.

## Testing

- Backend unit: catalog filtering by available keys; adapter dispatch with mocked providers; fallback to task default; cover-letter retry and deterministic checks; 400 on bad model ids.
- Frontend: `ModelSelect` component tests; form submission payloads.
- Manual: compare one JD across three cover-letter models before locking defaults.

## Adjustments made while planning

The implementation plan (`docs/superpowers/plans/2026-10-03-resume-model-selection.md`) refines this spec in five places; where they differ, the plan wins: single `app/llm_anthropic.py` module instead of a `llm_providers/` package; free-first default kept for resume/screening when the user has a saved NVIDIA key; frontend verified by type-check/build/manual check because the app has no test runner; OpenAI prices left unknown (`cost n/a`); `coverLetterHook` added to the generate and regenerate APIs.

## Out of scope

- Per-user provider API keys for Anthropic/OpenAI (server env only; NVIDIA key stays per user).
- Streaming output.
- Changing the screening parser or the resume HTML contract.

## Risks and notes

- `feat/job-screening` (sibling worktrees) touches `routers/resume.py` and `routers/job_applies.py`. Expect merge conflicts; decide whether to merge it first.
- Price table can go stale; it is data, easy to update, and used only for estimates.
