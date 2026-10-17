# Role-based Resume Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Several role-based resume prompts per profile, and a Prompt dropdown per profile row in Job Applies that
defaults to the prompt suggested from the job and is used for tailoring.

**Architecture:** Prompts live in the profile document (`Account.resumePrompts`, lazily migrated from
`resumePromptBody`, which stays in sync with the default). A pure keyword module detects a job's role and stack marks
from its title and extracted skills; Job Applies combines that with each profile's prompts into a suggestion, stores
the user's override per job × profile, and passes the chosen prompt's text into the resume queue. Frontend first
(against the API shapes below, mocked in a headless browser), then backend (TDD).

**Tech Stack:** FastAPI + Beanie (MongoDB), pytest + mongomock-motor; React + TypeScript + Vite + Tailwind, SWR,
Playwright (from the backend venv) for browser checks.

**Spec:** `docs/superpowers/specs/2026-10-17-profile-role-prompts-design.md` (engineers_backend)

**Repos / worktrees** (branch `feat/profile-prompts` in both, on top of `feat/job-apply`):
- Backend `C:\Users\Administrator\Downloads\engineer\engineers_backend-job-apply` — paths below prefixed `be:`
- Frontend `C:\Users\Administrator\Downloads\engineer\engineers_app-job-apply` — paths prefixed `fe:`
- Python: `../engineers_backend/.venv/Scripts/python.exe`; unit tests: `-m pytest tests_unit -q -p no:cacheprovider
  --ignore=tests_unit/test_resume_experience_roles.py --ignore=tests_unit/test_style_serializer.py
  --ignore=tests_unit/test_template_annotate_removals.py` (known failure: `test_bidder_ext::test_upload_url_without_storage_is_503`).
- Node: `export PATH="/c/Program Files/nodejs:$PATH"`; type check `npx tsc -b --noEmit` (known error only in
  `InterviewPrepLibrary.tsx`).

## Global Constraints

- Roles (exact ids → labels): `fullstack` Full stack · `frontend` Frontend · `backend` Backend · `devops` DevOps ·
  `data` Data · `ml` ML / AI · `mobile` Mobile · `qa` QA / Test; role optional ("No role"). No link to Prompt Eval roles.
- ≤ 10 prompts per profile, ≥ 1 when sent; name 1–60 chars, unique per profile case-insensitively; body ≤ 30000.
- Migrated / fallback prompt name: `Default`. Default prompt = `defaultPromptId` if it names a prompt, else the first.
- `resumePromptBody` always equals the default prompt's body after any save.
- Job keyword marks: at most 7.
- Eval panel: placeholder "No eval yet" and a disabled **Run eval** button with "Coming soon"; no Prompt Eval calls.
- Profile layout: text 70% / Eval panel 30% from `lg`; stacked below `lg`.
- Job Applies tailoring model stays `openai:gpt-4o-mini`.
- Commits authored as rice-big0923 (already the global git identity); end messages with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A profile saved from an older screen (Prompt Eval "Save to profile" writes only `resumePromptBody`) → the default
   prompt's body updates, the other prompts stay. Test in Task 4.
2. A job's saved choice names a prompt that was later deleted → the Prompt cell and tailoring fall back to the
   suggestion, no error. Test in Task 6.
3. A job with no extraction (still pending, fetch/read failed) → no role tag, suggestion = default, no crash. Tests in
   Tasks 5 and 6.
4. Keyword false positives: `go` in "good", `ui` in "build", `qa` in "aqua", `react` in "reactive"; punctuation
   keywords `ci/cd`, `next.js`, `node.js` must still match. Test in Task 5.
5. Unsaved prompt edits survive switching tabs, and a name that differs only by case ("DevOps" / "devops") is
   rejected. Browser check in Task 2, server test in Task 4.

---

## Frontend

### Task 1: API types, roles and prompt helpers

**Files:**
- Modify: `fe:src/api/endpoints.ts` (JobApplyProfile ~1686, JobApplyRow ~1840, JobApplyTailored ~1802, after
  `setJobApplyRowInvalid`)
- Create: `fe:src/components/prompts/promptRoles.ts`

