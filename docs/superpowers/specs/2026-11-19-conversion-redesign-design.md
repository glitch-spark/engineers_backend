# Statistics → Conversion redesign (real totals, key findings, rate bars) — design

Date: 2026-11-19. Approved by the user from the mockup
(`engineers_app/.superpowers/brainstorm/162256-1795140102/content/conversion-redesign.html`).

## Why

People should see **correct numbers** (the bids and interviews they know they made) with their rank against the team,
and learn quickly **what raises bid → interview conversion** so they come back to the page. Today the top strip shows
bids only (Plans + Bid Track), the breakdowns use job-level data with a different "bids" number, and the tables are
plain number grids.

## Goals (user-chosen: "1, 3" from the brainstorm + the top section)

1. **Top cards** (your numbers vs the team): Bids (real Plans + Bid Track count; self / bidder split), Interviews
   (interviews **started** in the period = interviews whose first non-canceled round falls in the period, one per
   company process; note shows rounds held), Interviews per 100 bids. Each: value, rank `#r of n`, % vs team median
   (per 100: vs team rate), basis line.
2. **Everyone**: one ranked list with a switch Bids | Interviews | Per 100 bids (default Interviews); every row shows
   all three numbers; the subject's row highlighted; admins can click a name to view that person (as today).
3. **Key findings**: 3–4 plain sentences generated from the breakdowns, ★ (works) / ⚠ (doesn't), each quoting counts,
   only when backed by ≥ 3 interviews or ≥ 3 expected at the team rate.
4. **Coverage line**: `Breakdowns below use job-level data: they cover {rows} of {bids} bids and {matched} of
   {interviews} interviews.`
5. **Rate bars** in every breakdown table: bar per group with a tick at the team rate; ★ on the best group and ⚠ on
   the weakest (same evidence rule); "few" rows muted.
6. Order: filters → top cards → Everyone → Key findings → coverage line → breakdowns → What to change → Your profiles →
   When to bid. The old header strip (StatsFrame bids) is removed from this tab.

## Non-goals

Recent-interviews feed, trend chart, drill-down (deferred by the user).

## Behaviour by Profiles filter

- **All my profiles**: cards = the subject vs the team (rank, % vs median); Everyone highlights the subject;
  findings may include "you vs team" contrasts (e.g. your bidder vs self rates).
- **Whole team** (default): cards = team totals (no rank; median per person shown as the note); Everyone unchanged;
  findings are team-level.

## Key definitions

- Bids per person = `interview_rates.counts` total as the Bids tab uses it (same period, region).
- Interviews started per person = interviews created by that person (owner) whose first non-canceled round (per
  `app.applications.rows.interview_outcome`) is in the period; region = the interview's profile's current region
  (archived included); AI interviews / home assessments excluded as everywhere.
- Rounds held = non-canceled rounds in the period on those people's interviews (note only).
- Team median / rank over the team members with ≥ 1 bid in the period (as the Bids frame does).
- Per 100 bids = interviews started ÷ bids × 100; ranked only for people with ≥ 30 bids.

## Findings rules (backend, pure; ordered, max 4)

From the conversion report groups (team rows for Whole team; the subject's rows for All my profiles):
- **Best group per breakdown** (score, site, area, age, self vs bidder): highest rate with `few == False` and
  ≥ 3 interviews and rate ≥ 1.5 × team rate → ★ `{label} converts best for {by}: {rate}% ({i} interviews from {m}
  matured bids) vs {team}% overall.`
- **Weak big group**: matured × team rate / 100 ≥ 3 and 0 interviews → ⚠ `{label}: 0 interviews from {m} matured
  bids — the team rate would give about {e}.`
- **Self vs bidder gap** (All my profiles): both ≥ 30 matured, self ≥ 3 interviews, self rate ≥ 2 × bidder rate → ⚠
  `Your bidder bids convert at {b}% vs your self bids {s}% ({bi} of {bm} vs {si} of {sm}).`
- Ranked by size of effect; at most 4; none → `No clear pattern yet in this period — try a longer date range.`
- Groups get `mark: "best" | "weak" | null` from the same rules (used for ★ / ⚠ in tables).

## Approach

One sensible way: extend `GET /stats/conversion` with a `summary` block (cards, everyone, coverage, findings) and
`mark` on groups; the app redesigns `ConversionTab`. Computing findings in the browser was rejected — the evidence
rules belong in the tested backend.

## Constraints

No real-DB runs in tests; commits single line, no co-author / "Generated with" trailers; TDD; backend full suite with
the usual ignores (2 known failures); app vitest + tsc.
