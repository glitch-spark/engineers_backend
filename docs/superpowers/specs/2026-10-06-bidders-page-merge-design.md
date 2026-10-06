# One Bidders page: team week view, bidder view, review and history together

Date: 2026-10-06
Repos: `engineers_backend`, `engineers_app` (branch `feat/bid-review-ux`, worktrees `.worktrees/bid-review-ux`)
Builds on: `2026-10-05-bid-records-review-design.md`, `2026-10-06-bid-review-ux-design.md` (focus viewer, evidence,
reject reasons, approve all complete)
Status: design approved in conversation, pending spec review

## 1. Goal

Owners and admins manage bidders, review their bids and see their pay in **one place**, organised like the
Plans/Report page: a **team view** of all bidders for a pay week, and a **bidder view** for one bidder with the
week's days, their bids (click a bid to review it) and their history. The separate Bid review page and the history
side panel go away.

Also from user feedback:
- Clicking a bid row starts its review.
- The plain-text daily summary ("Window for … Total · Approved · Pending · Rejected") becomes summary tiles.
- "No submit screenshot" reads like "no screenshot" when a screenshot exists — reword.

### Out of scope

Changes to counting rules, Slack reports or the extension; analytics beyond the tiles below; bidder-facing views.

## 2. Weeks and days

- A **pay week** is the weekly report window (ends at `BID_WEEKLY_REPORT_DAY` `BID_WEEKLY_REPORT_TIME`, default
  Saturday 16:00, `BID_REPORT_TIMEZONE`). It is identified by any date inside it (`?week=YYYY-MM-DD`); the label is
  "Week ending Sat Oct 10".
