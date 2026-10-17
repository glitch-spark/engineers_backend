# Resume Units Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each profile prompt becomes a resume unit (HTML template + prompt + several role marks); Job Applies shows
job roles, suggests the best unit and tailors with that unit's prompt and template.

**Architecture:** Units stay in `Account.resumePrompts` (`ResumePrompt` gains `roles`, `templateHtml`, `templateName`,
`pageFormat`); `prompts_view` migrates lazily and the legacy fields mirror the default unit. `prompt_suggest` returns
several job roles and picks the unit with the most overlap. Generation takes an optional template/format override,
resolved from the job's `promptId`.

**Tech Stack:** FastAPI + Beanie, pytest + mongomock-motor; React + TS + Tailwind; Playwright (backend venv).

**Spec:** `docs/superpowers/specs/2026-10-17-resume-units-design.md` (engineers_backend)

**Repos** (branch `feat/resume-units` in both): backend `be:` = `C:\Users\Administrator\Downloads\engineer\engineers_backend-job-apply`,
frontend `fe:` = `C:\Users\Administrator\Downloads\engineer\engineers_app-job-apply`. Commands as in
`2026-10-17-profile-role-prompts.md` (venv python, ignored broken tests, known `test_bidder_ext` failure, Node PATH).

## Global Constraints

- Roles: `fullstack, frontend, backend, devops, data, ml, mobile, qa` (labels unchanged); 0–8 per unit, no duplicates;
  none = "All roles (general)".
