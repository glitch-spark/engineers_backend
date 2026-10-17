# Role-based resume prompts per profile — design

Date: 2026-10-17 · Repos: engineers_app (frontend), engineers_backend · Branch: `feat/profile-prompts` (on top of
`feat/job-apply`)

## Goal

A profile can hold several resume prompts, one per job role (DevOps, Full stack, …), instead of one. In Job Applies,
each profile row gets a **Prompt** dropdown next to HTML template, pre-set to the prompt that best fits the job; the
user can change it, and tailoring uses it.

## Non-goals (this round)

- **Prompt Eval.** Nothing here uses or changes Prompt Eval. The prompt section reserves the right 30% for a future
  diagram and shows **Run eval / Re-evaluate** disabled ("Coming soon").
- AI-based suggestion (the suggestion is keyword based and can be swapped later).
- Screening and cover-letter prompts stay single.

## Roles

A fixed list of job roles, used only to label prompts and match jobs:
`fullstack` Full stack · `frontend` Frontend · `backend` Backend · `devops` DevOps · `data` Data · `ml` ML / AI ·
`mobile` Mobile · `qa` QA / Test. A prompt's role is optional ("No role").

## Defaults

- A profile's **default prompt** = its current prompt (today's single "Profile resume content prompt", shown as
  **"Default"**); a profile without one gets an empty "Default". New profiles start the same way.
- Every job (including jobs already read before this ships) starts with the **suggested** prompt; with no role match the
  suggestion is the profile's default.

---

## 1. Frontend design

### 1a. Profile page — "Resume generating prompt" (`AccountEdit.tsx` › `PromptsBlock`)

```
Resume generating prompt
Drives which fields the resume LLM rewrites (and how). One prompt per role; Job Applies suggests the right one per job.

 ┌───────────┬──────────┬─────────────┬────────────────┐
 │ Default ★ │ DevOps • │ Full stack  │ + Add prompt   │        (• = unsaved changes)
 └───────────┴──────────┴─────────────┴────────────────┘
 Name [ DevOps                ]   Role [ DevOps        ▾]   [☆ Set as default]   [🗑 Delete]

 ┌──────────────────────────── 70% ────────────────────────────┐ ┌────────── 30% ──────────┐
 │ Prompt                              19120/30000  Load global │ │ Eval                    │
 │ ┌──────────────────────────────────────────────────────────┐ │ │                         │
 │ │ # Resume Tailoring — Master Prompt …                     │ │ │   ◇  No eval yet        │
 │ │                                                          │ │ │                         │
 │ │                                                          │ │ │ [▶ Run eval] (disabled) │
 │ └──────────────────────────────────────────────────────────┘ │ │ Coming soon             │
 │ Empty = inherit global.                                      │ │                         │
 └──────────────────────────────────────────────────────────────┘ └─────────────────────────┘
```

- **Tabs**: one per prompt, in saved order; ★ = default; • = unsaved changes. Clicking a tab switches the editor.
  **+ Add prompt** (hidden at 10 prompts) adds "New prompt" with no role and selects it, name field focused.
- **Name** (1–60 chars, unique per profile) · **Role** select (the roles above + "No role") · **Set as default** (hidden
  on the default) · **Delete** (disabled on the default with the hint "Make another prompt the default first"; asks
  for confirmation).
- **Text area** = the existing `ResumePromptField` (counter, Load global), **70%** width; the **Eval** panel **30%**
  with the "No eval yet" placeholder and a disabled **Run eval** button ("Coming soon"). Side by side from `lg`;
  stacked (Eval panel below) on narrower screens.
- **Save prompts** (bottom, existing button) saves all resume prompts with the screening / cover-letter prompts.
  Validation errors (duplicate name, empty name, too long) show under the field and on the tab; nothing is saved
  until they're fixed.
- Summary line in the collapsed header: "3 resume prompts · default: Default" (replaces "Customized: Resume").

### 1b. Job Applies — Prompt column (`Suggestions.tsx` › `ProfileCells`, `JobApplyRun.tsx`)

Column order: … Profile · Sample resumes (.pdf) · HTML template · **Prompt** · Tailored.

```
│ Job                                          │ … │ HTML template       │ Prompt                    │ Tailored       │
│ Senior DevOps Engineer                       │   │ ⧉ Blake modern.html │ [ DevOps        ▾]        │ [✨ Tailor]     │
│ Acme  open ↗  📄                             │   │                     │ Suggested                 │                │
│ [DevOps] Kubernetes · Terraform · AWS · Helm │   │ ⧉ Two-column.html   │ Default                   │ ✨ Tailored 88  │
│          CI/CD · Go · Python                 │   │                     │                           │                │
```

- **Job cell marks** (once per job, under the company line): the job's detected **role** tag (e.g. `DevOps`) and up
  to **7** small chips of the main-stack skills that matched (required/core first), wrapping to a second line if
  needed. Hover: "DevOps job — title says DevOps; skills Kubernetes, Terraform, …". No role match → no role tag; the
  top required/core skills still show as chips.
- **Prompt cell**: a select of the profile's prompts (name; role in grey after it), value = the user's choice for this
  job × profile, else the suggestion. Changing it saves immediately (spinner in the cell; toast on error, value
  restored). Under it a **"Suggested"** tag when the value is the suggestion (hover: "Matches the DevOps job"), or
  "Default" when no prompt matches the job's role. Choosing the suggested prompt again clears the user's choice.
