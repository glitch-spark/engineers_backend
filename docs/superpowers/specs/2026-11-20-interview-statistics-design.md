# Statistics → Interviews tab — design

Date: 2026-11-20. Scope agreed with the user ("good with statistic tab scope"). Second tab of the same family as
Statistics → Bids → Interviews (2026-11-19-conversion-redesign-design.md); it reuses that tab's filters, layout and
components.

## Why

People should see what happens to their interviews after the first call — canceled, rescheduled, passed, rejected,
never answered — for self and caller rounds, and learn what raises the pass rate.

## Goals

One tab, "Interviews", after "Bids → Interviews", with the sections below. Filters: Region, Profiles (Whole team,
default / All my profiles), Date range (from Oct 6, 2026, default "Since Oct 6", same guidance tooltip).

1. **Top cards**: Companies interviewed · Total rounds · Pass rate · Offers (names by the user, 2026-11-20); each with rank vs the team in "All my
   profiles", team totals in "Whole team".
2. **Everyone**: ranked list with a switch Companies | Rounds held | Pass rate | Offers.
3. **Key findings**: up to 4 sentences, ★ / ⚠, same evidence idea as the other tab.
4. **By stage**: per stage — rounds, held, passed, rejected, no answer, waiting, canceled, pass rate.
5. **By caller** (one row per caller; a caller stored by list id, by platform-user id or by typed name is one row).
   Self vs caller removed (user, 2026-11-20).
6. **Why**: canceled reasons, rescheduled reasons, rejected reasons (count and share).
7. Depth and speed removed (user, 2026-11-20).
8. **What raises the pass rate** — breakdown tables: profile, region, job area, resume score, self vs bidder bid,
   weekday, time of day.
9. **Follow-ups**: rounds followed up vs not, and how each ended.

## Non-goals

Answer-score breakdown from transcripts (user, 2026-11-20: since Oct 6 only one person uploaded transcripts, none
after Nov 4, so the table would describe one person); drill-down to single interviews; trend charts; exporting; a drafted follow-up email; tech-stack breakdown (no data
yet); changing the Bids → Interviews tab.

## Definitions

