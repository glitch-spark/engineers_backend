# Admin weekly planning: a team grid, a profile log item, and a per-person handover

Date: 2026-11-21. Status: draft for approval. Repos: `engineers_backend` (model, API, Slack) and `engineers_app`
(Plans page). Builds on `2026-11-17-plans-routines-and-standup-design.md` (weekly plan = targets + routines + weekly
goals; the daily standup follows it).

Prototype (clickable, sample data): `engineers_app/.superpowers/brainstorm/1220572-1795296821/content/plan-setup-v3.html`.

## Why

Today each person writes their own week and the standup follows whatever they wrote. The admin can read anyone's
week but cannot change it (`require_writer` rejects admins), and a week is edited one person at a time in a side
panel. The admin wants to decide each person's main goals for the week (bids, interviews, email outreach, profile
review, company work, interview review, manage bidders, and whatever else comes up that week) and have people
report their standup against exactly that. This is a starting phase: the admin publishes plans at first, and later
people plan their own weeks again.

## Goals

1. The admin sets every person's week on one screen: a grid, one row per person, one column per item.
2. Every week is its own plan. Any item can be added, renamed, re-typed, re-targeted or removed that week. No fixed
   item list.
3. A new week starts as a copy of the last one; the admin edits and publishes.
4. A new item type, **log**, for Profile review: each day the person picks a profile and writes what changed, as
   many entries as needed.
5. A per-person switch, **Plans own week**. Off: the admin plans, the person's week is view-only. On: the person
   edits their own week as today; the admin can still edit it from the grid.
6. The daily standup (app and Slack form) and the Slack post follow the published plan, including log entries.

## Non-goals

- No shared item catalog or role templates. Each person's `WeeklyPlan` stays the only home of their plan.
- No per-item "set by admin" lock. Control is per person (the switch), not per item.
- No change to how bids and interviews are counted, to derived daily bid targets (week ÷ 5), to frozen targets on
  submitted days, or to the 6 PM nudge.
- No notification when a week is published (no DMs, as before).
- Admins still have no plan of their own.

## Anti-goals (failure even if it works)

- A person with the switch off can still change their week through the API.
- Publishing the grid twice (double click, retry) creates duplicate plans, duplicate items or new item ids for
  items that already had results.
- Publishing wipes something the grid does not show: `recapNotes`, archived items that hold results, a person's
  one-off items.
- A day already submitted changes its numbers because the admin edited the week afterwards.
- A log item left empty disappears from the standup post instead of showing as not done.

## Constraints

- Local development talks to the production database. No destructive migration; new fields must read fine on
  existing documents.
- Slack modals: at most 100 blocks, no rows added on the fly.
- `MAX_ITEMS` (20) per list and `MAX_ITEM_LENGTH` (200) stay.
- Items are never deleted, only archived (`_merge_items`), so past days keep their results.

## Approaches considered

- **A. Extend the existing weekly plans (picked by the user).** Admin write access, one bulk save for the grid, a
  `log` kind, a per-person switch. Grid columns are derived by matching item names across people. No migration, and
  it is already the end state (people owning their plans).
- B. Shared item catalog with per-person assignments. Rename once, cleaner columns, but a new collection, a
  migration, two homes for plan data, and dead weight once people plan their own weeks.
- C. No grid; the admin opens each person's Week panel. Smallest, but slow every week and no comparison across
  people.

## Design

### 1. Data

- `PlanItem.kind` gains `"log"`. `target` is ignored for it, like `check`.
- `ItemResult` gains `entries: list[LogEntry] = []`, `LogEntry = {profileId: Optional[str], note: str (1..200)}`,
  at most 20 per result. `profileId` is one of the person's own profiles (`plan_profiles.owner_profiles`); `None`
  for entries typed in Slack (see 4). A log result is done when it has at least one entry.
- `UserExtra.plansOwnWeek: bool = False`. False = admin-planned.
- No stored "published" state. A week with a saved `WeeklyPlan` is published; a week without one reads as the
  carry-over draft, as today.

### 2. Carry-over

`plan_auto._draft` also carries last week's live weekly goals of kind `count` and `hours` (recurring quotas such as
"Email outreach 250"). `check` and `log` weekly goals are one-offs and are not carried. Targets and live routines
carry as today. Carried items keep their ids.

### 3. API

- `GET /weekly-plans/team/{week_start}` (admin only): for every active non-admin user, `{userId, name,
  plansOwnWeek, exists, goal, routines, weeklyGoals}` using the saved plan, else the carry-over draft, else empty.
  Live items only. The grid calls it for the shown week and for the week before (to mark what changed).
