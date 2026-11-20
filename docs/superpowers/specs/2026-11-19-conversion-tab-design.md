# Statistics "Conversion" tab — design

Date: 2026-11-19. Repos: engineers_backend (FastAPI + Beanie) and engineers_app (React + SWR). Builds on the
applications table and cards of `2026-11-19-applications-insights-design.md` (slices 1–5, branch `feat/stats-insights`).

## Why

People need to learn **which factors raise bid → interview conversion**: who applied (self or which bidder), where
(job site), with what resume (score), for what kind of job (area), and on how old a profile. Today the Bids and
Interviews tabs split this across several you-vs-team cards that are hard to read; the user wants one tab with plain
breakdown tables over a period long enough to mean something.

## Goals

1. One tab, **Conversion**, replacing the Bids and Interviews tabs.
2. A totals line and five breakdown tables — **Bidder, Job site, Resume score, Job area, Profile age** — each row:
   **Bids · Matured · Interviews · Rate**.
3. Scope switch: **My profiles** (all merged, default) · **One profile** · **Team total**.
4. Region filter (US / EU / LATAM / All) over **active (non-archived) profiles** of that region only.
5. Period filter kept; data only from **Oct 6, 2026** (the week Bid Track bidder bids start; before it the table holds
   self bids only, so totals and rates aren't comparable).
6. Bids carry their **tailored resume's score, job title and area** (fix below), so the score and area breakdowns rest
   on ~70% of bids instead of ~5–10%.
7. "What to change" suggestions stay and are **correct**: driven by the same data, every one quotes its evidence, none
   fires on chance-level data.

## Non-goals

- Winning vs weakest profiles (removed).
- You-vs-team comparison columns inside the breakdown tables (scope picks whose numbers you see).
- Bidder names across owners: the Bidder table is hidden in Team total.

## Anti-goals

- A rate that looks meaningful but rests on a handful of bids without saying so.
- A suggestion that contradicts the tables or the profile's grade.
- Counting tailored-but-unapplied resumes as extra bids (they enrich the bid; they are not bids).

## Decided by the user (2026-11-19)

- Replace the Bids and Interviews tabs. Name: **Conversion** (controller proposal, accepted implicitly; easy to change).
- Scope switch as above; Team total skips the Bidder table.
- Data starts Oct 6, 2026.
- Rate always shown; a ⚠ when the rate is `few` (final review I2): matured < 30 → tooltip `Only {matured} matured bids — this rate can swing a lot.`; else when interviews < 3 and the team rate would also expect < 3 (matured × teamRate / 100) (changed 2026-11-19 after real data) → `Only about {expected:.1f} interviews expected from {matured} matured bids at the team rate — this rate can swing a lot.`
- Resume score = score bands + a "No score" row (no tailored resume, or an unscored one).
- Keep "What to change" (correct suggestions), drop "Winning vs weakest".
- Approach A (one new endpoint + new tab).

## Key definitions

- **Bid** = a row of the `applications` table (non-rejected; `byInterviewOnly` rows excluded, as everywhere since the
  survivor-bias fix).
- **Matured** = applied at least `matured_days` ago (fixed **14 days** — user decision 2026-11-19; it was the team response-window p90, ~27 days). Rate =
  interviews among matured ÷ matured × 100, shown as a percent with one decimal (e.g. `0.7%`). Younger bids are
  "in play" and appear only in the Bids column and the totals line.
- **Interviews** column = interviews among matured bids (same basis as the rate); the totals line also shows all
  interviews in the period.

## Data fix: tailored resumes enrich bids (backend sync)

Since Oct 6: 64k tailored resumes (96% Job Applies, `countsAsBid: false`, 98% scored) and 27.8k bids; **69% of bids
have a tailored resume for the same profile + job link**, but the sync drops unapplied resumes, so those bids carry
no score/title/area. Change in `app/applications/sync.py` / `rows.py`:

- For each row with a bid source, find unapplied (`countsAsBid: false`) resumes with the same `(accountId, jobKey)`;
  take the **earliest** one's `score`, `scoreBand`, `promptId/promptName/templateName` and job title (first line of
  its `jobDescription`) → `area`. A title already on the row from an applied resume wins; otherwise the tailored
  resume's title beats the bid's own `jobTitle` / jobKey fallback.