- **One prompt** on the profile → its name as plain text (no select), no tag.
- The **Tailored** chip's tooltip adds "Tailored with <prompt name>".
- The bulk **Tailor resumes** dialog gets one line: "Each job uses the prompt selected in its Prompt column."

### 1c. Build order

Frontend first, against the API shapes below, checked in a headless browser with a mocked backend; then the backend
(tests first); then the two together.

---

## 2. Storage (backend)

`Account` gains:

```python
class ResumePrompt(BaseModel):
    id: str                       # ObjectId hex, assigned by the server when missing
    name: str                     # 1–60 chars, unique per profile (case-insensitive)
    role: Optional[PromptRole]    # None = no role
    body: str = ""                # ≤ 30000 chars
    updatedAt: datetime

resumePrompts: List[ResumePrompt] = []   # ≤ 10
defaultPromptId: Optional[str] = None
```

- **Lazy migration**: a profile with no `resumePrompts` is read as one prompt `{name: "Default", role: None, body:
  resumePromptBody or ""}` that is the default; the list is written on the next prompt save. Default when
  `defaultPromptId` is missing/stale = the first prompt.
- **Compatibility**: `resumePromptBody` is kept equal to the default prompt's body on every save, so everything that
  reads it today (generation's fallback chain, the profile lookup badge, other pages) works unchanged. A write to
  `resumePromptBody` alone (older screens) also updates the default prompt's body.
- **Validation** (400 with a message): ≤ 10 prompts, ≥ 1 when sent; names 1–60 and unique; body ≤ 30000; ids unique;
  `defaultPromptId` must be in the list.

## 3. API

- `GET /accounts/{id}` → adds `resumePrompts` (migrated view when empty) and `defaultPromptId`.
- `PUT /accounts/{id}` ← accepts `resumePrompts` + `defaultPromptId` (whole list); returns the saved list with ids.
- Job Applies run (`_run_out(with_profiles=True)`) → each profile adds `prompts: [{id, name, role}]` and
  `defaultPromptId`.
- Job Applies row summary → `jobRole: {role, marks: [str]}` (once per job) and `prompt: {accountId: {suggestedId,
  chosenId}}`.
- `PUT /job-applies/rows/{id}/prompt` ← `{accountId, promptId}`; choosing the suggestion clears the choice. Returns the
  row summary.

## 4. Suggestion (backend, `app/job_apply/prompt_suggest.py`, pure functions)

- Keyword table per role (whole-word, case-insensitive), e.g. devops: `devops, sre, site reliability, platform
  engineer, kubernetes, k8s, terraform, ansible, helm, ci/cd, jenkins, infrastructure as code`; frontend: `frontend,
  front-end, react, vue, angular, next.js, css, ui engineer`; fullstack: `full stack, fullstack, full-stack, mern`;
  backend: `backend, back-end, microservices, rest api, django, spring, node.js`; data: `data engineer, etl, spark,
  airflow, dbt, snowflake`; ml: `machine learning, ml engineer, ai engineer, llm, pytorch, tensorflow, rag`; mobile:
  `ios, android, mobile, swift, kotlin, react native, flutter`; qa: `qa, sdet, test automation, quality assurance,
  selenium, cypress`.
- Score per role = title hits × 3 + skill hits from `extraction.terms` (required/core × 2, other tiers × 1), each
  keyword once. The job's role is the top score if ≥ 2 (a title hit alone qualifies); a tie goes to the role named in
  the title, else no role.
- **Marks** (shown in the Job cell): up to 7 main-stack skills — the matched keywords for that role first, then other
  required/core skills — in the job's own spelling; role words that only appear in the title are left out. With no
  role, the top required/core skills.
- **Pick**: the profile's prompts with that role → the default if among them, else the most recently updated; no role
  or no match → the default.
- Computed per request from `row.title` and `row.extraction` (list projection adds `extraction.title`,
  `extraction.terms.term/tier`); nothing stored, so jobs read before this ship get suggestions too.

## 5. Choice storage and tailoring

- `JobApplyRow.promptChoice: Dict[accountId, promptId]`. A choice naming a deleted prompt is ignored.
- Effective prompt = choice → suggestion → default. `_tailor_row` passes its body to
  `enqueue_generation(prompt_body=…)` (stored as `ResumeApplication.promptOverride`, which already wins at generation
  time and survives restarts); empty body → None (profile/global fallback as today).
- `ResumeApplication` gains `promptId`, `promptName`; `_tailored_out` returns `promptName`.
- `requeue_failed` refreshes `promptOverride` / `promptId` / `promptName`, so a retry uses the current selection.
- Applies to Tailor, Tailor all, Tailor resumes (N), Retry and re-tailor.

## 6. Testing

- Frontend: `tsc -b --noEmit`; headless-browser checks with a mocked backend — profile prompt tabs (add, rename, role,
  default, delete rules, validation, unsaved •, 70/30 and stacked layouts) and the Prompt column (suggested value,
  Job-cell role tag + marks ≤ 7, change → saved, one-prompt plain text, tailor uses it).
- Backend (written first, mongomock): prompt save / validation / lazy migration / `resumePromptBody` sync both ways;
  suggestion (title, skills, ties, no role → default, marks order and limit); row `prompt` summary; choice save /
  clear / deleted-prompt fallback; `run.profiles[].prompts`; tailoring puts the chosen body in `promptOverride` with
  `promptId/Name`, retry picks up a new choice, `tailor-all` uses each row's prompt.

## Rollout

Backend first (all new fields optional; older frontends ignore them); the frontend reads `resumePrompts`, `prompts`
and `prompt` only when present.