**Interfaces — Produces:**
```ts
// endpoints.ts
export type PromptRole = 'fullstack' | 'frontend' | 'backend' | 'devops' | 'data' | 'ml' | 'mobile' | 'qa';
export interface ResumePrompt { id: string; name: string; role: PromptRole | null; body: string; updatedAt?: string }
export interface JobApplyPromptOption { id: string; name: string; role: PromptRole | null }
// JobApplyProfile += prompts?: JobApplyPromptOption[]; defaultPromptId?: string | null
// JobApplyRow     += jobRole?: { role: PromptRole | null; marks: string[]; reason: string };
//                    prompt?: Record<string, { suggestedId: string | null; chosenId: string | null }>
// JobApplyTailored += promptName?: string | null
export const setJobApplyRowPrompt = (rowId: string, body: { accountId: string; promptId: string }) =>
  apiFetch<JobApplyRow>(`/job-applies/rows/${rowId}/prompt`, { method: 'PUT', body: JSON.stringify(body) });

// promptRoles.ts
export const PROMPT_ROLES: PromptRole[];                       // order as in Global Constraints
export const PROMPT_ROLE_LABEL: Record<PromptRole, string>;
export const MAX_PROMPTS = 10, MAX_PROMPT_NAME = 60;
export function newPromptId(): string;                          // 'new-' + random; server replaces it
export function promptErrors(prompts: ResumePrompt[]): Record<string, string>; // id → message ('' = ok)
```
`promptErrors` messages: empty name → "Give the prompt a name", > 60 → "Keep the name under 60 characters", duplicate
(case-insensitive) → "Another prompt already has this name".

- [ ] **Step 1:** Add the types / API function / helpers above.
- [ ] **Step 2:** `npx tsc -b --noEmit` → only the known `InterviewPrepLibrary.tsx` error.
- [ ] **Step 3:** Commit `feat(prompts): types, roles and validation helpers for role-based resume prompts`.

### Task 2: Profile page — prompt tabs, editor, 70/30 Eval panel

**Files:**
- Create: `fe:src/components/prompts/ResumePromptsEditor.tsx`
- Modify: `fe:src/pages/AccountEdit.tsx` (`PromptsBlock` 262-409: load 271-287, save 289-303, summary 315-324,
  resume section 353-364; `AccShape` ~20)

**Interfaces:**
- Consumes: Task 1 types and helpers; `ResumePromptField` (`fe:src/components/ResumePromptField.tsx`, props `value,
  onChange, label, hint, kind, defaultSource`).
- Produces:
```tsx
export default function ResumePromptsEditor(props: {
  prompts: ResumePrompt[]; defaultId: string;
  onChange: (prompts: ResumePrompt[], defaultId: string) => void;
  dirtyIds: Set<string>;            // tabs showing •
  errors: Record<string, string>;   // from promptErrors
}): JSX.Element
```
Behaviour (spec §1a): tabs (★ default, • unsaved, error tint), **+ Add prompt** hidden at 10 (adds `{id:
newPromptId(), name: 'New prompt', role: null, body: ''}`, selects it, focuses name), name input, role select
(`PROMPT_ROLE_LABEL` + "No role"), **Set as default** (hidden on default), **Delete** (disabled on default with title
"Make another prompt the default first"; `window.confirm` "Delete the “{name}” prompt?"), text area 70% /
Eval panel 30% (`lg:grid-cols-[7fr_3fr]`), Eval panel: "No eval yet" + disabled **Run eval** + "Coming soon".

`PromptsBlock`: load `acc.resumePrompts` / `acc.defaultPromptId` (if the account has none yet, build `[{id:
'default', name: 'Default', role: null, body: acc.resumePromptBody ?? ''}]`); save sends `{resumePrompts,
defaultPromptId, screeningPrompt, coverLetterPrompt}` (no `resumePromptBody`), replaces state with the returned list,
blocks save while `promptErrors` has any message; collapsed summary "N resume prompt(s) · default: <name>".

