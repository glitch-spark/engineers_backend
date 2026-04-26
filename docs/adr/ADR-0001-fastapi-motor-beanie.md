# ADR-0001: FastAPI + Motor + Beanie for the new backend

- Status: Accepted
- Date: 2026-04-25

## Context

`engineersapp/` is a Next.js 14 monolith whose `app/api/*` routes are backed by Mongoose against a live MongoDB Atlas cluster. The split carves the API out into a separate FastAPI service in `engineers_backend/`, while the SPA stays in `engineersapp/`.

The existing data — `userextras`, `transactions`, `accounts`, `cardlinks`, `weeklyplans` — already lives in Atlas, and includes bcryptjs password hashes that production users authenticate against today. We considered migrating to Postgres + SQLAlchemy as part of the rewrite, but the cost would be a full data migration (with a writer-shim window) on top of an already substantial Next.js → SPA + FastAPI split.

## Decision

Keep MongoDB as the system of record. Use **Motor** (async MongoDB driver) with **Beanie** (Pydantic-based ODM) inside FastAPI. Document classes mirror the existing Mongoose schemas one-to-one, with `Settings.name` overrides to keep the existing collection names addressable (see ADR-0003). The new service points at the same Atlas cluster the Next app uses today; no data migration runs as part of the split.

## Consequences

**Positive**
- Zero data migration; cutover is a config change, not a multi-day backfill.
- bcryptjs hashes verify under `passlib`'s bcrypt scheme unchanged, so existing user passwords keep working.
- Mongoose schemas → Beanie `Document`s is a near-mechanical port (field types, enums, indexes), keeping the diff reviewable.
- Async Motor pairs cleanly with FastAPI's async stack.

**Negative**
- We forgo the relational guarantees SQLAlchemy + Postgres would have given us (foreign keys, joins, cross-collection transactions). Per CLAUDE.md, transactions are no longer linked to accounts, so the relational pull is weaker than it might first appear, but it remains a real trade-off.
- Beanie's ecosystem is smaller than SQLAlchemy's; advanced query patterns may need to drop to raw Motor.
- We inherit Mongoose's historical naming choices instead of designing fresh tables.

**Reversible if** the data model later wants relational guarantees we are working around — a Postgres migration becomes its own planned project rather than something bundled into this split.
