# Resume Model Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users pick the LLM for resume generation, cover letters and screening answers (Anthropic, OpenAI, NVIDIA free), with a higher-quality draft/critique/revise cover-letter pipeline.

**Architecture:** A static model catalog (availability filtered by configured keys) feeds one router entry point, `complete(task, ...)`, which walks a fallback chain (requested model, task default, configured OpenAI model). Requested model ids are stored on the job so queue retries honor them. The frontend fetches the catalog per task and sends the chosen id.

**Tech Stack:** FastAPI, Beanie/Mongo, `anthropic>=1,<2` SDK (new), `openai` SDK (existing), pytest + pytest-asyncio; React 18 + TypeScript + SWR + Tailwind (Vite).

**Spec:** `docs/superpowers/specs/2026-10-03-resume-model-selection-design.md`

## Plan-time adjustments to the spec

1. The Anthropic adapter is one module, `app/llm_anthropic.py`. The OpenAI and NVIDIA callers stay in `app/llm_router.py` (no `llm_providers/` package). Less churn, same behavior.
2. When no model is requested for **resume** or **screening** and the user has a saved NVIDIA key, their saved free preset is tried first (today's behavior), then the task default. **Cover letter** never auto-uses a free model.
3. The frontend has no test runner. Frontend tasks are verified with `npm run lint`, `npm run build` and a manual check. No test dependency is added.
4. OpenAI prices are not verified, so `price_in`/`price_out` are `None` and the UI shows "cost n/a" for that model. Anthropic prices come from the Claude API price table cached 2026-09-25.
5. `coverLetterHook` (optional one-line hook) is added to the generate and regenerate APIs, since the spec lists it as a cover-letter input.

## Global Constraints

- Repos and branches: backend `engineers_backend-resumes` (`feat/resumes`, from `dev`), frontend `engineers_app-resumes` (`feat/resumes`, from `main`). Backend paths below are relative to the backend worktree, frontend paths to the frontend worktree.
- Task ids: `resume`, `cover_letter`, `screening`. Model ids are provider-prefixed: `anthropic:claude-haiku-4-5`, `anthropic:claude-sonnet-5-5`, `anthropic:claude-opus-5-5`, `anthropic:claude-fable-5-1`, `openai:<RESUME_OPENAI_MODEL>`, `free:<preset id>`.
- Defaults: resume `anthropic:claude-sonnet-5-5`, cover letter `anthropic:claude-opus-5-5`, screening `anthropic:claude-sonnet-5-5` (env: `RESUME_DEFAULT_MODEL`, `COVER_LETTER_DEFAULT_MODEL`, `SCREENING_DEFAULT_MODEL`).
- Anthropic prices ($/1M in/out): haiku-4-5 1/5, sonnet-5-5 2/10, opus-5-5 4/20, fable-5-1 10/50. Free models: 0/0. OpenAI: unknown (`None`).
- Anthropic calls: no `thinking` param, no `temperature`/`top_p`, no forced `tool_choice`, no assistant prefill (Sonnet 5.5 / Opus 5.5 reject them). Depth is set only via `output_config={"effort": ...}`, and only for models with `supports_effort=True` (not Haiku 4.5). Use streaming + `get_final_message()`, `max_tokens=16000`.
- Task effort: resume `low`, screening `low`, cover letter `high`.
- Long dashes in generated text are normalized with `resume_service._normalize_dashes`.
- Run backend tests from the backend worktree root with `PY=C:/Users/Administrator/Documents/Engineer/engineers_backend/.venv/Scripts/python.exe`: `$PY -m pytest <path> -q`. Unit tests live in `tests_unit/`, integration tests in `tests/`. Integration tests need `MONGODB_URI_TEST` (a throwaway DB, currently unset here): tasks mark them **(integration)**; if the variable is unset, say so instead of claiming they pass.
- Every commit message ends with the trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Do not push (`origin` pushes go through the `spark` remote per `AGENTS.md`, only when the user asks).

## Review Focus

1. Requested model id is unknown, wrong for the task, or its provider key is missing: expect a 400 naming the problem, never a 500 or a silent substitution (Tasks 1, 4, 5, 6, 8).
2. Anthropic returns `refusal` or hits `max_tokens` (truncated HTML or JSON): expect fallback to the next model in the chain, never saved as output (Tasks 2, 3).
3. Cover letter contains `[Your Name]`-style placeholders, omits the company, or the critique says OK: expect one retry at most, `OK` skips revise, and the job never fails because of the letter (Task 7).
4. A failed job is retried or the queue restarts: expect the same chosen models to be used again (Task 5).
5. A user with a saved NVIDIA key and no explicit choice keeps free-first behavior for resume and screening, and the cover letter never silently goes free (Tasks 1, 3).

---

## Backend

### Task 1: Model catalog and settings

**Files:**
- Create: `app/model_catalog.py`
- Modify: `app/config.py` (add settings after `RESUME_OPENAI_MODEL`), `.env.example`
- Test: `tests_unit/test_model_catalog.py`

**Interfaces:**
- Produces (all in `app/model_catalog.py`):
  - `Task = Literal["resume","cover_letter","screening"]`, `Tier = Literal["free","budget","balanced","premium"]`
  - `@dataclass(frozen=True) ModelSpec(id: str, provider: Literal["anthropic","openai","free"], api_model: str, label: str, tier: Tier, tasks: frozenset[Task], best_for: str, price_in: Optional[float], price_out: Optional[float], supports_effort: bool = False)`
  - `class ModelNotAvailable(ValueError)`
  - `TASK_EFFORT: dict[Task, str]`, `TASK_TOKEN_PROFILE: dict[Task, tuple[int, int]]` (typical input/output tokens: resume 6000/5000, cover_letter 21000/1700 for all three calls, screening 4000/800)
  - `available_models(task: Task, user: Optional[UserExtra]) -> list[ModelSpec]` (catalog order: anthropic, openai, free)
  - `get_model(model_id: str, user: Optional[UserExtra]) -> Optional[ModelSpec]` (any task, availability-filtered)
  - `default_model_id(task: Task, user: Optional[UserExtra]) -> Optional[str]`
  - `validate_requested(task: Task, model_id: Optional[str], user: Optional[UserExtra]) -> Optional[ModelSpec]` (`None`/blank returns `None`; raises `ModelNotAvailable` with a message naming the id and the reason)
  - `fallback_chain(task: Task, requested_id: Optional[str], user: Optional[UserExtra]) -> list[ModelSpec]`
  - `cache_lane(spec: ModelSpec, task: Task, user: Optional[UserExtra]) -> str`
  - `estimate_call_cost(spec: ModelSpec, task: Task) -> Optional[float]`, `usage_cost(spec: ModelSpec, usage: dict) -> Optional[float]`
  - `public_view(spec: ModelSpec, task: Task, default_id: Optional[str]) -> dict` with keys `id, label, provider, tier, bestFor, estCostUsd, isDefault`
- Consumes: `get_settings()` (imported at module level so tests can monkeypatch `app.model_catalog.get_settings`), `app.free_llm.FREE_LLM_PRESETS/get_preset/clamp_max_tokens`.

Catalog content: haiku-4-5 (`budget`, tasks resume+screening), sonnet-5-5 (`balanced`, all), opus-5-5 (`premium`, all), fable-5-1 (`premium`, cover_letter only, best_for "Maximum quality, most expensive"), one OpenAI entry from `RESUME_OPENAI_MODEL` (`budget`, all tasks), one `free:<preset.id>` entry per preset (`free`, tasks resume+screening, prices 0). Each needs a one-line `best_for`. `supports_effort=True` for sonnet and opus (and fable).

- [ ] **Step 1: Write failing tests** in `tests_unit/test_model_catalog.py` (copy the `os.environ.setdefault` header from `tests_unit/test_llm_router.py`; use a `settings()` helper returning `SimpleNamespace(ANTHROPIC_API_KEY, OPENAI_API_KEY, RESUME_OPENAI_MODEL="gpt-5-mini", RESUME_REASONING_EFFORT="low", RESUME_DEFAULT_MODEL=..., COVER_LETTER_DEFAULT_MODEL=..., SCREENING_DEFAULT_MODEL=...)` patched via `monkeypatch.setattr("app.model_catalog.get_settings", ...)`; users are `SimpleNamespace(freeLlmModelId, freeLlmApiKey, freeLlmMaxTokens)`). Tests:
  - `test_anthropic_models_need_api_key`: no key means no `anthropic:*` ids; key set means all four.
  - `test_free_models_need_user_key`: `free:*` ids appear only when `user.freeLlmApiKey` is non-blank.
  - `test_cover_letter_excludes_free_and_haiku`: ids for `cover_letter` contain neither `free:` nor `haiku`; contain `fable-5-1`.
  - `test_defaults_per_task`: resume/screening default `anthropic:claude-sonnet-5-5`, cover_letter `anthropic:claude-opus-5-5`.
  - `test_default_prefers_saved_free_preset_for_resume_and_screening_not_cover_letter`: user with key and `freeLlmModelId="glm-5.2"` gives `free:glm-5.2` for resume and screening, `anthropic:claude-opus-5-5` for cover_letter.
  - `test_default_falls_back_to_openai_without_anthropic_key`: no Anthropic key, OpenAI key set gives `openai:gpt-5-mini`; neither key and no free gives `None`.
  - `test_validate_requested_blank_returns_none`; `test_validate_requested_unknown_id_raises` (message contains the id); `test_validate_requested_missing_provider_key_raises` (message mentions "not configured"); `test_validate_requested_wrong_task_raises` (`anthropic:claude-haiku-4-5` for `cover_letter`).
  - `test_fallback_chain_order_and_dedup`: requested haiku for resume gives `[haiku, sonnet, openai]`; requested sonnet gives `[sonnet, openai]`; no request and free-preferring user gives `[free, sonnet, openai]`.
  - `test_usage_cost`: sonnet with `{"input": 1_000_000, "output": 1_000_000}` is `12.0`; OpenAI is `None`; free is `0.0`.
  - `test_estimate_call_cost_resume_sonnet`: `0.062`.
  - `test_cache_lane`: openai gives `openai:gpt-5-mini:low`; free gives `free:<api_model>:<clamped max tokens>`; anthropic gives `anthropic:claude-sonnet-5-5:low` (effort from `TASK_EFFORT`, or `none` when `supports_effort` is false).
- [ ] **Step 2: Run** `$PY -m pytest tests_unit/test_model_catalog.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `app/model_catalog.py` per the interfaces above, and add to `Settings`: `ANTHROPIC_API_KEY: Optional[str] = None`, `RESUME_DEFAULT_MODEL`, `COVER_LETTER_DEFAULT_MODEL`, `SCREENING_DEFAULT_MODEL` (defaults from Global Constraints). `default_model_id`: if the user has a saved free preset and task is resume/screening return it; else the configured default if available; else the OpenAI entry if available; else `None`. `fallback_chain`: `[requested or default] + [task default] + [openai]`, filtered to available-for-task and deduped, order preserved. Add the four settings (commented) to `.env.example`.
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat(resume): model catalog with availability, defaults and fallback chain`.

### Task 2: Anthropic provider adapter

**Files:**
- Create: `app/llm_anthropic.py`
- Modify: `requirements.txt` (add `anthropic>=1.0,<2`)
- Test: `tests_unit/test_llm_anthropic.py`

**Interfaces:**
- Produces: `class ModelRefused(RuntimeError)`, `class OutputTruncated(RuntimeError)`, `ANTHROPIC_TIMEOUT_SECONDS = 300`, `async def anthropic_complete(api_model: str, prompt: str, *, effort: Optional[str] = None, max_tokens: int = 16000, timeout: float = ANTHROPIC_TIMEOUT_SECONDS) -> tuple[str, dict]` (usage dict is `{"input": int, "output": int}`).
- Consumes: `get_settings().ANTHROPIC_API_KEY`.

- [ ] **Step 1: Install** `anthropic` in the shared venv: `$PY -m pip install "anthropic>=1.0,<2"`; expect success.
- [ ] **Step 2: Write failing tests** with `monkeypatch.setattr("app.llm_anthropic.AsyncAnthropic", FakeClient)` (fake exposes `messages.stream(**kw)` as an async context manager whose `get_final_message()` returns `SimpleNamespace(content=[...], stop_reason=..., usage=SimpleNamespace(input_tokens=.., output_tokens=..))`; record kwargs):
  - `test_returns_text_blocks_only_and_usage`: a `thinking` block plus two `text` blocks gives the joined text only; usage maps to `input`/`output`.
  - `test_request_has_no_thinking_or_sampling_params`: kwargs lack `thinking`, `temperature`, `top_p`, `tool_choice`; contain `model`, `max_tokens=16000`, one user message.
  - `test_effort_sent_only_when_given`: `effort="low"` gives `output_config == {"effort": "low"}`; `None` gives no `output_config`.
  - `test_refusal_raises_model_refused`; `test_max_tokens_stop_raises_output_truncated`; `test_missing_key_raises_runtime_error`.
- [ ] **Step 3: Run** `$PY -m pytest tests_unit/test_llm_anthropic.py -q`. Expected: FAIL.
- [ ] **Step 4: Implement** `anthropic_complete` using `AsyncAnthropic(api_key=..., timeout=...)` and `async with client.messages.stream(...) as stream: msg = await stream.get_final_message()`. Check `stop_reason` before reading content.
- [ ] **Step 5: Run** tests. Expected: PASS.
- [ ] **Step 6: Commit** `feat(resume): Anthropic adapter (streaming, no thinking/sampling params)`.

### Task 3: Router `complete()` with fallback chain

**Files:**
- Modify: `app/llm_router.py`
- Test: `tests_unit/test_llm_router_complete.py`

**Interfaces:**
- Consumes: Task 1 (`fallback_chain`, `validate_requested`, `usage_cost`, `ModelSpec`, `ModelNotAvailable`), Task 2 (`anthropic_complete`).
- Produces:
  - `LlmProvider = Literal["free","openai","anthropic"]`; `LlmResult` gains `model_id: str = ""` and `cost_usd: Optional[float] = None`.
  - `async def complete(task: Task, prompt: str, user: Optional[UserExtra], *, model_id: Optional[str] = None, validate: Optional[Callable[[str], None]] = None) -> LlmResult`
  - `async def _call_spec(spec: ModelSpec, prompt: str, task: Task, user: Optional[UserExtra]) -> tuple[str, dict]` (module-level so tests can patch it)
  - `complete_resume(prompt, user, model_id=None)` and `complete_screening(prompt, user, questions, model_id=None)` delegate to `complete`.
- `complete_with_fallback` and `complete_openai_only` stay unchanged (`app/job_apply/extract.py` uses the former).

Behavior: `validate_requested(task, model_id, user)` first (raises `ModelNotAvailable`). Then walk `fallback_chain`; per spec call `_call_spec`, reject empty text, run `validate`. First success returns `LlmResult(provider=spec.provider, model=spec.api_model, model_id=spec.id, fallback_used=index>0, fallback_reason=<message of first failure>, cost_usd=usage_cost(spec, usage))`. All failed: re-raise the last exception. Empty chain: `RuntimeError("No model available for <task>: configure an API key")`. `_call_spec` dispatch: anthropic goes to `anthropic_complete(spec.api_model, prompt, effort=TASK_EFFORT[task] if spec.supports_effort else None)`; openai goes to `_openai_responses(prompt, spec.api_model)`; free goes to `_free_chat_completion` with the preset's `clamp_max_tokens(user.freeLlmMaxTokens, preset)`, the user's key, and the existing purpose-based timeout/max-token helpers (cover_letter uses purpose `"resume"`). Failure reasons for free models reuse `_free_llm_user_message`; others use `str(exc)[:300]`.

- [ ] **Step 1: Write failing tests** (patch `app.llm_router._call_spec` and `app.llm_router.fallback_chain`/catalog settings as needed):
  - `test_first_model_succeeds`: `fallback_used is False`, `model_id` set, `cost_usd` computed.
  - `test_falls_back_when_first_raises`: second used, `fallback_used is True`, `fallback_reason` non-empty.
  - `test_refusal_and_truncation_fall_back` (Review Focus 2): first raises `ModelRefused`, then `OutputTruncated` on a second run; both reach the next model.
  - `test_validate_failure_falls_back`: `validate` raising `ValueError` on the first output uses the second model.
  - `test_all_fail_raises_last_error`; `test_empty_chain_raises_runtime_error`.
  - `test_unavailable_requested_model_raises_model_not_available` (Review Focus 1).
  - `test_free_first_when_no_model_requested_for_screening_but_not_cover_letter` (Review Focus 5): with a free-key user and a recording `_call_spec`, the first spec for screening is `free:*`, for cover_letter `anthropic:claude-opus-5-5`.
  - `test_complete_screening_passes_model_id`.
- [ ] **Step 2: Run** `$PY -m pytest tests_unit/test_llm_router_complete.py tests_unit/test_llm_router.py -q`. Expected: new tests FAIL, old tests still pass.
- [ ] **Step 3: Implement** as above in `app/llm_router.py`.
- [ ] **Step 4: Run** both files. Expected: PASS.
- [ ] **Step 5: Commit** `feat(resume): router complete() with per-task model chain`.

### Task 4: `GET /resume/models` and shared model validation

**Files:**
- Modify: `app/routers/resume.py` (new route above `/jobs`), `app/routers/_util.py` (add `require_model`)
- Test: `tests/test_resume_models.py` **(integration)**

**Interfaces:**
- Produces: `def require_model(task: Task, model_id: Optional[str], user: UserExtra) -> Optional[str]` in `_util.py` (returns the stripped id or `None`; maps `ModelNotAvailable` to `HTTPException(400, detail=str(exc))`); route `GET /resume/models?task=resume|cover_letter|screening` returning `{"task": ..., "defaultId": str|None, "models": [public_view(...)]}`.
- Consumes: Task 1.

- [ ] **Step 1: Write failing tests** using fixtures from `tests/test_resume_queue.py` (`client`, `staff_user`, `auth_header`) and patching `app.model_catalog.get_settings`:
  - `test_models_requires_auth` (401).
  - `test_models_lists_anthropic_when_key_configured`: `defaultId == "anthropic:claude-sonnet-5-5"` for resume, exactly one `isDefault` true.
  - `test_models_cover_letter_excludes_haiku_and_free`.
  - `test_models_empty_when_nothing_configured`: `models == []`, `defaultId is None`.
  - `test_models_invalid_task_422`.
- [ ] **Step 2: Run** `$PY -m pytest tests/test_resume_models.py -q` (needs `MONGODB_URI_TEST`). Expected: FAIL.
- [ ] **Step 3: Implement** route and `require_model`.
- [ ] **Step 4: Run** tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(resume): GET /resume/models and require_model helper`.

### Task 5: Resume generation uses the requested model; job fields; cost; Job Applies passthrough

**Files:**
- Modify: `app/models/resume_application.py`, `app/resume_queue.py` (`enqueue_generation`), `app/resume_service.py` (`_resume_llm_output`, `generate_resume`, `GeneratedResume`, remove `_openai_lane/_free_lane`), `app/routers/resume.py` (`GenerateBody`, `generate`, `_run_job`, `_reset_failed_job_fields`, `_job_to_dict`), `app/routers/job_applies.py` (`TailorBody`, `TailorAllBody`, `_tailor_row`, both routes)
- Test: `tests/test_resume_model_routing.py` **(integration)**; extend `tests/test_resume_queue.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4.
- Produces:
  - `ResumeApplication` new fields: `resumeModelId`, `coverLetterModelId`, `screeningModelId`: `Optional[str]`; `coverLetterHook: Optional[str]`; `estimatedCostUsd: Optional[float]`. `_job_to_dict` returns all of these under the same names.
  - `enqueue_generation(..., resume_model: Optional[str] = None, cover_letter_model: Optional[str] = None, screening_model: Optional[str] = None, cover_letter_hook: Optional[str] = None)`.
  - `GenerateBody` gains `resumeModel`, `coverLetterModel`, `screeningModel`, `coverLetterHook` (all `Optional[str]`, hook max 500 chars). `generate` validates each model with `require_model` (resume/cover_letter/screening) before enqueue.
  - `generate_resume(..., model_id: Optional[str] = None)`; `GeneratedResume` gains `llm_model_id: Optional[str]` and `cost_usd: Optional[float]`.
  - `_resume_llm_output(prompt: str, user: Optional[UserExtra], template: str, model_id: Optional[str] = None) -> LlmResult`: look up `ResumeLLMCache` under `cache_lane(preferred_spec, "resume", user)` where `preferred_spec = fallback_chain("resume", model_id, user)[0]`; a hit returns an `LlmResult` with zero usage and `cost_usd=0.0`; a miss calls `complete("resume", prompt, user, model_id=model_id, validate=<finalize + _validate_llm_html>)` and stores under the lane of the model that actually answered.
  - `TailorBody` / `TailorAllBody` gain `resumeModel`, `coverLetterModel` (optional); `_tailor_row(..., resume_model=None, cover_letter_model=None)` validates with `require_model` and forwards to `enqueue_generation`.
- `_run_job` reads `job.resumeModelId` etc. (the job is already loaded there), records `resumeLlm*` as today, and adds each step's `cost_usd` into `job.estimatedCostUsd` (sum, `None`-safe). `_reset_failed_job_fields` clears `estimatedCostUsd` but keeps the model-id inputs.

- [ ] **Step 1: Write failing tests**:
  - extend `tests/test_resume_queue.py`: `test_generate_stores_requested_models` (POST with `resumeModel`, `coverLetterModel`, `screeningModel`, `coverLetterHook`; job stores them), `test_generate_rejects_unknown_model_with_400` (Review Focus 1: body `{"resumeModel": "anthropic:nope"}`; no job inserted), `test_retry_keeps_requested_models` (Review Focus 4).
  - `tests/test_resume_model_routing.py`: `test_cache_hit_on_preferred_lane_skips_llm` (insert `ResumeLLMCache` under the lane for the requested model; patch `app.resume_service.complete` to fail the test if called), `test_cache_miss_calls_complete_with_model_and_stores_under_answering_lane` (patch `complete` to return an `LlmResult` for another model_id; assert stored `promptHash` matches that model's lane), `test_job_applies_tailor_validates_and_forwards_models`.
- [ ] **Step 2: Run** `$PY -m pytest tests/test_resume_queue.py tests/test_resume_model_routing.py -q`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** the changes listed above.
- [ ] **Step 4: Run** the same command plus `$PY -m pytest tests_unit -q`. Expected: PASS (integration only if `MONGODB_URI_TEST` is set; otherwise state that they were not run).
- [ ] **Step 5: Commit** `feat(resume): per-task model ids on jobs, router-backed resume generation, cost tracking`.

### Task 6: Screening uses the requested model

**Files:**
- Modify: `app/routers/resume.py` (`ScreeningBody`, `JobAskBody`, `_run_screening`, `screening_answers`, `ask_job_screening`, `_run_job` screening step)
- Test: extend `tests/test_resume_model_routing.py` **(integration)**

**Interfaces:**
- Consumes: Tasks 3, 4, 5.
- Produces: `ScreeningBody.model` and `JobAskBody.model` (`Optional[str]`); `_run_screening(..., model_id: Optional[str] = None)` and `ScreeningResult` gains `cost_usd: Optional[float]`. The `use_free_llm` flag keeps its meaning: when `False` the user is not passed to the router (so no free-first). `_run_screening` calls `complete_screening(prompt, owner_or_none, questions, model_id=model_id)`; the former `complete_openai_only` branch is removed. The ask endpoint adds the cost to `job.estimatedCostUsd`; the job step uses `job.screeningModelId`.

- [ ] **Step 1: Write failing tests**: `test_ask_forwards_requested_model` (patch `app.routers.resume.complete_screening` to record `model_id`; POST `/resume/jobs/{id}/ask` with `"model": "anthropic:claude-opus-5-5"`), `test_ask_unknown_model_400`, `test_standalone_screening_unknown_model_400`.
- [ ] **Step 2: Run** `$PY -m pytest tests/test_resume_model_routing.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file plus `$PY -m pytest tests_unit/test_llm_router.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(resume): selectable model for screening answers`.

### Task 7: Cover letter pipeline (draft, critique, revise)

**Files:**
- Create: `app/cover_letter.py`
- Modify: `app/resume_service.py` (delete `_COVER_LETTER_SYSTEM`, `generate_cover_letter`, `_openai_call`, `_resume_model_and_effort`, unused imports), `app/models/resume_application.py` (add `coverLetterLlmProvider`, `coverLetterLlmModel`, `coverLetterLlmFallbackUsed`, `coverLetterLlmFallbackReason`), `app/routers/resume.py` (`_run_job` step 4, `_reset_failed_job_fields`, `_job_to_dict`)
- Test: `tests_unit/test_cover_letter.py`

**Interfaces:**
- Consumes: Tasks 3 and 5 (`complete`, `LlmResult`, job model-id fields).
- Produces (`app/cover_letter.py`):
  - `@dataclass CoverLetterResult(text: str, provider: str, model: str, model_id: str, fallback_used: bool, fallback_reason: Optional[str], cost_usd: Optional[float], problems: list[str])`
  - `check_cover_letter(text: str, company: str, *, enforce_length: bool) -> list[str]` (problems: placeholder patterns `[...]`, `{{...}}`, `<...>`, "Your Name"; company name missing, case-insensitive; word count outside 150-450 only when `enforce_length`)
  - `async def generate_cover_letter(*, resume_text: str, job_description: str, company: str, prompt_body: str, user: Optional[UserExtra], model_id: Optional[str], hook: Optional[str] = None) -> CoverLetterResult`
  - prompt builders `build_draft_prompt`, `build_critique_prompt`, `build_revise_prompt` (module-private is fine). The draft prompt keeps the existing `_COVER_LETTER_SYSTEM` text verbatim, plus the hook line when given.
- Pipeline: draft with `complete("cover_letter", ..., model_id=model_id)`. Critique with the **same actual model** (`model_id=draft.model_id`), asked to reply exactly `OK` or a short bullet list against the rubric (specific to this JD, no resume-bullet repetition, no filler, correct company/role, no fabricated claims, length). If the critique is `OK`, the draft is final (2 calls). Otherwise revise with the critique (3 calls). Run `check_cover_letter` (`enforce_length` only when `prompt_body` is blank); if problems remain, one more revise call with the problems appended, then return whatever came back (log a warning, put problems in `problems`, never raise for them). Normalize output with `_normalize_dashes(_strip_fences(...))`. `cost_usd` sums all calls; fallback info comes from the first call that fell back.
- `_run_job` step 4 calls `generate_cover_letter` with `job.coverLetterModelId` and `job.coverLetterHook`, stores text, the four `coverLetterLlm*` fields, adds cost; stays inside the existing non-fatal `try/except`.

- [ ] **Step 1: Write failing tests** with `monkeypatch.setattr("app.cover_letter.complete", fake)` where `fake` is an async recorder returning scripted `LlmResult`s (`model_id="anthropic:claude-opus-5-5"`, `cost_usd=0.01`):
  - `test_check_flags_placeholders`, `test_check_flags_missing_company_case_insensitive`, `test_check_word_range_only_when_enforced`.
  - `test_critique_ok_skips_revise`: 2 calls, final == draft.
  - `test_critique_issues_trigger_revise`: 3 calls; the revise prompt contains the critique text; final is the revised text.
  - `test_deterministic_failure_retries_once_then_returns` (Review Focus 3): revise output contains `[Your Name]`, one extra call (4 total), result returned with `problems` non-empty, no exception.
  - `test_all_steps_use_the_model_that_answered_the_draft`: critique/revise calls carry `model_id` equal to the draft result's `model_id`.
  - `test_cost_is_summed`, `test_hook_appears_in_draft_prompt`, `test_long_dashes_normalized`.
- [ ] **Step 2: Run** `$PY -m pytest tests_unit/test_cover_letter.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `app/cover_letter.py` and rewire `_run_job`; delete the superseded code from `resume_service.py` (`grep -rn "generate_cover_letter\|_openai_call"` must show no stale references).
- [ ] **Step 4: Run** `$PY -m pytest tests_unit -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(resume): draft/critique/revise cover letter pipeline with selectable model`.

### Task 8: Regenerate cover letter endpoint with version history

**Files:**
- Modify: `app/models/resume_application.py` (add `CoverLetterVersion(BaseModel)`: `text`, `provider`, `model`, `createdAt: datetime`; field `coverLetterHistory: List[CoverLetterVersion] = []`), `app/routers/resume.py` (route, `_job_to_dict`, `_reset_failed_job_fields` clears history)
- Test: `tests/test_resume_cover_letter_regen.py` **(integration)**

**Interfaces:**
- Consumes: Tasks 4, 7.
- Produces: `POST /resume/jobs/{job_id}/cover-letter`, body `CoverLetterRegenBody(model: Optional[str] = None, hook: Optional[str] = None)`, owner/admin only (same checks as `/ask`). Requires non-empty `job.plainText` and `job.jobDescription` (else 400). Pushes the current letter (if any) onto the front of `coverLetterHistory` (cap 5), writes the new text, `coverLetterLlm*` fields and cost, `await job.save()`. Response: `{"coverLetterText", "coverLetterLlmProvider", "coverLetterLlmModel", "estimatedCostUsd", "coverLetterHistory"}`.

- [ ] **Step 1: Write failing tests** (patch `app.routers.resume.generate_cover_letter` to return a `CoverLetterResult`): `test_regen_replaces_text_and_archives_previous`, `test_regen_history_capped_at_five`, `test_regen_forbidden_for_other_staff_user` (403), `test_regen_without_plain_text_400`, `test_regen_unknown_model_400`.
- [ ] **Step 2: Run** `$PY -m pytest tests/test_resume_cover_letter_regen.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat(resume): regenerate cover letter with another model, keep prior versions`.

---

## Frontend (`engineers_app-resumes`)

### Task 9: API client, `useModelChoice`, `ModelSelect`

**Files:**
- Modify: `src/api/endpoints.ts` (resume section around lines 1244-1360, `askResumeJobScreening` near line 472)
- Create: `src/lib/useModelChoice.ts`, `src/components/ModelSelect.tsx`

**Interfaces:**
- Produces (`endpoints.ts`): `type LlmProvider = 'free' | 'openai' | 'anthropic'`; `type ModelTask = 'resume' | 'cover_letter' | 'screening'`; `interface ModelOption { id: string; label: string; provider: LlmProvider; tier: 'free'|'budget'|'balanced'|'premium'; bestFor: string; estCostUsd: number | null; isDefault: boolean }`; `listResumeModels(task: ModelTask) => Promise<{ task: ModelTask; defaultId: string | null; models: ModelOption[] }>`; `enqueueResumeJob` body gains `resumeModel?`, `coverLetterModel?`, `screeningModel?`, `coverLetterHook?`; `askResumeJobScreening(jobId, questions, model?)`; `regenerateCoverLetter(jobId, body: { model?: string; hook?: string })` with `timeoutMs: 180_000`; `ResumeJob` provider fields typed `LlmProvider | null` and new optional fields `resumeModelId, coverLetterModelId, screeningModelId, coverLetterLlmProvider, coverLetterLlmModel, coverLetterLlmFallbackUsed, coverLetterLlmFallbackReason, estimatedCostUsd, coverLetterHistory: { text: string; provider: string; model: string; createdAt: string }[]`.
- Produces: `useModelChoice(task: ModelTask): { options: ModelOption[]; value: string; setValue: (id: string) => void; loading: boolean }`. `value` is the stored choice if it is still in `options`, else the server `defaultId`, else `''` (empty means "omit from payload, server decides"). The choice is stored in `localStorage` under `resume-model:${user.id}:${task}` (all access in try/catch). If the models request fails, `options` is `[]` and `value` is `''` (the form must still submit).
- Produces: `<ModelSelect label, options, value, onChange, loading, hint? />` built on the existing `Select` (`src/components/Select.tsx`). Option text: `Label · Tier · ~$0.06/job` (or `cost n/a`), plus "Recommended" on the default; a `text-xs text-faint` line under the control shows the selected model's `bestFor`. Renders nothing when `options` is empty and not loading.

- [ ] **Step 1: Implement** the three files above.
- [ ] **Step 2: Verify** with `npm run lint`. Expected: exits 0 (type-check passes).
- [ ] **Step 3: Commit** `feat(resumes): model catalog client, useModelChoice hook, ModelSelect`.

### Task 10: Selectors on the resume form and the screening panel

**Files:**
- Modify: `src/pages/Resume.tsx` (form around lines 80-160 and 345-387), `src/pages/Generated.tsx` (`ask()` near line 890 and the Ask panel markup)

**Interfaces:** Consumes Task 9.

- [ ] **Step 1: Implement** in `Resume.tsx`: a "Resume model" `ModelSelect` above the Generate button row; when `generateCoverLetter` is checked, reveal a "Cover letter model" `ModelSelect` (premium default via the server `defaultId`) and a single-line "Hook (optional)" input (`maxLength={500}`) with a short helper text. Send `resumeModel`, `coverLetterModel` (only when the box is checked), `coverLetterHook` in `enqueueResumeJob`; send `undefined` when the value is `''`. In `Generated.tsx`, add a "Screening model" `ModelSelect` above the question textarea in the Ask panel and pass it to `askResumeJobScreening`.
- [ ] **Step 2: Verify** `npm run lint` and `npm run build`. Expected: both succeed.
- [ ] **Step 3: Manual check** (`npm run dev` with the backend pointed at a configured key): dropdowns populate; changing a choice persists after reload; with the backend stopped the form still queues a job with no model fields.
- [ ] **Step 4: Commit** `feat(resumes): model selectors for resume, cover letter and screening`.

### Task 11: Show the model used, cost, and cover-letter regeneration

**Files:**
- Modify: `src/pages/Generated.tsx` (`LlmProviderBadge` near line 59, tokens cell near line 575, cover-letter block near lines 992-1025)

**Interfaces:** Consumes Task 9 (`regenerateCoverLetter`, `coverLetterHistory`, new job fields).

- [ ] **Step 1: Implement**: `LlmProviderBadge` accepts `'anthropic'` (labels "Anthropic" / "Anthropic (fallback)"); the tokens cell tooltip appends `· est. $X.XX` when `estimatedCostUsd != null`; the cover-letter block shows a `LlmProviderBadge` for `coverLetterLlm*`, a "Regenerate with" `ModelSelect` (task `cover_letter`) plus a button (disabled with a spinner while the request runs, error via `notify.error`, success calls `onChanged()`), and a collapsed "Previous versions (n)" list showing each version's model label and text with a copy button.
- [ ] **Step 2: Verify** `npm run lint` and `npm run build`. Expected: both succeed.
- [ ] **Step 3: Manual check**: regenerate a letter with a different model: new text appears, the old one is listed under previous versions, badge shows the new model.
- [ ] **Step 4: Commit** `feat(resumes): show model and cost per job, regenerate cover letters`.

---

## Wrap-up

### Task 12: Docs, full verification, quality check

**Files:**
- Modify: `readme.md` (document `ANTHROPIC_API_KEY` and the three `*_DEFAULT_MODEL` settings) in the backend worktree

- [ ] **Step 1: Run** the full backend unit suite `$PY -m pytest tests_unit -q`. Expected: all pass. Run `$PY -m pytest tests -q` only if `MONGODB_URI_TEST` is set; otherwise report integration tests as not run.
- [ ] **Step 2: Run** frontend `npm run lint` and `npm run build`. Expected: both succeed.
- [ ] **Step 3: Quality check (needs the user's `ANTHROPIC_API_KEY`)**: generate one cover letter for the same JD with Sonnet 5.5, Opus 5.5 and Fable 5.1; share the three letters and costs with the user so they can lock the defaults.
- [ ] **Step 4: Merge check**: `git merge-tree` / try a local merge of `feat/job-screening` into `feat/resumes` and report conflicts in `routers/resume.py` and `routers/job_applies.py` to the user.
- [ ] **Step 5: Commit** `docs(resume): document model selection settings`.