- [ ] **Step 1:** Build the editor and wire `PromptsBlock`.
- [ ] **Step 2:** Type check (as Task 1).
- [ ] **Step 3: Browser check** — scratchpad Playwright script, real `/accounts/acc1` page, mocked `GET/PUT
  /accounts/acc1` (+ `/auth/me`, model lists → `{models: []}`, everything else `{}`), seeded login in
  `localStorage` (`engineersapp_token`, `engineersapp_user`). Assert: legacy account shows one "Default ★" tab with its
  text; add "DevOps" (role DevOps), type text, switch tabs and back → text kept, • shown; rename to "default" →
  error, Save blocked; fix, set DevOps default, Save → PUT body has 2 prompts, `defaultPromptId` = DevOps id; Delete
  disabled on the default; at 1600px the text area is ~70% and the Eval panel shows "No eval yet" with a disabled
  button; at 900px they stack. Screenshot both widths.
- [ ] **Step 4:** Remove any preview files; commit `feat(prompts): several role-based resume prompts on the profile page`.

### Task 3: Job Applies — Job-cell marks and Prompt column

**Files:**
- Create: `fe:src/components/jobApplies/PromptCell.tsx`, `fe:src/components/jobApplies/JobRoleMarks.tsx`
- Modify: `fe:src/components/jobApplies/Suggestions.tsx` (`ProfileCells`: new cell between HTML template and
  Tailored; Tailored chip tooltip), `fe:src/pages/JobApplyRun.tsx` (header: add `Prompt` th between `HTML template` and
  `Tailored`; expanded-row `colSpan` 11 → 12; no-profile cell `colSpan` 4 → 5; Job cell under the company line;
  `ProfileCells` props; tailor dialog copy)

**Interfaces:**
- Consumes: Task 1 types, `setJobApplyRowPrompt`.
- Produces:
```tsx
export function JobRoleMarks({ jobRole }: { jobRole?: JobApplyRow['jobRole'] }): JSX.Element | null
// role tag (PROMPT_ROLE_LABEL) + ≤ 7 chips, title={jobRole.reason}; nothing when no role and no marks
export function PromptCell(props: {
  options: JobApplyPromptOption[]; defaultId: string | null | undefined;
  suggestedId: string | null; chosenId: string | null;
  onChange: (promptId: string) => Promise<void>;
}): JSX.Element
// value = chosenId (if still in options) ?? suggestedId ?? defaultId; 1 option → plain name; 0 options → "—";
// tag "Suggested" when value === suggestedId and suggestedId !== defaultId, "Default" when value === defaultId;
// spinner while onChange runs; on error the select shows the previous value
```
`JobApplyRun`: `changePrompt(row, acc, promptId)` calls `api.setJobApplyRowPrompt`, merges the returned row into the
SWR rows cache (`mutateRows`), toasts on error. `ProfileCells` gets `promptOptions`, `defaultPromptId`,
`promptState` (`row.prompt?.[acc]`), `onPromptChange`. Tailored chip `title` appends `\nTailored with ${t.promptName}`
when set. Tailor dialog adds "Each job uses the prompt selected in its Prompt column."

- [ ] **Step 1:** Build the two components and wire them.
- [ ] **Step 2:** Type check.
- [ ] **Step 3: Browser check** — extend the scratchpad `run_page_shot.py` mocks: profiles with `prompts` (Blake: Default
  + DevOps; Sam: Default only), rows with `jobRole` (`{role: 'devops', marks: ['Kubernetes','Terraform','AWS','Helm',
  'CI/CD','Go','Python'], reason: 'DevOps job — title says DevOps; skills Kubernetes, Terraform'}`) and `prompt`
  (`{blake: {suggestedId: 'p-devops', chosenId: null}}`), mocked `PUT /rows/r2/prompt` echoing the row with
  `chosenId`. Assert: Job cell shows "DevOps" + 7 chips; header order ends `HTML TEMPLATE, PROMPT, TAILORED`; Blake's
  select shows DevOps + "Suggested"; Sam shows plain "Default"; changing Blake to Default sends `{accountId:'blake',
  promptId:'default'}` and the tag becomes "Default"; rows still line up (header vs cells). Screenshot at 1920px.
- [ ] **Step 4:** Commit `feat(job-applies): Prompt column with suggested prompt and job keyword marks`.

