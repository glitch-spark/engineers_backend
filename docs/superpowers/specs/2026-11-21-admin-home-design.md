# Admin Home (one person at a time) and Pending items — design

Date: 2026-11-21. Approved by the user from the prototypes in
`engineers_app/.superpowers/brainstorm/581247-1795206047/content/`: `admin-home-v4.html` (the person's page) and
`person-selector-options.html` option **A** (the selector). They are binding for layout and wording; their numbers
and names are samples. Builds on 2026-11-20-home-redesign-design.md.

## Why

An admin's Home shows the admin's own (often empty) cards and a totals table at the bottom. The admin wants to see,
for each person, their bids, interviews, daily to-dos across the week, weekly goals and what is waiting on them —
and the red flags on their profiles and bidders. People should see the same pending items on their own Home.

## Admin Home

- A switch **Me | Team** at the top, for admins only. An admin with no plan of their own lands on Team; otherwise the
  last choice is remembered (per browser), default Team.
- **Team** = a list of people down the left (always visible), the picked person's page on the right. On a narrow
  screen the list becomes a "Viewing" dropdown with previous / next arrows.
- **List row**: status dot, name, pending count badge, `{bids} / {target} bids · {x} of {y} to-dos`, and a bids bar.
  Dot: red = an interview ended and was not marked (profile and bidder flags only add to the pending count: on
  real data every person has one, so they would paint the whole list red), amber = behind on bids or
  to-dos for the day, green = otherwise. Order: red first, then amber, then green; inside a colour most pending
  first, then name. The first person is picked on arrival; the choice sits in the URL (`?user=`).
- **Today | Yesterday** switch for the page's day (bids table, today's interviews, the to-dos summary).
- **The person's page**, in this order (two columns on a wide screen: Bids + Interviews left, Daily to-dos + Weekly
  goals right; Pending items full width below):
  1. **Bids** — `{done} of {target} today · {n} to go`; a table Region (US, EU, Latam; only regions the person has)
     × Self (`{done} / {target}` when a target exists), Bidder, Total, and an All row; numbers under their target in
     amber. Then `This week · {total} of {target}` with Mon–Fri as on the Home week card.
  2. **Interviews** — `{n} today · {u} not marked · {p} in pipeline`; **Today**: the day's rounds (time, company,
     stage, who takes it, status) as on the Home Today card, without buttons; **In pipeline ({p})**: a table
     Company · Stage · Next / last round · Status.
  3. **Daily to-dos** — `{d} of {e} this week · {x} of {y} today`; the routines grid (Mon–Fri marks, `{d} of {n}
     days`, legend) exactly as the Home week card's.
  4. **Weekly goals** — `{d} of {n} done` with the "last day" / "{n} days left" note; one row per goal: done with
     its day, `not yet`, or `{value} / {target}` with a bar.
  5. **Pending items** — see below; heading `Pending items ({n})`, sub-line `Things waiting on {name}`.
- Read-only for the admin, except the interview result buttons in Pending items (as an admin can already do on the
  Interviews page). "I followed up" stays with the owner: no checkbox for the admin.
- The old "Team overview" table stays, below, on the Me view.

## Pending items (admin's person page AND every person's own Home)

On a person's own Home it replaces the "Follow-ups" block of the Today card (same place), with the checkbox on
follow-up rows as today. Three groups, each left out when empty; the heading count is the sum.

- **Interviews** — the `needs-input` rows, each with a tag and the buttons it has today:
  `Status unknown` (need `outcome`: ended, never marked), `Follow-up due` (`follow_up`), `Heard back?`
  (`heard_back`), `No reason` (`reason`). Today's unmarked rounds are included here on the admin page; on a
  person's own Home they stay in the Today list above and are not repeated.
- **Profiles** — the two existing Profiles-page banners (`app/profile_alerts.py`), per profile the person owns:
  `No interviews` (no interview in the last 2 weeks): `{name} · no interview for {n} days · {bids} bids in that
  time` and `, resume score {s} on average` when known; link "What to change" (Statistics → Job applies for that
  person). `No bids` (no bids, self or bidder, in 3 weeks): `{name} · no bids, self or bidder, for {n} days ·
  archive it?`; link to the profile.
- **Bidders** — per active bidder of the person (not archived, registered):
  `Not bidding`: at least one bid in the 14 days before, none in the last 2 working days →
  `{name} · no bids for {n} working days`. `Slowing down`: average of the last 3 working days under half of the
  average of the 10 working days before them, and that earlier average at least 10 a day →
  `{name} · {a}, {b} and {c} bids the last 3 days · {avg} a day before`. Link to the bidder.
  Working days = Mon–Fri; today is not counted until it is over.

## API contract (field names binding; all additions)

- `GET /dashboard/team-today?day=YYYY-MM-DD` (admin only) →
  `{day, people: [{userId, name, bids: {done, target: number | null}, todos: {done, total}, pending: number,
  state: "red" | "amber" | "green"}]}` in the order above (active non-admin members).
- `GET /daily-plans/{day}` adds `bidsByRegion: [{region: "US" | "EU" | "Latam", self, selfTarget: number | null,
  bidder, total}]` (only regions with a profile, a target or a bid) and `bidsTotals: {self, selfTarget, bidder,
  bidderTarget, total, target}` (targets null when unset). Bidder bids by region = by the region of the profile the
  bidder bids for.
- `GET /interviews/pipeline?userId=` (own; admins may pass `userId`) → `{items: [{interviewId, companyName,
  profileName, stage, at: ISO, upcoming: bool, status: "scheduled" | "waiting" | "no_result" | "followed_up" |
  "passed", days: number | null, followedUpAt: ISO | null}]}`: interviews still open — the latest round is
  `scheduled` (status scheduled, `upcoming` true when its start is ahead), `completed` (`waiting` under 7 days after
  it ended, `no_result` from 7 days with `days`, `followed_up` once a follow-up exists) or `passed` with no later
  round. Rejected, canceled and closed-as-no-answer interviews are out. Soonest upcoming first, then most recent.
  AI interviews / home assessments skipped as elsewhere.
- `GET /dashboard/pending?userId=` (own; admins may pass `userId`) → `{interviews: [needs-input item…],
  profiles: [{accountId, name, kind: "no_interviews" | "no_bids", idleDays, capped: bool, bids: number | null,
  tailorScore: number | null}], bidders: [{bidderId, name, kind: "not_bidding" | "slowing", days: number | null,
  recent: number[] | null, before: number | null}], counts: {interviews, profiles, bidders, total}}`.
  `team-today`'s `pending` equals `counts.total` for that person.
- The existing reads already take `userId` for admins (`/daily-plans/{day}`, `/dashboard/week`,
  `/dashboard/month-interviews`, `/interviews/needs-input`); the person page uses them.

## Approaches

1. **One list read for the team, the existing per-person reads for the page, two small new reads (chosen).** The
   page reuses what Home already loads for one person; only the summary list, the pipeline and the pending items
   are new.
2. One endpoint returning everything for everyone — a single request, but six people's full pages on every load
   and every tick.
3. Cards for everyone on one page — shown in an earlier prototype; the user chose one person at a time.

## Builder's calls (not the user's words)

- The two bidder flags and their thresholds (there is no per-bidder target to measure against).
- The dot rules and the list order.
- Admins cannot tick a person's to-dos or follow-ups.

## Constraints

No runs against the real database; mongomock tests; single-line commits without trailers; TDD; backend full suite
with the usual ignores (2 known failures); app `npx vitest run`, `npx tsc --noEmit`. Dark mode; phone width.
