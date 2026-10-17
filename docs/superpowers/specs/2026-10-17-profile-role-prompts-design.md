# Role-based resume prompts per profile — design

Date: 2026-10-17 · Repos: engineers_backend, engineers_app · Branch: `feat/profile-prompts` (on top of `feat/job-apply`)

## Goal

A profile can hold several resume prompts, one per role (DevOps, FullStack, …), instead of one. In Job Applies, each
profile row gets a **Prompt** dropdown next to HTML template, pre-set to the prompt that best fits the job; the user can
change it, and tailoring uses it.

## Non-goals (this round)

- **Eval on save.** Saving a prompt only saves. The prompt section shows **Run eval / Re-evaluate** buttons, disabled
  ("Coming soon"). Wiring them to Prompt Eval, and the save-time confirmation, come later.
- **DevOps sample JDs / DevOps Eval role.** The prompt role list includes DevOps; the Eval role list is unchanged.
- **AI- or Eval-score-based suggestion.** The suggestion is keyword based (below); it can be replaced later behind the
  same interface.
- Screening and cover-letter prompts stay single.

## 1. Storage (backend)

`Account` gains:

```python
class ResumePrompt(BaseModel):
    id: str                      # stable id (ObjectId hex), assigned by the server when missing
    name: str                    # 1–60 chars, unique per profile (case-insensitive)
    role: Optional[PromptRole]   # None = no role (only the migrated "Default" starts like this)
    body: str                    # ≤ 30000 chars, may be empty
    updatedAt: datetime

resumePrompts: List[ResumePrompt] = []   # ≤ 10
defaultPromptId: Optional[str] = None
```

`PromptRole` = the Eval roles (`fullstack, frontend, backend, ai, ml, llm, data, test, senior_swe, staff_swe`) **plus
`devops`**. It is its own Literal (in `app/models/account.py`), so adding DevOps here does not touch Prompt Eval.

**Migration (lazy, no script).** A profile with no `resumePrompts` is read as one prompt `{name: "Default", role: None,
body: resumePromptBody or ""}` that is the default. The list is written on the profile's next prompt save.

**Compatibility.** `resumePromptBody` is kept equal to the default prompt's body on every save. Everything that reads
it today keeps working unchanged: `_run_job`'s fallback chain (`resume.py`), Prompt Eval's `resolve_prompt`, the
`hasPrompt` lookup badge, `NewEvalPanel` prefill / "Save to profile" (which writes `resumePromptBody`; the server then
also updates the default prompt's body so the two never drift).

**Validation** (server, 400 with a clear message): ≤ 10 prompts; names 1–60 chars and unique; body ≤ 30000;
`defaultPromptId` must name a prompt in the list; at least one prompt when the list is sent; ids unique.

## 2. API

- `GET /accounts/{id}` returns `resumePrompts` (migrated view when empty) and `defaultPromptId`.
- `PUT /accounts/{id}` accepts `resumePrompts` + `defaultPromptId` (the whole list, like the current "Save prompts"
  that saves all fields at once). New prompts may omit `id`; the server assigns one and returns the saved list.
- Job Applies `_run_out(..., with_profiles=True)` adds to each profile: `prompts: [{id, name, role}]` (no bodies) and
  `defaultPromptId`.

## 3. Profile page — "Resume generating prompt" (engineers_app `AccountEdit.tsx` › `PromptsBlock`)

```
[ Default ★ ] [ DevOps ] [ FullStack ] [ + Add prompt ]
Name [DevOps          ]  Role [DevOps ▾]   ☆ Set as default   🗑 Delete
┌──────────────────────── 70% ──────────────────────┐┌──── 30% ─────┐
│ prompt text (19120/30000 · Load global)           ││ radar, or    │
│                                                   ││ "No eval yet"│
└───────────────────────────────────────────────────┘│ [Re-evaluate]│ (disabled)
                                       [Save prompts] └──────────────┘
```

- One tab per prompt; ★ marks the default. **+ Add prompt** adds "New prompt" (role unset) and selects it.
- Editor: name, role select, "Set as default", Delete (disabled on the default: make another the default first).
- Text area is the existing `ResumePromptField` (counter, Load global) at 70% width; ≥ lg screens side by side, stacked
  below that.
- Right 30%: the radar (`RoleRadar`, match metric) of the latest **done** Prompt Eval run for this profile whose
  `promptBody` equals this prompt's saved text; otherwise "No eval yet". **Run eval / Re-evaluate** shown disabled with
  "Coming soon".
- **Save prompts** saves all resume prompts plus the screening / cover-letter prompts, as today. Unsaved changes are
  flagged on the tab (•).

## 4. Prompt suggestion (backend, `app/job_apply/prompt_suggest.py`)

Pure functions, no I/O:

```python
def job_role(title: str, terms: list[Term]) -> tuple[Optional[PromptRole], list[str]]
def suggest_prompt(prompts, default_id, role) -> Optional[str]
```

- A keyword table per role, e.g. devops: `devops, sre, site reliability, platform engineer, kubernetes, terraform,
  ansible, ci/cd, infrastructure`; frontend: `frontend, front-end, react, vue, angular, ui engineer`; fullstack:
  `full stack, fullstack, full-stack`; backend: `backend, back-end, api, microservices`; data: `data engineer, etl,
  spark, airflow, dbt`; ml: `machine learning, ml engineer, pytorch, tensorflow`; llm: `llm, rag, langchain, prompt
  engineering`; ai: `ai engineer, artificial intelligence`; test: `qa, sdet, test automation, quality`; senior_swe /
  staff_swe: `senior` / `staff`, `principal` in the title only, used when no stack role scores.
- Score per role: title hits × 3 + extracted-skill hits (`extraction.terms`, required/core × 2, other tiers × 1),
  each keyword counted once; whole-word, case-insensitive match. The job's role is the top score if ≥ 2 (a title hit
  alone qualifies); ties → the role whose keyword appears in the title, else no role.