- Its **days** are the seven daily report windows (18:30 → 18:30 by default) whose **start** falls inside the pay
  week, keyed by their period key (Sun … Sat with the defaults; Saturday's window ends after the weekly cutoff).
- The day that straddles the weekly cutoff (Saturday with the defaults) shows its full daily count; the **week totals
  and pay** use the pay week's own window, so they match the weekly Slack report. That day's cell carries an ⓘ
  explaining the difference.
- Days before `BID_RECORDS_START` (per the existing rule: a window uses records only if it starts on/after it) show
  Backblaze folder counts, marked "folder count, not reviewed".

## 3. Backend

### `GET /bids/week?week=YYYY-MM-DD` (new)

Access: same as `/bids` (owners see their bidders, admins all); archived bidders included only with
`includeArchived=true`. `week` defaults to the current pay week; invalid → 400 `invalid week`.

Response:
```
{ week: {start, end, label, timezone, days: [{day, start, end, isToday, isFuture}]},
  bidders: [{ id, name, status, country, profileName, rate,
              days: [{day, approved, pending, rejected, source: "records"|"folders", error}],
              week: {approved, pending, rejected, pay, toReview, source} }],
  totals: {approved, pending, rejected, pay, toReview, days: [{day, approved, pending, rejected}]} }
```
- `status`: `active` | `invited` | `archived` (same rule as the Bidders table today).
- Record days: one aggregation over `bids` grouped by bidder × daily window × status. Folder days: the existing
  cached folder counts (`count_folder_windows`); a read error gives `error` and null counts for that bidder/day.
- `week.pay = round(week.approved × rate, 2)` from the pay-week window; `toReview` = pending in the pay week.
- Future days: zeros with `isFuture: true`.

### `GET /bids` — `week` filter

`GET /bids?week=YYYY-MM-DD&bidderId=…&status=…` returns the bids whose `firstAt` falls in the pay week (same
`BidOut` with evidence). `day` and `week` are mutually exclusive (both → 400). Used by the bidder view and by the
focus viewer for a week-wide pending queue.

### History

The History tab reuses `GET /bidders/{id}/reports?kind=weekly` (stored weekly rows); no new endpoint.

## 4. Frontend: one page at `/bidders`

URL state like Plans: `?week=` (pay week), `?bidder=` (bidder view), `?day=` (expanded day in the bidder view),
`?tab=history`, `?mode=focus` (focus viewer, plus `bid=<id>` to open at a bid). `/bids/review?…` redirects to the
matching `/bidders?…` (bidder → `bidder`, day → `day` + its pay week).

### Header and week navigator (both views)

`PageHeader` "Bidders" with **Add bidder** (team view) and the "Show archived" switch; week navigator: prev / next,
"This week", label "Week ending Sat Oct 10 · pay week".

### Team view (no `bidder`)

- **Tiles**: Approved this week (with pay), To review (pending, with "Review all"), Rejected, Today (approved +
  pending badge, only when the shown week is current).
- **Table** (`.table-wrap`): Name, status, profile, rate, **Sun … Sat** (approved count + amber pending badge;
  "–" future, folder days greyed with a tooltip), Week, Pay, To review, ⋯ menu (Edit, New invite code, Reset login,
  Archive / History for archived). A **Total** row at the bottom.
- Clicks: name/row → bidder view for the week; day cell → bidder view with that day expanded; To review → focus
  viewer on that bidder's pending bids of the week; "Review all" → focus viewer on everyone's pending bids of the week.

### Bidder view (`bidder` set)

- Header: "← Team", name, status, profile (with resume names), rate; Edit and ⋯ menu.
- **Tiles**: Approved (with pay), To review (with **Review pending** → focus viewer on the bidder's pending bids of
  the week, oldest first), Rejected (with the most common reason), Today (if in the week).
- **Tabs: This week | History**
  - *This week*: one row per day (date, window, Approved / Pending / Rejected chips; today highlighted; amber dot when
    pending). Clicking a day expands its bids in place. Bid row: time, job title (link), job site, screenshots,
    **Submitted** / **Not marked submitted** badge, Missing upload, status + reason, quick Approve / Reject. **Clicking
    a bid row opens the focus viewer at that bid** (decided bids too); J/K then move through that day's bids. An
    expanded day shows "Approve all complete (N)" for that day.
  - *History*: past pay weeks newest first (week ending, approved, pending, rejected, rate, pay); folder weeks
    labelled "not reviewed"; clicking a week switches to it.

### Focus viewer changes

`FocusReview` gains scope props: `{week | day, bidderId?, startAt?: bidId}`. When opened at a bid it starts there
(any status); its navigation set is that scope's bids (all statuses when opened from a bid row, pending when opened
from a Review button). Everything else (keys, undo, stale check, evidence) is unchanged.

### Wording

"No submit screenshot" → **Not marked submitted** (tooltip: "No screenshot was taken with Submit screenshot — the
application may not have been submitted"); a bid with a Submit step shows **Submitted** (green).

### Removed

The separate `/bids/review` page (redirect), the "Review bids" header link, and `BidderHistoryPanel` (replaced by
the History tab). `BidderFormModal`, `InviteDialog`, reset/archive dialogs are reused unchanged.

## 5. Edge cases

- Current week: future days "–"; Today tile only for the current week.
- Saturday straddle: §2.
- Folder days / storage errors: counts null with a warning icon, as today.
- No bidders: empty state with Add bidder.
- Archived bidders: only with Show archived; their bidder view is read-only for management actions but reviewable.

## 6. Testing

- Backend unit tests (mongomock): `/bids/week` per-day status counts, totals, pay from the pay-week window,
  `toReview`, future days, folder days before `BID_RECORDS_START` (fake storage), straddling day vs week total,
  access (owner / admin / other), `includeArchived`, invalid week; `/bids` `week` filter and `day`+`week` → 400.
- Frontend: `tsc -b` and a Vite build.
- Browser sandbox (`scratchpad/e2e`): team view → bidder → expand a day → click a bid → focus viewer at that bid →
  approve / reject → counts update in the bidder view and team view; History tab; `/bids/review` redirect.