- `PUT /weekly-plans/team/{week_start}` (admin only): body `{plans: [{userId, plansOwnWeek, goal, routines,
  weeklyGoals}]}`. For each row: upsert that user's `WeeklyPlan` by `(userId, weekStart)` through the same code as
  `put_week` (`_merge_items` against the saved plan or draft, so ids are kept and dropped items are archived);
  `recapNotes` and `goal.interviewsCaller` are left as stored (the grid does not show them); `plansOwnWeek` is
  written to the user when it differs. Rows are independent: the response lists per-user `ok` / error and the whole
  call is safe to repeat. Unknown or admin `userId` → that row fails with a clear message. A row with no targets
  and no items for a person without a saved plan is skipped (no empty plan is created).
- The app sends only rows that were edited or that have no saved plan yet. Untouched saved rows are not re-sent,
  so publishing never overwrites a plan the admin did not change. For edited rows the last write wins; the grid
  re-reads the week after a save and on window focus.
- `PUT /weekly-plans/{week_start}` (the person's own) when `plansOwnWeek` is false: only `recapNotes` is applied,
  and only when the week already has a saved plan (else 403 "Your week is planned by your admin"); targets and
  items in the body are ignored. `DELETE` → 403. `GET` adds `plansOwnWeek` so the app can show the week view-only
  with the recap still editable.
- Changing an item's type, or moving it between daily and weekly, is sent as remove + add: the old item is
  archived (days with results keep it, with its old type) and a new item with a new id replaces it. Rename and
  re-target keep the id.
- The Monday "add this week's plan" alert (`_tick_weekly_plans`) skips people with `plansOwnWeek` false.
- Day reads (`GET /daily-plans...`) return each item's `entries`; day saves (`DayIn` / `ProgressIn`) accept
  `entries` per result. Entries with a `profileId` that is not the person's profile → 422.
- `require_writer` stays for everything else (admins cannot submit a day or write a plan of their own).

### 4. Standup: app, Slack form, Slack post

- App day panel: a log item shows its entries as rows (profile select + "what changed" text + remove) and
  "+ add profile". Other kinds unchanged.
- Slack form: a log item is one optional multiline text input, hint "one line per profile: Profile name: what
  changed". Each non-empty line becomes an entry with `profileId = None` and the line as `note`. When the form
  opens on a day that already has entries, it is pre-filled with one line per entry (`<profile name>: <note>` for
  entries with a profile); saving from Slack replaces the entries with the lines.
- Slack post (`slack_bot.daily_plan_text`): under Routines / Weekly goals, a log item prints
  `Profile review   ✅ 2 profiles` followed by one `• <profile name>: <note>` line per entry; with no entries it
  prints as not done (✗), not omitted.
- `plan_save.result_done`, `plan_auto._routine_done`, `items_tally`, `routines_grid` and `goal_progress` treat a
  log result as done when it has entries.

### 5. App: Plans page

- New admin view `/plans?view=setup&week=<monday>`, reachable from the admin's team table ("Plan the week").
  Layout as in the prototype:
  - Week stepper with a status chip: **Published** (every person whose row is not empty has a saved plan),
    **Draft** (some rows are carry-over drafts or have unsaved edits), **Not planned yet** (nobody has a plan or a
    draft).
  - Grid: person column; **Week targets** (Bids self, Bidder bids, Interviews = `interviewsSelf`; the caller
    interview target is not in the grid and is edited in the person's Week panel); **Daily routines**; **Weekly
    goals**; **Only this person**; "+ item". A final "Fill all" row sets a whole column.
  - Columns are derived: items with the same name (case and whitespace insensitive), kind and scope across two or
    more people form one column; a name only one person has shows as a chip under "Only this person". A column just
    added in the screen stays a column until saved even if only one cell is filled.
  - Cells: number for count / hours targets, checkbox for check / log ("has this item"). An empty number or an
    unticked box means the person does not have the item.
  - Column header menu: rename, change type, switch daily ↔ weekly, remove from this week. Each applies to every
    person who has the item, in the one save.
  - "+ item": name, type, daily or weekly, for everyone or one person.
  - Cells that differ from last week are highlighted with "was N"; columns new this week are highlighted.
  - A "Plans own week" toggle per row.
  - **Publish week** saves every row (including untouched carry-over drafts, so the week becomes Published) and
    shows per-person failures inline; rows that saved are not re-sent on retry unless edited.
  - Bids self is one number in the grid; the US / EU / LATAM split stays editable in the person's Week panel.
    Editing the total in the grid clears a stored split for that person (the total is what the admin set).
- Week panel (person): targets and items view-only with the note "Planned by your admin" when `plansOwnWeek` is
  false, recap notes still editable once the week is published; unchanged when true. The admin opening a person's Week panel can edit it (same fields as one grid row, plus the region
  split), saving through the team endpoint with one row.
- `PlanItemsEditor` gets the `Log` kind option.

### 6. Behaviour rules kept from the earlier design

- Editing a published week applies from the next unsubmitted day; submitted days keep their frozen targets.
- Removing an item archives it; days with results keep showing it.
- A week nobody touches reads as last week's carry-over (targets, routines, recurring weekly goals), so the standup
  is never empty.

### 7. Addendum (2026-11-21, user decision): people add their own items under an admin plan

The user decided that a person whose week the admin plans (`plansOwnWeek` false) can still add items of their own;
the admin's targets and items stay locked for them. This replaces designer call #2 below.

- `PlanItem.addedBy: Literal["admin", "self"] = "admin"`. Missing on old documents reads as `"admin"`, so items
  that exist today become locked once the person is admin-planned. Carry-over keeps the field.
- Team save (`PUT /weekly-plans/team/{week}`): an item with a known id keeps its stored `addedBy`; a new item is
  `"admin"`. The admin can still rename, re-target or remove any item, including a person's own.
- Person's save (`PUT /weekly-plans/{week}`) when `plansOwnWeek` is false and the week has a saved plan: targets
  and `"admin"` items are taken as stored, whatever the body says (an admin item missing from the body is not
  archived); `"self"` items follow the body (new ones created as `"self"`, missing ones archived, kind change =
  remove + add); recap as before. A week without a saved plan stays 403 "Your week is planned by your admin".
  The 20-item limit counts both kinds together. When `plansOwnWeek` is true, nothing changes: the person edits
  everything, new items are `"self"`.
- Week panel, own mode, admin-planned: targets and admin items read-only with a lock and the "Planned by your
  admin" note; the person's items editable in the same lists with "+ Add item" (new rows are theirs); Save sends
  the whole week as today. Before the week is published: view only, as today.
- Setup grid: a person's own items show in "Only this person" with "added by <name>"; they never form a shared
  column with the admin's items of the same name (the column key includes `addedBy`). Publish keeps them (ids
  sent back unchanged).