- Store its id as `tailoredResumeId` (new optional field). It is **not** added to `sources` (so it never counts as a
  bid and never affects `byInterviewOnly`).
- An interview linked (`resumeLink.resumeJobId`) to such a resume matches the bid row (resume_row maps the resume id
  to the bid row's key).
- Incremental sync already rebuilds every account touched by a changed resume; no extra trigger needed.
- After deploy (and locally now): one full rebuild of the prod table (user approves the `--apply` run).

## Endpoint: `GET /stats/conversion`

Params: `period, date, to, region` (as other stats endpoints) + `scope=me|profile|team` + `profile=<accountId>`
(scope=profile) + admin `user`. Rules:

- Period start clamped to 2026-10-06 (a range entirely before it → empty report with `startClamped: true`).
- `me`: rows on the subject's **non-archived** profiles in the region. `profile`: one of those (403/404 if not the
  subject's, admins any). `team`: rows on all non-archived profiles in the region.
- Response: `{label, startClamped, window, scope, totals: {bids, matured, interviews, interviewsAll, inPlay, rate},
  teamRate, byBidder (omitted for team), bySite, byScore, byArea, byAge}`; each group `{key, label, bids, matured,
  interviews, rate, few, fewReason}`: `few` when matured < 30 (`fewReason: "matured"`) or interviews < 3 and matured × teamRate / 100 < 3
  (`"expected"`; changed 2026-11-19 after real data; teamRate = the team rows' pooled matured rate, as grades use; None/0 → the matured rule only). Group labels/order as `app/stats/job_fit.py` (sites ranked by bids); Bidder:
  "Self" then each bidder by bids (name from Bidder; null → "Unnamed bidder"); Score: 80+, 65–79, 50–64, Under 50,
  No score.
- Uses the cached `load_period_rows`; one Account query for archived/region.

## Page (app)

```
Conversion   Region [US ▾]  Period [Since Oct 6 ▾]  [My profiles | Profile ▾ | Team total]
Oct 6 – Nov 19 · 9,840 bids · 6,120 matured · 41 interviews from matured bids (58 in all) · 0.7% · 3,720 in play
By bidder   By job site   By resume score   By job area   By profile age      (Bids · Matured · Interviews · Rate)
Your profiles (report card)      What to change      When to bid (+ Bidder quality)
```

- Header (you-vs-team bids) and Everyone list stay at the top (controller call: the old Interviews-tab header goes; the
  totals line covers interviews).
- New preset **Since Oct 6** (default on this tab); other presets kept; a period shorter than ~6 weeks shows the
  existing "in play / Show last month" note pattern.
- Breakdown tables: 2-column grid on desktop, 1 column on phones; ⚠ + tooltip on `few` rates (also visible text on
  tap/focus for phones).
- Scope "Team total" hides the Bidder table, "Your profiles", "What to change" and "Bidder quality" (they are about
  one person's profiles); "When to bid" shows the team numbers.
- Removed from the app: Bids tab, Interviews tab (their FramedTab usage), `JobFitCard`, `LeadersCard`, `ChangesCard`'s
  leaders sibling; removed from the backend: `/stats/applications/jobs`, `/stats/applications/leaders` and their
  modules/tests once nothing uses them.

## "What to change" must be correct

- Same filters/scope as the tables (period, region, profile).
- Every suggestion quotes its evidence with counts; fires only when expected interviews ≥ 3 (existing rule) and never
  on a profile graded ⏳/⛔ other than the dead suggestion itself.
- With real scores on ~70% of bids, `low_scores` / `wrong_sites` use the profile's scored bids; "Good resumes" only
  with ≥ 10 scored bids at or above the team average (existing rule).
- Tests on realistic sparse fixtures (0.5 interviews per 100) assert no suggestion fires by chance.

## Decisions from the builder check (controller calls — check these)

1. **Every card on the tab follows the same rules.** "Your profiles", "What to change" and "When to bid / Bidder
   quality" take `scope` + `profile`, clamp to Oct 6 and drop archived profiles, like the tables (their endpoints get
   the same params). Under One profile they narrow to that profile.
2. **Team baseline = the Team total row set** (non-archived profiles in the region, from Oct 6, same period). Grades
   and suggestion thresholds use it, so "below team" always compares with a rate visible on the page (Team total).
3. **`volume_up_rate_down` is off when its previous window starts before Oct 6** (that data is incomparable).
4. **Early (⏳) profiles:** keep today's rule — the negative suggestions (low scores, wrong sites, slow, bidder gap,
   volume up / rate down) never fire on ⏳; `stuck_intro` and `quiet_now` still can (they are facts, not rate
   judgements). ⛔ profiles get only the dead suggestion.
5. **Resume fields on a row:** an applied resume's score/prompt/template/title win when present; any field it lacks is
   filled from the earliest tailored (unapplied) resume. Enrichment only applies to rows with a bid source (rows from
   an applied resume already carry their own).
6. **`interviewsAll`** = interviews on all period rows (matured + in play), same row set as the tables
   (`byInterviewOnly` and unmatched interviews excluded; unmatched stay in the profiles card's coverage line).
7. **Region = the profile's current region** (from Account), which also defines "active profiles in the region".
   The loader loads rows by period and the endpoint filters by the region's non-archived account ids.
8. **Header + Everyone strip stays as-is** (Plans + Bid Track counts, you vs team) with a caption `Bids counted from
   Plans and Bid Track, all profiles.` so a difference from the totals line reads as intended.
9. Minor: "Since Oct 6" = a custom range Oct 6 → today; rows with no `bidderId` on a bidder bid → "Unnamed bidder";
   earliest tailored resume wins regardless of lag; 403 for another user's profile follows `_subject_id`.

## Constraints

- Never run tests or scripts against the real DB except approved runs (dry run first). Prod rebuild needs user OK.
- Tests: backend `tests_unit/` (mongomock), app vitest; TDD.
- Commits single line, no co-author / "Generated with" trailers.

## Approaches

- **A (chosen):** one new `/stats/conversion` endpoint + a new tab; delete the cards and endpoints it supersedes.
  Straight counts per scope match the user's description.
- **B:** stretch `/stats/applications/jobs` + JobFitCard with a scope param and a bidder group — less new code, but
  its you-vs-team columns are meaningless for Team total and the bidder table doesn't fit; patchwork.
- Building tables client-side from raw rows — rejected (tens of thousands of rows to the browser).

## Left to the builder

Module names/layout, exact table styling, how the profile picker lists profiles (name + region), how the tooltip is
rendered, tests layout.

## Change 2026-11-19 (user decision after real data: "this is the correct interview count")

Real data showed Conversion counting 18 of 38 October US interviews. User decisions:

1. **Archived profiles count** in every scope (their bids and interviews); region = the profile's current region; the
   profile picker lists archived profiles marked "archived". (Replaces "active (non-archived) profiles only".)
2. **Interview-proven rows count**: rows built only from an unapplied tailored resume that an interview links
   (`byInterviewOnly`) are applications and count in bids, interviews, rates, grades, suggestions and the response
   window. The survivor-bias exclusion is removed; the totals line notes: `Includes applications known only from their
   interview (no tracked bid), so rates can read a little high.`
3. **Interviews not tied to any bid** still count in the totals: `untied` = interviews on the scope's profiles whose
   first non-canceled round falls in the period and that no row carries; totals line adds `· {untied} more
   interviews not tied to a bid`. They have no bid data, so they are not in the breakdown tables.
4. **Fit score for interviews without a scored resume (option A):** a one-off script
   `scripts/score_interview_fit.py` (dry run by default, `--apply` writes; never in the cron) scores the interview's
   job description (`resumeLink.jobDescription`) against the profile's base resume text with the existing match
   scoring (`app.job_apply.tailored_score.score_manual`), for interviews whose `resumeLink.kind` is fixed / uploaded /
   none / missing and that have a job description; it stores `fitScore {total, band}` on the interview. The sync
   uses an interview's `fitScore` for its matched row's `score/scoreBand` when the row has none. Rows scored this way
   are still in the score bands (the score measures job-to-profile fit either way).