- **Round in the period**: `scheduledAt` inside the period, on a profile in scope (region = the profile's current
  region; archived profiles included; owner = the interview's `createdBy`), on or after 2026-10-06. AI interviews
  and home assessments are left out everywhere, as in the rest of the app.
- **Outcome of a round** (exactly one):
  - `canceled` — status canceled;
  - `scheduled` — status scheduled (not held yet);
  - `passed` — status passed, or a later round exists in the same interview;
  - `rejected` — status rejected with a reason other than `no_answer`;
  - `no_answer` — status rejected with reason `no_answer`, or status completed, latest round, ended 14+ days ago;
  - `waiting` — status completed, latest round, ended less than 14 days ago.
- **Held** = passed + rejected + no_answer + waiting. **Decided** = passed + rejected + no_answer.
- **Pass rate** = passed ÷ decided × 100, one decimal. `few` when decided < 20: shown muted with ⚠, never named in
  findings, never sets the bar scale.
- **Offer** = a `final` round with outcome passed.
- **Companies interviewed** = interviews (one per company process on a profile) whose first HELD round is in the
  period. A company whose first round is only scheduled or was canceled is not counted, so this never exceeds
  rounds held (user, 2026-11-20; differs from the other tab's "interviews started", which counts a planned first
  round too). **Total rounds** = every round in the period, with the split held · canceled · scheduled (not held yet) under it;
  the Everyone list and the tables use **Rounds held**.
- **Caller round** = the round's caller is enabled and names a caller; otherwise self.
- **Reschedule rate** = rounds with at least one logged reschedule ÷ rounds in the period scheduled on or after
  2026-11-20 (the day logging started); a note says so.
- **Breakdown keys**: job area, resume score band and self/bidder come from the interview's `applications` row
  (`interviewId`) and fall back to `resumeLink` / `appliedBy`; missing → "Unknown" (never named or marked).
  Weekday and time of day (morning before 12, afternoon 12–17, evening after) in the app timezone.
- **Rank / median**: over team members with at least one round held in the period; pass rate ranked only with 20+
  decided rounds.

## Findings (backend, pure; max 4)

- **Best group per breakdown**: `few` false, pass rate ≥ 1.5 × team pass rate, and binomial upper-tail
  P(X ≥ passed | n = decided, p = team rate) ≤ 0.05 → ★ `{label} passes best for {by}: {rate}% ({p} of {d}
  decided rounds) vs {team}% overall.`
- **Weak group**: `few` false, pass rate ≤ 0.5 × team rate, binomial lower-tail ≤ 0.05 → ⚠ `{label}: {rate}% pass
  ({p} of {d} decided rounds) vs {team}% overall.`
- **Top reason**: one reason holds ≥ 40% of at least 10 rejected (or canceled) rounds → ⚠ `{share}% of rejected
  rounds: {reason} ({n} of {total}).` "No reason given" and "Other" are never named.
- **No answer**: no_answer ≥ 30% of at least 20 decided rounds → ⚠ `{share}% of held rounds got no answer ({n} of
  {d}) — follow up after a week.`
- None → `No clear pattern yet in this period — try a longer date range.`
- Groups get `mark: "best" | "weak" | null` from the first two rules, one of each per table.

## Approach

**Compute on read from `interviews` (chosen).** About 1,200 rounds: one cached load per (period, region), the same
single-flight cache pattern as `load_period_rows`; `GET /stats/interviews-report` returns `{window, totals,
summary: {cards, everyone}, findings, byStage, byCaller, reasons: {canceled, rescheduled, rejected},
byProfile, byRegion, byArea, byScore, byAppliedBy, byWeekday, byHour, followUps}`. Scope through
`app.stats.conversion_scope` (me / team, region, DATA_START).

Rejected:
- A synced `interview_rounds` collection like `applications` — needed there for 49,000 rows; here it only adds a
  sync that can go stale.
- Computing in the browser from `/interviews` — the evidence rules and outcome definitions belong in the tested
  backend, and the list endpoint hides other owners' rounds.

## API contract (`GET /stats/interviews-report`)

Query: the same parameters as `GET /stats/conversion` (`period`, `date`, `to`, `region`, `scope` = `team` | `me`,
`user_id` for admins). Response (all keys always present; lists may be empty):

```
Group = {key, label, rounds, held, decided, passed, rejected, noAnswer, waiting, canceled, scheduled,
         rate (number | null), few (bool), mark ("best" | "weak" | null),
         members?: Group[], memberCount?: number}          // members only on a folded "Other …" row
{
  window: {start, end, startClamped},
  totals: Group (key "all") + {companies, offers},
  summary: {
    cards: {
      companies: {value, rank, of, median, vsMedian},
      rounds:    {value, held, canceled, scheduled, rank, of, median, vsMedian},   // value = all rounds; rank on held
      passRate:  {value, passed, decided, waiting, rank, of, teamRate},
      offers:    {value, rank, of}
    },                                    // scope team: rank / of / vsMedian null, median = per-person median
    everyone: [{userId, name, companies, held, decided, passed, passRate, few, offers, isSubject,
                ranks: {companies, held, passRate, offers}}]
  },
  findings: [{kind: "best" | "weak" | "reason" | "no_answer" | "none", text}],
  byStage, byCaller, byProfile, byRegion, byArea, byScore, byAppliedBy, byWeekday, byHour: Group[],
  reasons: {
    canceled, rescheduled, rejected: [{key, label, count, share}],     // share = % of that list, one decimal;
                                                                       // key "none" / label "Reason not filled in yet" last
    rescheduleRate: {rescheduled, rounds, since: "2026-11-20"}
  },
  followUps: [{key: "followed_up" | "not_followed_up", label, rounds, passed, rejected, noAnswer, waiting}]
}
```

- `byStage` in stage order (intro … final), stages with no rounds left out. `byCaller` (only rounds with an assigned caller) and
  `byProfile`: rows with `few` are folded into one "Other callers" / "Other profiles" row with `members` (as
  "Other sites" on the other tab). `byWeekday` Mon–Sun without empty days; `byHour` Morning, Afternoon, Evening.
  `byScore` bands and labels as on the other tab, plus "No score". `byAppliedBy`: Self bid, Bidder bid, Unknown.
- `followUps`: `followed_up` = held rounds in the period with at least one follow-up. `not_followed_up` = held
  rounds with no follow-up that stayed without a result for 7 days after they ended (still `completed` and ended
  7+ days ago, or `statusAt` 7+ days after the end; a round with no `statusAt` counts only while still `completed`).

## App

`InterviewStatsTab` in Statistics (tab key `interviews`; the old `?tab=interviews` redirect to the conversion tab is
removed). Reuse `ConversionTable` (rate bars, ★ / ⚠, few rows; column heads become Group · Rounds · Decided ·
Passed · Pass rate), the cards, Everyone list and findings components — generalise their labels by props instead of
copying. Reasons tables are count + share bars. Dark mode, phone width.

## Builder's calls (not the user's words)

- `few` at 20 decided rounds (about 300 held rounds exist since Oct 6; 30 would mute most tables).
- "Waiting" rounds are outside the pass rate.
- Reschedule rate only from 2026-11-20.

## Constraints

No runs against the real database in tests; single-line commits without trailers; TDD; backend full suite with the
usual ignores (2 known failures); app `npx vitest run`, `npx tsc --noEmit`. Built after the follow-up design.