---

## Backend

### Task 4: Prompt storage on the profile

**Files:**
- Modify: `be:app/models/account.py` (add `PromptRole`, `ResumePrompt`, `Account.resumePrompts`,
  `Account.defaultPromptId`)
- Create: `be:app/resume_prompts.py`
- Modify: `be:app/routers/accounts.py` (`AccountCreate`/`AccountUpdate` 88-115, `update_account` 308-352, account
  output — wherever `doc_to_dict(acc)` is returned for GET/PUT)
- Test: `be:tests_unit/test_resume_prompts.py`

**Interfaces — Produces:**
```python
PromptRole = Literal["fullstack", "frontend", "backend", "devops", "data", "ml", "mobile", "qa"]
class ResumePrompt(BaseModel): id: str; name: str; role: Optional[PromptRole] = None; body: str = ""; updatedAt: datetime
# app/resume_prompts.py
MAX_PROMPTS = 10; MAX_NAME = 60; MAX_BODY = 30000; DEFAULT_NAME = "Default"; LEGACY_ID = "default"
def prompts_view(account) -> tuple[list[ResumePrompt], str]          # migrated view; (prompts, default id)
def apply_prompts(account, prompts: list[dict], default_id: Optional[str]) -> None   # raises PromptError(msg)
def sync_default_body(account, body: Optional[str]) -> None          # resumePromptBody-only writes
def find_prompt(account, prompt_id: Optional[str]) -> Optional[ResumePrompt]
class PromptError(ValueError)
```
The migrated prompt's id is `LEGACY_ID` (stable, so a job choice made before the profile's first save stays valid).
`apply_prompts`: assigns ids to missing/`new-…` ids (ObjectId hex; other ids, including `default`, are kept), keeps `updatedAt` for unchanged bodies/names,
sets `defaultPromptId`, sets `account.resumePromptBody` = default body or None when empty. Router maps `PromptError` →
400. `update_account`: if `resumePrompts` sent → `apply_prompts`; elif `resumePromptBody` sent → `sync_default_body`.
GET/PUT output adds `resumePrompts` (as dicts) and `defaultPromptId` from `prompts_view`.

- [ ] **Step 1: Failing tests** in `test_resume_prompts.py` (pure on `Account(...)` objects; router via mongomock like
  `test_job_apply_tailor_retry.py`):
  - `test_legacy_profile_reads_as_one_default_prompt` — `Account(resumePromptBody="X")` → `[("default", "Default",
    None, "X")]` as (id, name, role, body), default id `"default"`.
  - `test_empty_profile_has_an_empty_default`.
  - `test_apply_prompts_assigns_ids_and_syncs_default_body` — `[{id:"default",…}, {id:"new-1",…}]`, default the
    second → first keeps `"default"`, second gets a 24-hex id, `resumePromptBody` == second body.
  - `test_apply_prompts_rejects_bad_lists` — parametrized: 11 prompts; empty list; name "" ; name 61 chars; names
    "DevOps"/"devops"; body 30001 chars; unknown `default_id` → each raises `PromptError`.
  - `test_body_only_write_updates_just_the_default` — profile with Default + DevOps; `sync_default_body(acc, "new")` →
    Default body "new", DevOps unchanged, `resumePromptBody` "new". *(Review Focus 1)*
  - `test_update_account_route_saves_prompts` — PUT body with `resumePrompts` + `defaultPromptId` → 200, response has
    both lists with real ids; a duplicate name → 400 "Another prompt already has this name".
- [ ] **Step 2:** Run → fail (import errors / missing attributes).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the file → pass; then the full unit suite → only the known failure.
- [ ] **Step 5:** Commit `feat(prompts): several role-based resume prompts per profile (storage, validation, migration)`.

### Task 5: Job role and keyword marks

**Files:**
- Create: `be:app/job_apply/prompt_suggest.py`
- Test: `be:tests_unit/test_prompt_suggest.py`