- Daily standup, Slack post and Home: no change — a person's items are routines / weekly goals like any other.

## Calls made by the designer (not decided by the user; please check)

1. `plansOwnWeek` defaults to **false**: on release everyone becomes admin-planned and their Week panel turns
   view-only until the admin flips the switch. (The user said they will publish plans at first.)
2. ~~While admin-planned, a person cannot add items of their own.~~ Replaced by the user's decision in Design §7.
3. Recurring weekly goals (count / hours) now carry to the next week; check / log weekly goals do not.
4. Slack log entries are free-text lines without a profile link; the app has the structured picker.
5. Grid columns are matched by name, not by a stored shared id.
6. Editing the Bids self total in the grid clears that person's region split.
7. Log entries pick from the person's own profiles only (not bidders, not other people's profiles).
8. The grid's Interviews column is the person's own interviews; the caller interview target stays as stored.
9. Changing an item's type or daily ↔ weekly mid-week starts a new item (progress restarts); the old one stays on
   past days.
10. Publish does not re-send rows the admin did not edit; on edited rows the last write wins (no conflict check).
11. Admin-planned people can still write their recap notes, and no longer get the Monday "add your plan" alert.

## Builder checks

- Readers of `PlanItem.kind` / `ItemResult` that assume three kinds: `plan_save`, `plan_auto`, `standup/modal.py`,
  `slack_bot.daily_plan_text`, `routers/{daily_plans, weekly_plans, dashboard, reports}`, app `report/*`,
  `api/endpoints.ts`, Home to-do (`2026-11-18-home-todo-design.md`).
- `_keep_unshown` (Slack form) must keep log entries of items the form did not show.
- `self_bids` write-through and `save_progress` must not drop `entries`.

## Left to the builder

- Endpoint field names beyond those above, module layout, grid component structure, keyboard handling in the grid.
- Logging on the team save (admin id, week, rows saved / failed, latency).
- Tests: team read (saved / draft / empty), team save (idempotent repeat, ids kept, archive on removal, recapNotes
  kept, admin-only, per-row failure), the 403 for admin-planned people, carry-over of recurring weekly goals, log
  results (done rule, entries validation, tally), Slack form round trip for a log item, post layout with and
  without entries, grid column derivation and "Fill all" in the app.
