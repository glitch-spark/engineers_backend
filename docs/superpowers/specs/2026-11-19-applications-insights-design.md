# Applications insights: which profiles and which jobs turn bids into interviews — design

Date: 2026-11-19. Repos: engineers_backend (FastAPI + Beanie) and engineers_app (React + SWR).
Follows `2026-11-18-stats-insights-design.md` (the you-vs-team frame on Bids / Interviews stays as the page header).

## Why

The Bids and Interviews tabs now compare a person with the team, but don't help them get **more interviews**. People
need to see which of their **profiles** and which **kinds of jobs** turn applications into interviews, how long
interviews take to come, and what to change in a weak profile. The user called "what kind of job works" the most
important part.

## Goals

1. **Applications table** — one row per identifiable application with its outcome, kept fresh by the cron.
2. **Response window** — the team's "half of interviews come within X days, 90% within N days".
3. **Profile report card** — per profile: bids (self / bidder), matured per-100, speed (first / median / slowest days),
   how far interviews get, region, grade (✅ strong / ⚠ watch / ⛔ dead).
4. **What kind of job works** — four ways (user decision, 2026-11-19): by **job site** (link domain), by **resume score**
   band, by **job area** (Backend, Full stack, Frontend, AI / ML, DevOps, QA, Data, Mobile, Other / unknown), and by
   **profile age** when the bid went out (< 2 weeks, 2–6 weeks, 6 weeks – 3 months, 3+ months). Prompt / template is
   dropped from the page.
5. **What to change** — suggestions matched to each profile's pattern (table below).
6. **Winning vs losing profiles** — top 3 vs bottom 3 side by side.
7. **When to bid** (weekday / hour) and **bidder quality** (per-100 per bidder, rejected share).

## Non-goals

- Changing how bids, resumes or interviews are recorded.
- Automatic changes to profiles (the page suggests; people decide).
- Typed self-bid counts in job-level breakdowns (they have no job attached; they stay in totals only).

## Anti-goals

- Judging a profile on bids too young to have produced interviews.
- A per-job breakdown that silently ignores unmatched data — every breakdown says how many applications it covers.
- Slow pages: no live joins over tens of thousands of rows on a page view.

## Decided by the user

- Period = **bids made in the period, and what became of them** (cohort). Bids younger than the response window show as
  "still in play" and aren't counted against anyone.
- Build approach = **precomputed applications table** updated by the existing 5-minute cron (keen-spirit
  `app.alerts_tick`), plus a one-time backfill.
- Keep the you-vs-team header on Bids / Interviews; the breakdowns replace what's below it.

## Approaches considered

1. **Precomputed applications table (chosen).** Fast reads, one place for every breakdown; extraction adds columns
   later. Cost: new collection, backfill, incremental job, ≤ 5 min lag.
2. Live joins per request — exact but seconds in prod, minutes over a slow link; grows with data. Rejected.
3. Matched bid stored on the interview only — "bids that got nothing" still need live scans. Rejected.

## Data model: `applications` collection

One document per application, keyed by **(profile id, job key)**; when there's no job link, by the source record id.

| Field | Source |
|---|---|
| `accountId` (profile), `ownerId`, `region` | Bid.accountId / ResumeApplication.accountId; profile's region |
| `jobKey` | `resume_application.job_url_key(jobUrl)` (same key tailored resumes already store as `jobUrlKey`) |
| `board` | registrable domain of the job link (`greenhouse.io`, `ashbyhq.com`, `myworkdayjobs.com`, …), `null` without a link |
| `appliedAt` | earliest of: Bid.firstAt, ResumeApplication.createdAt (resumes with `countsAsBid != False`) |
| `by` | `"bidder"` (Bid kind bidder; `bidderId`) or `"self"` (tracked self Bid, or a tailored resume with no bidder bid) |
| `bidStatus` | Bid Track review status when from a bid (approved / pending / rejected); rejected bids are kept but excluded from conversion |
| `score`, `scoreBand` | ResumeApplication.matchScore total / band (null when no tailored resume) |
| `promptId/Name`, `templateName` | from the tailored resume (kept; not shown on the page for now) |
| `title`, `area` | job title = first line of the tailored resume's `jobDescription`, else the readable part of the bid's `jobKey` job segment (slug → words; ids give none); `area` = keyword rules on the title (Backend, Full stack, Frontend, AI / ML, DevOps, QA, Data, Mobile, else Other); no title → `null` (shown as Unknown) |
| `profileAgeDays` | `appliedAt` − the profile's `createdAt`, in whole days |
| `interviewId`, `firstRoundAt`, `daysToInterview`, `furthest` (intro/tech/hiring/panel/final/offer), `result` | matched interview |
| `sources` | ids of the bid(s) / resume(s) merged into the row |

**Matching an interview to its application** (first that works): `interview.resumeLink.resumeJobId` → that resume's row;
else the interview's own job link key + profile → the row with the same `(accountId, jobKey)`. `daysToInterview` =
first held/scheduled round date − `appliedAt` (whole days, ≥ 0). An interview matching nothing counts in totals as
"unmatched".

**Freshness:** each cron tick upserts rows for bids / resumes created or changed since the last tick, and re-matches
interviews created or changed since the last tick (a `stateKey` checkpoint in the existing settings collection). A
backfill script builds everything from Aug 1, 2026 (dry run by default, `--apply` to write).