**Interfaces — Produces:**
```python
@dataclass(frozen=True)
class RoleMatch:
    role: Optional[str]; marks: list[str]; reason: str
ROLE_KEYWORDS: dict[str, tuple[str, ...]]           # values from the spec §4, verbatim
MAX_MARKS = 7
def job_role(title: str, terms: Sequence[Term]) -> RoleMatch
def suggest_prompt(prompts: Sequence[ResumePrompt], default_id: str, role: Optional[str]) -> str
```
Matching: lowercase; keyword matches when not preceded/followed by `[a-z0-9]` (so `ci/cd`, `next.js` work and `go`
≠ "good"). Score = 3 × title hits + Σ skill hits (required/core 2, others 1), each keyword once per role; role = top
score if ≥ 2 or any title hit; tie → role with a title hit, else None. Marks: that role's matched skill keywords in
the job's spelling (from `Term.term`), required/core first, then other required/core terms, de-duplicated, ≤ 7;
keywords matched only in the title are excluded. Reason: `"{Label} job — title says {word}; skills {a}, {b}"` (parts
omitted when empty); no role → `""`.

- [ ] **Step 1: Failing tests:**
  - `test_title_decides_the_role` — "Senior DevOps Engineer", terms [] → role "devops".
  - `test_skills_decide_when_the_title_is_generic` — "Software Engineer", terms Kubernetes(required),
    Terraform(core), Python(mentioned) → "devops", marks start ["Kubernetes", "Terraform"].
  - `test_marks_are_capped_at_seven_and_skip_title_words`.
  - `test_no_role_without_enough_signal` — "Engineer", one mentioned term "Jira" → role None, reason "".
  - `test_whole_word_matching` — titles/terms "Good communicator", "Build tools", "Aqua", "Reactive systems" → no
    go/ui/qa/react hits; "CI/CD pipelines", "Next.js" → matched. *(Review Focus 4)*
  - `test_tie_goes_to_the_role_in_the_title`.
  - `test_suggest_prompt` — role prompt chosen; two of that role → default if among them else latest `updatedAt`;
    no match / role None → default id.
  - `test_job_without_extraction` — `job_role("", [])` → `RoleMatch(None, [], "")`. *(Review Focus 3)*
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Commit `feat(job-applies): detect a job's role and stack keywords for prompt suggestions`.

### Task 6: Job Applies — prompts on runs, suggestions on rows, saved choice

**Files:**
- Modify: `be:app/models/job_apply.py` (`JobApplyRow.promptChoice: Dict[str, str] = {}`),
  `be:app/routers/job_applies.py` (`_run_out` profiles ~104-114; `_RowListView` projection ~170 add
  `"extraction.title": 1, "extraction.terms": 1, "promptChoice": 1` and fields; `_row_summary`; list-rows endpoint
  ~880-925 builds the prompt context; new endpoint after `mark_invalid_row`)
- Test: `be:tests_unit/test_job_apply_prompts.py`

**Interfaces:**
- Consumes: Task 4 `prompts_view`, `find_prompt`; Task 5 `job_role`, `suggest_prompt`.
- Produces:
```python
PromptCtx = dict[str, tuple[list[ResumePrompt], str]]      # accountId → (prompts, default id)
async def _prompt_ctx(run: JobApplyRun) -> PromptCtx       # accounts in run.selection
def _row_summary(row, threshold, account_id="", previous=None, tailored_docs=None, prompt_ctx: Optional[PromptCtx] = None) -> dict
#   adds "jobRole": {"role", "marks", "reason"} always, and "prompt": {acc: {"suggestedId", "chosenId"}} when prompt_ctx
def _effective_prompt(row, prompts: list[ResumePrompt], default_id: str) -> ResumePrompt   # choice → suggestion → default
class PromptChoiceBody(BaseModel): accountId: str; promptId: str
@router.put("/rows/{row_id}/prompt") async def choose_prompt(row_id, body, user) -> dict   # row summary with prompt
```
`chosenId` is reported only if it still names a prompt (else null). Choosing the suggested id removes the entry.
Unknown `accountId` (not in the run) or `promptId` (not on that profile) → 404. `_run_out` profiles add `prompts:
[{id, name, role}]` and `defaultPromptId`.

