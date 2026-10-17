# Resume units: an HTML template + its prompt + role marks — design

Date: 2026-10-17 · Repos: engineers_app (frontend), engineers_backend · Branch: `feat/resume-units` (on top of
`feat/job-apply`, which already has role-based prompts — `2026-10-17-profile-role-prompts-design.md`)

## Goal

A profile holds several **resume units**. A unit is one HTML template, the prompt written for it, and the job roles it
is meant for (several role marks, e.g. Full stack + Frontend). Job Applies shows each job's roles with the same marks,
suggests the unit whose marks fit the job best, and tailors with that unit's prompt **and** template.

## Non-goals (this round)

- Unit pickers on the Generate page, in Prompt Eval and for screening answers: they keep using the profile's **default
  unit** (unchanged behaviour, via the synced legacy fields).
- Eval on save (the Eval panel stays a disabled placeholder).
- A page-format setting in the UI (each unit keeps the profile's current format, A4 by default).

## Roles

Unchanged list: `fullstack` Full stack · `frontend` Frontend · `backend` Backend · `devops` DevOps · `data` Data ·
`ml` ML / AI · `mobile` Mobile · `qa` QA / Test. A unit may have 0–8 role marks; none = "All roles (general)".

---

## 1. Frontend design

### 1a. Profile page — "Resume units" (replaces the separate "HTML template" section and the prompt tabs)

```
Resume units
One HTML template + its prompt per unit. Job Applies suggests the unit whose roles fit each job.

 [ Resume Tailoring — Master Prompt ★ ] [ DevOps • ] [ + Add unit ]          (★ default, • unsaved)
 Name [ DevOps                  ]   Roles [DevOps ✕] [Backend ✕] [+ role ▾]   [☆ Set as default] [🗑 Delete]

 ┌──────────── 40% ────────────┐ ┌─────────── 30% ───────────┐ ┌────── 30% ──────┐
 │ HTML template               │ │ Prompt    19120/30000      │ │ Eval            │
 │ ⧉ devops-modern.html · 48 KB│ │           Load global      │ │                 │
 │ [Upload] [Preview] [Remove] │ │ [ text …                 ] │ │  No eval yet    │
 │ ┌─────────────────────────┐ │ │                            │ │                 │
 │ │ inline preview (scaled, │ │ │                            │ │ [▶ Run eval]    │
 │ │ sandboxed iframe)       │ │ │                            │ │ (off)           │
 │ └─────────────────────────┘ │ │                            │ │ Coming soon     │
 └─────────────────────────────┘ └────────────────────────────┘ └─────────────────┘
                                                                 [Save resume units]
```

- **Layout**: three columns from `lg` — HTML template **40%**, Prompt **30%**, Eval **30%**
  (`lg:grid-cols-[4fr_3fr_3fr]`); stacked (template, prompt, Eval) below `lg`.
- **HTML template column**: file name and size, Upload / Preview (full page in a new tab) / Remove, and an inline
  preview of the template — a scaled, sandboxed `srcdoc` iframe (scripts off, same sanitizing as today's preview),
  same height as the prompt box. No template: "No HTML template — Job Applies can't tailor with this unit" with
  the upload drop zone in place of the preview.

- Tabs, name, default and delete behave as today's prompt tabs (★, •, rules, validation, ≤ 10).
- **Roles**: chips with ✕; "+ role" menu lists roles not yet added. No chips = "All roles (general)".
- **HTML template** upload uses the same picker and checks as today's template pane (`.html` only). New units start
  with no template.
- One **Save resume units** button saves all units (and, as today, the screening / cover-letter prompts in the same
  block keep their own fields).
- The page no longer shows the separate "HTML template" section; the default unit's template is the profile template.

### 1b. Job Applies

```
│ Job                                   │ … │ HTML template          │ Prompt                     │ Tailored     │
│ Senior Full Stack Engineer            │   │ ⧉ fullstack.html       │ [ Full stack + DevOps ▾ ]  │ [✨ Tailor]   │
│ Acme  open ↗  📄                      │   │                        │ Suggested                  │              │
│ [Full stack] [DevOps]                 │   │ ⧉ master.html          │ Resume Tailoring — Master  │ ✨ Tailored 88│
```

- **Job cell**: the job's role marks only (no skill chips), strongest first, up to 3. Hover: per role, the title words
  and skills that gave it ("Full stack — title says Full Stack · DevOps — skills Kubernetes, Terraform").
- **Prompt column**: a select of the profile's units (option text = unit name, roles in grey when the name doesn't
  already say them), value = the user's choice, else the suggestion. "Suggested" / "Default" tag as today.
- **HTML template column**: the selected unit's template name; "No HTML template" (and Tailor disabled) when that unit
  has none.
- Tailored chip tooltip: "Tailored with <unit name>".

### 1c. Build order

Frontend first against the API shapes below (headless browser, mocked backend), then backend (tests first), then
together.

---

## 2. Storage (backend)

`ResumePrompt` (stored in `Account.resumePrompts`) becomes the unit:

```python
class ResumePrompt(BaseModel):
    id: str
    name: str
    roles: List[PromptRole] = []          # replaces `role`; a stored `role` is read as [role]
    body: str = ""                        # the prompt
    templateHtml: Optional[str] = None    # None = not set yet (legacy: inherits the profile template), "" = none
    templateName: Optional[str] = None
    pageFormat: PageFormat = "A4"
    updatedAt: datetime
```

- **Lazy migration** (no script), in `prompts_view`. For every existing profile, **the previous HTML template + the
  master prompt form one unit, and that unit is the default**:
  - no units → one unit from the legacy fields: id `default`, name from the prompt heading (as today), roles [],
    body = `resumePromptBody`, template = `styleTemplate` / `styleTemplateName`, `pageFormat` = the profile's; it is
    the default.
  - prompts saved before units existed (no stored unit has a `templateHtml` yet): the unit with id `default` (the
    master prompt) gets the previous template and **is the default**, whatever `defaultPromptId` says; other units
    with `templateHtml is None` also get a copy of the previous template so they keep working (they can upload their
    own later).
  - a stored `role` → `roles = [role]`.
  - After the profile's next save every unit stores its own `templateHtml`, and `defaultPromptId` is the user's choice
    from then on.
- **Default sync**: after every save, the legacy fields mirror the default unit — `resumePromptBody`, `styleTemplate`,
  `styleTemplateName`, `pageFormat`. Every reader of those fields (Generate page, Prompt Eval, screening, lookup
  badges, `_parse_selection`) keeps working on the default unit. A write of `styleTemplate` / `resumePromptBody` alone
  (older screens) updates just the default unit (as `sync_default_body` does today).
- **Validation** (400 with a message): as today (≤ 10, names, body ≤ 30000, default in list) + roles from the list
  without duplicates, `pageFormat` in `PageFormat`, template ≤ 2 MB. Templates are sanitized with
  `sanitize_template_html` on save (create path included).
- `GET /accounts` (list) strips `templateHtml` and `body` from units (plus resume markdown, as today).

## 3. API

- `GET/PUT /accounts/{id}`: `resumePrompts[]` carry the unit fields above; PUT takes the whole list + `defaultPromptId`.
- Job Applies run (`_run_out(with_profiles=True)`): each profile's `prompts[]` = `{id, name, roles, hasTemplate,
  templateName}` (no body / html) + `defaultPromptId`. `hasTemplate` / `templateName` at profile level stay = the
  default unit's.
- Row summary: `jobRole = {roles: [str], reason: str}` (replaces `{role, marks, reason}`); `prompt` per profile
  unchanged (`suggestedId`, `chosenId`).
- `PUT /job-applies/rows/{id}/prompt` unchanged.

## 4. Job roles and suggestion (`app/job_apply/prompt_suggest.py`)

- `job_roles(title, terms) -> RoleMatch(roles: list[str], reason: str)`. Same keyword table and scoring as today
  (title × 3, required/core skills × 2, others × 1, longest keyword wins on overlap). A role is shown when its score
  is ≥ 2 and ≥ half the top role's score; at most 3, ordered by score, then by first position in the title.
- `suggest_unit(units, default_id, job_roles) -> str`:
  1. most role marks in common with the job's roles;
  2. tie → a unit that has the job's main (first) role;
  3. tie → the default unit;
  4. tie → the most recently updated.
  No overlap (or no job roles) → the default unit. Units without a template are still suggested (the cell then says
  "No HTML template").

## 5. Tailoring with the unit's template

- `generate_resume(account, …, template_html=None, page_format=None)` / `generate_resume_html(…, template_html=None)`:
  given values override `account.styleTemplate` / `account.pageFormat`.
- `ResumeApplication` gains `templateName` (display). `_run_job` resolves the unit from `job.promptId` on the account
  (`find_prompt`): its template and format; a deleted unit, or a job without `promptId` → the profile's (default)
  template, as today. The prompt text stays snapshotted in `promptOverride`.
- `_tailor_row` passes the effective unit (choice → suggestion → default); `_check_inputs` / tailoring refuse a unit
  without a template with 400 "This resume unit has no HTML template." (`tailor-all` counts it as skipped).
- `requeue_failed` refreshes `promptId` / `promptName` / `templateName` too.

## 6. Testing

- Frontend: `tsc -b --noEmit`; headless checks — units tabs (roles chips add/remove, template upload/preview/remove,
  validation, default, save payload), Job Applies (role marks, Prompt select options, HTML template column follows the
  selected unit, Tailor disabled without template).
- Backend (tests first, mongomock): unit migration (legacy fields, `templateHtml None`, `role` → `roles`), validation,
  default sync both ways incl. template-only writes, list stripping; `job_roles` (multi-role, half-score rule, cap 3,
  order); `suggest_unit` (both marks, main role, default, latest, no overlap); `_run_out` unit fields; tailoring uses
  the unit's template/format (generation receives it), deleted unit → profile template, unit without template → 400.

## Rollout

Backend first (old frontends keep working: legacy fields still mirror the default unit). The frontend reads `roles` /
`templateHtml` when present and treats `role` as `[role]`.