- ≤ 10 units; names 1–60 unique (case-insensitive); body ≤ 30000; template ≤ 2 MB, sanitized with
  `sanitize_template_html`; `pageFormat` ∈ A3/A4/A5/Letter (default: the profile's, else A4).
- Existing profiles: previous template + master prompt = unit id `default`, and it is the default until the profile's
  next save (spec §2).
- Legacy fields (`resumePromptBody`, `styleTemplate`, `styleTemplateName`, `pageFormat`) mirror the default unit after
  every save.
- Job role marks: score ≥ 2 and ≥ half the top score, ≤ 3, order by score then title position.
- Unit editor: `lg:grid-cols-[4fr_3fr_3fr]` (template 40% / prompt 30% / Eval 30%); stacked below `lg`.
- Unit without template: Job Applies shows "No HTML template", Tailor disabled; server answers 400 "This resume unit
  has no HTML template.".

## Review Focus

1. A profile saved during prompt testing (units without `templateHtml`, `defaultPromptId` ≠ `default`) → master unit
   gets the old template and is the default; other units get a copy. Task 4 test.
2. A unit whose template is removed but stays selected on jobs → HTML template cell "No HTML template", Tailor
   disabled, `tailor-all` skips it, no crash. Tasks 3 and 7.
3. An older screen writes only `styleTemplate` (Generate page has none, but `POST/PUT /accounts` still accepts it) →
   only the default unit's template changes. Task 4 test.
4. A job tailored before units (no `promptId`) retried after units ship → uses the profile (default) template. Task 7
   test.
5. A JD matching 4+ roles, or several roles tied → at most 3, deterministic order. Task 5 test.

---

## Frontend

### Task 1: Types and unit helpers
**Files:** Modify `fe:src/api/endpoints.ts` (`ResumePrompt`, `JobApplyPromptOption`, `JobApplyRow.jobRole`,
`JobApplyTailored`); Modify `fe:src/components/prompts/promptRoles.ts`.
**Produces:**
```ts
export interface ResumePrompt { id; name; roles: PromptRole[]; role?: PromptRole | null; body: string;
  templateHtml?: string | null; templateName?: string | null; pageFormat?: string; updatedAt?: string }
export interface JobApplyPromptOption { id; name; roles: PromptRole[]; hasTemplate: boolean; templateName: string | null }
// JobApplyRow.jobRole?: { roles: PromptRole[]; reason: string }; JobApplyTailored.templateName?: string | null
export function unitRoles(p: { roles?: PromptRole[] | null; role?: PromptRole | null }): PromptRole[]  // role → [role]
export function rolesLabel(roles: PromptRole[]): string   // "Full stack + DevOps" / NO_ROLE_LABEL
```
- [ ] Implement; `tsc` → only known error; commit `feat(units): types and role helpers for resume units`.

### Task 2: Profile page — Resume units editor (40/30/30)
**Files:** Rename `fe:src/components/prompts/ResumePromptsEditor.tsx` → `ResumeUnitsEditor.tsx` (same props, unit
fields); Create `fe:src/components/prompts/TemplatePane.tsx`; Modify `fe:src/pages/AccountEdit.tsx` (drop the "HTML
template" section that renders `ResumeStylingEditor`; `PromptsBlock` loads/saves units incl. `roles`,
`templateHtml`, `templateName`, `pageFormat`; legacy fallback builds the unit from `styleTemplate`).
**Produces:** `TemplatePane({ html, name, onChange(html, name), onRemove })` — upload (`.html`, reuse
`sanitizeTemplateHtml` / checks from `ResumeStylingEditor`), Preview (`openTemplatePreview`), Remove, inline scaled
sandboxed `srcdoc` iframe; empty → drop zone + "No HTML template — Job Applies can't tailor with this unit".
Roles: chips with ✕ + "+ role" `<select>` of remaining roles.
- [ ] Build; `tsc`.
- [ ] Browser check (mocked `/accounts/acc1`): legacy account shows one ★ unit named from heading with the legacy
  template preview; add unit, add roles DevOps + Backend, remove Backend, upload a template file (set input files),
  preview iframe shows it; Save → PUT units with roles/templateHtml/templateName; 1600px: three columns ≈ 40/30/30;
  900px stacked; no page errors. Commit `feat(units): resume units on the profile page (template, prompt, roles)`.

### Task 3: Job Applies — role marks, unit select, template column
**Files:** Modify `fe:src/components/jobApplies/JobRoleMarks.tsx` (roles list), `PromptCell.tsx` (option label
`name` + roles via `rolesLabel` when the name doesn't say them), `Suggestions.tsx` (`ProfileCells`: template cell and
Tailor disabled from the **effective unit**: chosen → suggested → default), `fe:src/pages/JobApplyRun.tsx` if needed.
- [ ] Build; `tsc`.
- [ ] Browser check with mocks: job with roles [fullstack, devops] → two marks; select shows units; switching the
  unit changes the HTML template cell; a unit without template → "No HTML template" + Tailor disabled. Commit
  `feat(job-applies): job role marks and resume unit select with its template`.

## Backend

### Task 4: Unit storage, migration, sync
**Files:** Modify `be:app/models/account.py` (`ResumePrompt`), `be:app/resume_prompts.py`, `be:app/routers/accounts.py`
(`ResumePromptIn`, create sanitize, list strip, `styleTemplate`-only writes); Test `be:tests_unit/test_resume_prompts.py`.
**Produces:** `prompts_view(account) -> (units, default_id)` with spec §2 rules; `apply_prompts(account, units,
default_id)` validates roles/format/template size, sanitizes templates, mirrors legacy fields;
`sync_default_template(account, html, name)`; `unit_template(unit) -> tuple[Optional[str], str]` (html, format).
- [ ] Failing tests: `test_legacy_profile_is_one_default_unit_with_its_template`;
  `test_units_saved_before_templates_get_the_old_template_and_master_is_default` *(RF1)*;
  `test_role_is_read_as_roles`; `test_apply_units_sanitizes_and_mirrors_legacy_fields`;
  `test_apply_units_rejects_bad_roles_format_and_big_template` (parametrized: duplicate role, unknown role via router →
  422, pageFormat "B5", 2 MB+1 template); `test_template_only_write_updates_the_default_unit` *(RF3)*;
  `test_account_list_strips_unit_bodies_and_templates`. Run → fail; implement; pass; full suite; commit
  `feat(units): resume units store their own template and roles`.

### Task 5: Several job roles and the unit suggestion
**Files:** Modify `be:app/job_apply/prompt_suggest.py` (`job_roles`, `suggest_unit`; drop `job_role` / `suggest_prompt`
and marks), Test `be:tests_unit/test_prompt_suggest.py` (rewrite).
**Produces:** `RoleMatch(roles: list[str], reason: str)`; `job_roles(title, terms) -> RoleMatch`;
`suggest_unit(units, default_id, roles) -> str`.
- [ ] Failing tests: keep the whole-word / React Native / speed / no-extraction tests on `job_roles`; add
  `test_full_stack_and_devops_job_has_both_roles`, `test_weak_second_role_is_left_out` (below half),
  `test_at_most_three_roles_in_order` *(RF5)*, `test_suggest_unit` (both marks > main role > other overlap > default;
  tie → default, then latest; no overlap → default). Implement; pass; commit
  `feat(job-applies): several roles per job and the best-matching resume unit`.

### Task 6: Job Applies payloads
**Files:** Modify `be:app/routers/job_applies.py` (`_prompt_info`: `{id, name, roles, hasTemplate, templateName}`;
`_AccountPromptsView` projection `resumePrompts.roles`/`role`; `_job_role` → `job_roles`; `jobRole = {roles,
reason}`; `_prompt_state` / `choose_prompt` / `_effective_prompt` use `suggest_unit`); Test
`be:tests_unit/test_job_apply_prompts.py` (update + new).
- [ ] Tests: `test_run_profiles_list_units_with_template_info`, row summary roles, suggestion with two-role unit,
  existing choice / deleted / no-extraction tests updated. Implement; pass; commit
  `feat(job-applies): units, job roles and suggestions in Job Applies`.

### Task 7: Tailor with the unit's template
**Files:** Modify `be:app/resume_service.py` (`generate_resume_html(..., template_html=None)`, `generate_resume(...,
template_html=None, page_format=None)`), `be:app/routers/resume.py` (`_run_job` resolves unit from `job.promptId`),
`be:app/resume_queue.py` (`_check_inputs(jd, account, template_html=None)`, `templateName` param on enqueue/requeue,
`_QUEUE_OWNED`), `be:app/models/resume_application.py` (`templateName`), `be:app/routers/job_applies.py`
(`_tailor_row` 400 for a unit without template; pass `template_name`; `_tailored_out.templateName`); Test
`be:tests_unit/test_job_apply_prompts.py` + `be:tests_unit/test_resume_generate_html.py`.
- [ ] Tests: `test_generation_uses_the_given_template` (generate_resume_html with override, LLM stubbed as in
  `test_resume_generate_html.py`); `test_run_job_uses_the_jobs_unit_template` (stub `generate_resume`, assert
  template/format passed); `test_job_without_unit_uses_profile_template` *(RF4)*;
  `test_tailoring_a_unit_without_template_is_refused` + tailor-all skips it *(RF2)*; `templateName` stored and in
  `_tailored_out`. Implement; pass; full suite; commit `feat(job-applies): tailor with the resume unit's template`.

### Task 8: End-to-end and hand-off
- [ ] Restart backend; real-shape JSON (as `t8_shapes.py`) → re-run Task 2/3 browser checks; full suite; `tsc`;
  report; ask before merge/push.