- [ ] **Step 1: Failing tests** (mongomock; reuse the `_setup` style of `test_job_apply_min_score.py`, profiles with
  prompts Default + DevOps(role devops)):
  - `test_run_profiles_list_their_prompts`.
  - `test_row_summary_has_job_role_and_suggestion` — DevOps-titled job → `jobRole.role == "devops"`,
    `prompt[blake].suggestedId == devops id`, `chosenId is None`; a profile with only Default → suggestion Default.
  - `test_choose_prompt_saves_and_clearing_by_choosing_the_suggestion`.
  - `test_choice_of_a_deleted_prompt_falls_back` — Blake has Default (default), DevOps, Backend(role backend); on a
    DevOps job choose Backend, then save Blake's prompts without Backend → summary `chosenId is None`,
    `_effective_prompt` returns DevOps (the suggestion). *(Review Focus 2)*
  - `test_job_without_extraction_suggests_the_default` — row with `extraction=None`. *(Review Focus 3)*
  - `test_choose_prompt_rejects_unknown_profile_or_prompt` → 404.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run this file and all `tests_unit/test_job_apply_*.py` → pass.
- [ ] **Step 5:** Commit `feat(job-applies): prompt per profile row, suggested from the job, saved per job`.

### Task 7: Tailor with the selected prompt

**Files:**
- Modify: `be:app/models/resume_application.py` (`promptId: Optional[str] = None`, `promptName: Optional[str] =
  None`), `be:app/resume_queue.py` (`enqueue_generation` 92-143 new params; `requeue_failed` 146-185 refreshes
  them), `be:app/routers/job_applies.py` (`_tailor_row` 1374-1426, `_tailored_out` 212-225 adds `promptName`, and
  `_TAILORED_PROJECTION` must not exclude it)
- Test: `be:tests_unit/test_job_apply_prompts.py` (same file as Task 6)

**Interfaces:**
- Consumes: Task 6 `_effective_prompt`, Task 4 `prompts_view`.
- Produces: `enqueue_generation(..., prompt_body: Optional[str] = None, prompt_id: Optional[str] = None,
  prompt_name: Optional[str] = None)`; `requeue_failed(job_id, ..., prompt_body=None, prompt_id=None,
  prompt_name=None)` (match its existing parameter style); `_tailored_out(doc)["promptName"]`.

`_tailor_row` loads the profile, takes `_effective_prompt(row, *prompts_view(account))`, passes `prompt_body=body or
None`, `prompt_id`, `prompt_name` to both enqueue and requeue.

- [ ] **Step 1: Failing tests:**
  - `test_tailor_uses_the_suggested_prompt` — DevOps job, no choice → `ResumeApplication.promptOverride ==` DevOps
    body, `promptName == "DevOps"`.
  - `test_tailor_uses_the_chosen_prompt`.
  - `test_retry_picks_up_a_new_choice` — failed job, change choice, `_tailor_row` again → same job id, new
    `promptOverride` / `promptName`.
  - `test_tailor_all_uses_each_rows_prompt` — two rows (DevOps job, Frontend-ish job with no frontend prompt) →
    DevOps body / Default body.
  - `test_empty_prompt_body_falls_back` — default with empty body → `promptOverride is None`.
  - `test_tailored_out_has_prompt_name`.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the file, then the full unit suite → only the known failure.
- [ ] **Step 5:** Commit `feat(job-applies): tailor with the prompt selected per job and profile`.

---

### Task 8: End-to-end check and hand-off

- [ ] **Step 1:** Restart the local backend (`FRONTEND_ORIGIN=http://localhost:5173 ... uvicorn app.main:app --port
  8080`, background) and confirm `GET /openapi.json` lists `/job-applies/rows/{row_id}/prompt`.
- [ ] **Step 2:** Re-run the Task 2 and Task 3 browser checks, with the mocked row / profile JSON replaced by output
  captured from the real `_row_summary` / `_run_out` in a small scratch script (mongomock data from Task 6), so the
  frontend is checked against real shapes.
- [ ] **Step 3:** Full backend unit suite; `npx tsc -b --noEmit`.
- [ ] **Step 4:** Report to the user with screenshots; ask before pushing / opening PRs (branch `feat/profile-prompts`,
  stacked on `feat/job-apply`).