- Marks: up to 3 matched **skill** keywords (title-matched role words are left out — the title is already shown),
  required/core first, displayed with their extracted spelling (`Kubernetes`, `Terraform`).
- `suggest_prompt`: the profile's prompts whose role equals the job's role; if several, the default when it is among
  them, else the most recently updated; if none (or no job role), the default prompt.
- Computed per request in `_row_summary` from `row.title` / `row.extraction` (the list projection gains
  `extraction.title` and `extraction.terms.term/tier`). Nothing stored; jobs read before this ship get suggestions too.

## 5. Job Applies — Prompt column (engineers_app `Suggestions.tsx` › `ProfileCells`)

Column order: … Profile · Sample resumes (.pdf) · HTML template · **Prompt** · Tailored.

- Per profile row: a compact `<select>` of the profile's prompts (name, role in grey). One prompt → plain text, no
  select. Under it, the marks: `Kubernetes · Terraform`, and "Suggested" when the value is the suggestion; hover
  explains ("DevOps job: title mentions DevOps; skills Kubernetes, Terraform").
- Value = the user's choice for this job × profile if any, else the suggestion.
- **Choice storage:** `JobApplyRow.promptChoice: Dict[accountId, promptId]`; `PUT /job-applies/rows/{id}/prompt
  {accountId, promptId}` saves it (choosing the suggestion clears the entry). A choice naming a deleted prompt is
  ignored (falls back to the suggestion).
- Row summary adds `prompt: {accountId: {suggestedId, chosenId, role, marks}}`.

## 6. Tailoring with the chosen prompt

- `_tailor_row` resolves the effective prompt (choice → suggestion → default) and passes its body to
  `enqueue_generation(prompt_body=…)`, so it is stored as `ResumeApplication.promptOverride` (already wins in
  `_run_job`, survives restarts). An empty body → `None` (falls back to profile/global as today).
- `ResumeApplication` gains `promptId`, `promptName` (for display). `_tailored_out` returns `promptName`; the Tailored
  chip tooltip says "Tailored with DevOps".
- `requeue_failed` also refreshes `promptOverride` / `promptId` / `promptName` so a retry uses the current choice.
- Applies to Tailor, Tailor all, Tailor resumes (N) (`tailor-all`), Retry and re-tailor.

## 7. Testing

Backend (written first, mongomock like the other Job Applies tests):
- prompts: save / validate (limits, unique names, default must exist) / lazy migration / `resumePromptBody` sync both
  ways / ids assigned.
- suggestion: role from title, from skills, tie-breaks, no role → default, marks (order, max 3, no title words).
- Job Applies: `prompt` in the row summary, choice save / clear / deleted-prompt fallback, `run.profiles[].prompts`.
- tailoring: chosen prompt body lands in `promptOverride` + `promptId/Name`; retry picks up a new choice;
  `tailor-all` uses each row's effective prompt.

Frontend: `tsc -b --noEmit`; headless-browser checks with a mocked backend for the profile prompt tabs (add, rename,
role, default, delete, 70/30 layout) and the Prompt column (suggested value, marks, change → saved, tailor sends it).

## Rollout

Backend first (new fields are optional; old frontends ignore them). The frontend reads `prompts` / `prompt` only when
present.