## The response window

Over matched applications in the last 90 days: median and 90th percentile of `daysToInterview` (team-wide; per profile
when it has ≥ 5 matched). **Matured** = `appliedAt` older than the team 90th percentile (minimum 7 days). Conversion,
grades and "dead bids" use matured applications only; the rest show as "still in play: N".

## Page (Bids / Interviews tabs)

Header: the existing you-vs-team cards (unchanged). Below, in order:

1. **Coverage line** — "Job-level numbers cover 1,240 of 1,410 bids (88%); typed self bids aren't tied to jobs."
2. **Your profiles** — report card per profile (the subject's; admins can view anyone): bids self / bidder, matured,
   interviews, per-100 vs team, first / median / slowest days, reach (intro → tech → final), grade, still-in-play.
3. **What kind of job works** — per-100 by job site, resume score, job area and profile age (the subject's and the
   team's side by side; groups with < 30 matured applications show "too few"; Unknown area shown with its count).
4. **What to change** — suggestions per profile from the pattern table.
5. **Winning vs losing profiles** — team top 3 vs bottom 3 by matured per-100 (≥ 100 matured each): region, avg score,
   prompt / template, top boards, median days, self/bidder mix.
6. **When to bid** — per-100 by weekday and by 3-hour block of `appliedAt` (app timezone).
7. **Bidder quality** (owners and admins) — per bidder: bids, matured per-100, rejected share.

Grades (thresholds from team numbers): ✅ matured per-100 ≥ team median and interviews reach tech or later;
⚠ below median, or median days ≥ 2× team's, or intros that never reach tech; ⛔ ≥ 150 matured bids and 0 interviews,
or no interview for 21 days while still bidding.

### What to change (pattern → suggestion)

| Pattern | Suggestion |
|---|---|
| Low per-100 and low scores on its jobs | Improve the base resume or the role's prompt; check it on Prompt tests |
| Low per-100 and good scores | Move bids to the boards (and later titles) that convert for the best profiles; drop the worst board |
| Intros but no tech rounds | Tone the prompt down to real skills; add interview prep |
| Slow (median days ≥ 2× team) | Sharpen the summary; try the winning profiles' template |
| Self converts, bidder doesn't | Review that bidder's jobs and rejected share |
| Volume up, per-100 down | Fewer, better-matched applies (score ≥ the band that converts) |
| ⛔ Dead | Archive, or rebuild with a new base resume / prompt and judge it fresh after the response window |

## Build order (slices, each ships alone)

1. Applications table + matching + backfill + cron update + response window (backend only; an admin-only debug
   endpoint shows coverage).
2. Profile report card + coverage line (Bids / Interviews tabs).
3. What kind of job works (board, score band, prompt, template).
4. What to change suggestions + winning vs losing.
5. When to bid + bidder quality.
6. Seniority / location extraction from job descriptions → new breakdown columns.

## Constraints

- Mon–Fri, America/Chicago (app tz) for weekday / hour; dates `YYYY-MM-DD`.
- Same admin `user` rule as `/stats` (staff see themselves; admins anyone).
- Never run scripts against the real DB without dry run first; backfill and cron writes are idempotent (upserts keyed
  as above).
- Tests: backend `tests_unit/` (mongomock); app vitest; TDD.

## Decisions from the builder check (my calls — check these)

1. **Stage buckets** reuse `interview_rounds._breakdown_group`: intro; tech (tech, tech1/2, tech_round_1/2,
   live_coding, system_design); hiring (cultural, hiring_manager); panel; final (final, offer). AI interviews and home
   assessments are ignored (as everywhere else). "Reached tech or later" = any tech / hiring / panel / final round.
   `furthest` = the latest bucket among the interview's rounds; `result` = `caller_stats.round_result` of that furthest
   round (passed / rejected / awaiting / scheduled).
2. **Days to interview** = first round of the interview that isn't canceled (held or still scheduled) − `appliedAt`.
   An interview whose every round was canceled doesn't count as an interview (same as the held-round rule).
3. **Per-100 divides by table rows** (matured, not rejected by Bid Track review). The coverage line compares table rows
   in the period with the header's total bids (which include typed self counts), so it reconciles with the header.
4. **Merging**: rows key on `(profile, job_url_key)`; a bid without a job link gets its own row (can't be matched;
   counted in coverage as "no job link"). When a bidder bid and a resume share a row, `by = "bidder"`.
5. **Freshness**: the 5-minute cron upserts rows for bids / resumes / interviews changed since the checkpoint
   (`updatedAt` / `firstAt` / `createdAt`); a **daily full rebuild** (idempotent) removes rows whose sources were
   deleted and fixes re-linked interviews.
6. **Response window is computed on read** from the table (one aggregation, cached in-process for 10 minutes) — "matured"
   is never stored, since it moves with the window.
7. **Thin data fallback**: with < 30 matched applications in the last 90 days, the window is 14 days (median shown as
   "not enough data"); the matured cutoff is never below 7 days.
8. **Interviews without a resume row** (kind uploaded / fixed / none, or no link): matched by their own job link + profile
   when they have one; otherwise they count in totals as "unmatched" and show in the coverage line.

## Left to the builder

- Module layout (e.g. `app/applications/` with `build.py`, `match.py`, `window.py`), index choices on the new
  collection, the exact registrable-domain rule (a small known-suffix list is fine), chart styling.
