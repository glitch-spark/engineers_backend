# ADR-0003: Preserve Mongoose collection names verbatim

- Status: Accepted
- Date: 2026-04-25

## Context

Mongoose pluralizes and lowercases model names to derive collection names. The existing data was written under those auto-generated names: `userextras` (from `UserExtra`), `cardlinks` (from `CardLink`), `weeklyplans` (from `WeeklyPlan`), plus `transactions` and `accounts`.

Beanie defaults to using the `Document` class name as the collection name, which would produce different names (`UserExtra`, `CardLink`, `WeeklyPlan`) and silently address empty collections — a particularly nasty failure mode because the API would boot, requests would succeed, and reads would just return nothing. Per CLAUDE.md: "users live in the `userextras` collection ... The collection is named `userextras` because the project predates and coexists with whatever `User` collection NextAuth might create." Renaming is not free — it requires a data migration in production.

Three options were on the table:
1. Rename collections to something conventional (`users`, `card_links`, `weekly_plans`) with a one-shot data migration.
2. Keep Mongoose collection names but use Pythonic class names in code, bridged via `Settings.name` overrides.
3. Keep Mongoose names everywhere — class names and collection names — for maximum literal correspondence.

## Decision

Pick **option (2)**: set `Settings.name = "userextras"` (etc.) on each Beanie `Document`, while class names stay Pythonic (`UserExtra`, `CardLink`, `WeeklyPlan`). The same principle applies to inherited field names — including the `cenceled` typo on `CardLink`, which is preserved for data compatibility and tracked as a follow-up rename, not fixed silently here.

## Consequences

**Positive**
- Zero data migration; the new service reads and writes the same collections the Next app does today.
- Cutover and rollback are both config changes; the two backends can run side-by-side against the same data during verification.
- Removes a whole class of "we deployed the API but it shows no users" failure modes during the split.

**Negative**
- Collection names look slightly off in MongoDB Compass and in any future ad-hoc queries (`userextras` instead of `users`).
- The `cenceled` typo lives on in the schema until a deliberate rename + migration; new code touching that field needs a one-line comment so reviewers don't "fix" it in passing.

**Reversible if** the team later wants conventional names — the rename becomes a planned, separate migration with a writer-shim window, not something bundled into the split.
