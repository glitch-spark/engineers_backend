# Bidder extension login — invite codes, bidder accounts, backend-signed uploads

Date: 2026-10-03
Branches: `feat/bidder-login` in `engineers_backend` (from `feat/bid-b2`) and `engineers_app`; the Bid Track
extension (`Engineer/Bid Track (BackBlaze)`, put under git first)
Builds on: `2026-10-02-bidder-management-design.md` (Bidders, reports) and the `feat/bid-b2` counting change
Status: approved design, pending spec review

## 1. Goal

A bidder goes from installing the Bid Track extension to correctly counted bids without ever seeing a
Backblaze key, bucket or folder:

1. An owner adds a bidder in the app. The bidder's storage folder is assigned automatically and the app shows
   an invite code.
2. The owner sends the code. The bidder installs the extension, registers with the code, a username and a
   password, and stays logged in.
3. Every screenshot the bidder captures is uploaded to `<bidder folder>/<date>/<profile>/<job>/…` in
   Backblaze. The backend decides the path, so a bidder can only add bids to their own folder.

Counting and Slack reports are unchanged: they keep listing each bidder's folder (approach 1 below).

### Decisions already made

| Topic | Decision |
|---|---|
| Upload trust | **Guaranteed.** Bidders never hold a storage key. The backend signs each upload for a path it builds itself. |
| Path | `<folder>/<YYYY-MM-DD>/<profile>/<job>/step<n>_<trigger>_<YYYY-MM-DD>_<HHMMSS>.png`. `folder` = the bidder (from their name in the app), `profile` = the bidder's profile name in the app. Same layout the extension writes today. |
| Folder | Assigned once at creation from the bidder name; unique; never changes on rename. |
| Invites | Copy-and-send code, single use, valid 7 days, regenerable. **Reset login** clears the account and issues a new code. No email. |
| Counting | Unchanged: list the bidder's folder, one bid per job folder (approach 1). Recording uploads in the database (approach 2) is a possible follow-up. |
| Bidder login | Extension only. Bidders do not log in to the web app. |

### Out of scope

Email invites or self-service password reset, bidder access to the web app, counting from upload records,
several profiles per bidder, un-archiving, publishing the extension to the Chrome Web Store, a persistent (multi-process)
rate limiter.

## 2. Prerequisite: counting fixes (`feat/bid-b2`)

Shipped before this feature, on the existing branch:

- Skip **any** file whose name starts with `_` when counting (today only `_bid_track_connection_test_*`).
  Real screenshots are always `step<n>_…`; the extension's test uploads (`_bid_track_connection_test_*`,
  `_aws4_test_*`, `_node_test_*`) all start with `_`. Validation on 2026-10-03 found two such files directly in
  `puma/`, counted as one extra bid.
- Slack copy: `1 bidder` / `N bidders` in the daily footer (today always `bidders`).

## 3. Data model — `Bidder` (`app/models/bidder.py`)

New fields:

| Field | Type | Notes |
|---|---|---|
| `folder` | `Optional[str]` | Lower-case slug, unique across all bidders (one shared bucket). Set at creation; never changed. Optional only for legacy rows until the backfill (§3.2) runs. |
| `inviteCodeHash` | `Optional[str]` | SHA-256 hex of the normalized code. `None` when no code is outstanding. |
| `inviteExpiresAt` | `Optional[datetime]` | UTC. |
| `username` | `Optional[str]` | Stored lower-case. Unique. `None` until registered. |
| `passwordHash` | `Optional[str]` | bcrypt via `app.security.hash_password`. |
| `registeredAt` | `Optional[datetime]` | |
| `tokenVersion` | `int = 0` | Incremented on Reset login and on archive; tokens carrying an older version are refused. |

Indexes (new): unique sparse `folder`, unique sparse `username`, unique sparse `inviteCodeHash`.

`screenshotFolderUrl` stays (counting reads it). For new bidders it is set automatically to
`s3://<BID_STORAGE_BUCKET>/<folder>/` and is no longer accepted from the client.

### 3.1 Folder slug

`folder_slug(name)`: NFKD-normalize, drop accents, lower-case, replace every run of characters outside
`[a-z0-9]` with `-`, trim `-`, cut to 40 characters (trim a trailing `-` again), empty → `bidder`.
If taken by any bidder (archived included), append `-2`, `-3`, … until free. The top-level prefix of every
bidder URL in `BID_STORAGE_BUCKET` also counts as taken (no `bids` while legacy `bids/ana/` exists). A
unique-index race on insert retries with the next suffix.

The profile path segment uses the same slug function on the bidder's current profile (`Account.name`) at
upload time; no profile → `no-profile`.

### 3.2 Legacy bidders

Bidders created before this feature have no `folder`. An idempotent backfill runs at API startup (after
`init_db`): for each bidder without `folder`, take the prefix of its `screenshotFolderUrl`
(`parse_folder_url`, trailing `/` removed, e.g. `puma`, `bids/ana`) and slug it the same way (`/` → `-`); on a
clash append `-2`… A first pass gives bidders already stored at `<BID_STORAGE_BUCKET>/<slug>/` exactly that
folder; the rest follow in `_id` order. `python -m app.bids.folders --report` lists (read-only) the active
bidders whose folder URL changes on registration. Legacy bidders have no invite: status "invited" with no
code until the owner clicks **New invite code**.

### 3.3 Status (derived, not stored)

- `archived` — `archivedAt` set
- `active` — `registeredAt` set
- `invited` — otherwise (with or without an outstanding code)

## 4. Settings — `app/config.py`

```
BID_STORAGE_BUCKET=bid-screenshots     # required to create bidders and sign uploads
```

The Backblaze key in `BID_STORAGE_ACCESS_KEY_ID` / `BID_STORAGE_SECRET_ACCESS_KEY` must allow **write and list**
on the whole bucket (`listBuckets`, `listFiles`, `writeFiles`). Bidder tokens are signed with `AUTH_SECRET`.

## 5. Invite codes — `app/bids/invites.py`

- Format: `<TAG>-XXXX-XXXX`, where `TAG` is the first 4 characters of the folder upper-cased (padded with `X`)
  and each `X` is drawn with `secrets` from Crockford base32 (`0-9 A-Z` without `I L O U`). ~40 bits of entropy.
- Normalization before hashing and comparing: upper-case, map `O→0`, `I/L→1`, then drop every character outside
  `[0-9A-Z]` (spaces, `-`, tabs, NBSPs, zero-width spaces).
- `new_invite(bidder) -> str`: sets `inviteCodeHash`, `inviteExpiresAt = now + 7 days`, returns the code. The code
  itself is never stored.
- Registration consumes the code: `inviteCodeHash` and `inviteExpiresAt` are cleared in the same update that sets
  the login (single use).

## 6. API

### 6.1 Owner routes (`app/routers/bidders.py`, existing owner-only auth)

| Method | Path | Behaviour |
|---|---|---|
| `POST` | `/bidders` | Body no longer takes `screenshotFolderUrl`. Assigns `folder`, sets the URL, creates an invite. Returns `{"bidder": <bidder_out>, "invite": {"code", "expiresAt"}}`. 503 `BID_STORAGE_BUCKET is not set` when unset. |
| `PUT` | `/bidders/{id}` | Unchanged, except `screenshotFolderUrl` is no longer editable (ignored if sent). |
| `POST` | `/bidders/{id}/invite` | Only for `invited` bidders (409 otherwise). Replaces any outstanding code. Returns `{"code", "expiresAt"}`. |
| `POST` | `/bidders/{id}/reset-login` | Only for `active` bidders (409 otherwise). Clears `username`, `passwordHash`, `registeredAt`; `tokenVersion += 1`; new invite. Returns `{"code", "expiresAt"}`. |
| `DELETE` | `/bidders/{id}` | Archive as today, plus `tokenVersion += 1` and clears any outstanding invite. |
| `GET` | `/bidders` | `bidder_out` gains `folder`, `status`, `username`, `inviteExpiresAt` (null when no code), `registeredAt`. |

### 6.2 Extension routes (`app/routers/bidder_ext.py`, prefix `/ext`)

| Method | Path | Body → result |
|---|---|---|
| `POST` | `/ext/register` | `{inviteCode, username, password}` → `{token, bidder: <me>}`. 400 `Invite code is invalid or expired` (wrong, used, expired, archived bidder — one message). 409 `Username is taken`. 400 with the rule on rule violations (below). |
| `POST` | `/ext/login` | `{username, password}` → `{token, bidder: <me>}`. 401 `Wrong username or password` (also for archived bidders). 400 with the rule on rule violations. |
| `GET` | `/ext/me` | → `<me>` = `{name, folder, profileName, username, status}` (+ `token` when renewed, §6.3) |
| `POST` | `/ext/upload-url` | `{job, step, trigger, capturedAt}` → `{url, key, expiresAt, headers: {"Content-Type": "image/png"}}` (+ `token` when renewed, §6.3) |

Rules: `username` 3–40 characters `[a-z0-9._-]` (input lower-cased); `password` at least 8 characters.
The schemas only strip/lower-case the username and cap lengths (username 100, password 1024; past that the
generic 400 `Invalid request`); the routes check the rules after reserving a limiter slot and answer 400 with
`Username must be 3–40 characters: lowercase letters, numbers, dots, underscores or dashes`,
`Password must be at least 8 characters` or (over 72 bytes) `Password is too long`.
Error bodies are `{"error": "<message>"}` (the app's exception handlers, `app/main.py`).

### 6.3 Bidder tokens and auth dependency

- JWT HS256 with `AUTH_SECRET`; claims `{sub: <bidder id>, typ: "bidder", ver: <tokenVersion>, iat: now, exp: now + 30 days}`.
- Renewal: when the presented token is older than 7 days (or has no `iat`), `/ext/me` and `/ext/upload-url` add a
  fresh `token` (same `ver`, new `exp`) to their response; the extension stores any `token` it receives.
- `get_current_bidder` (new, `app/deps.py`): bearer token → decode → `typ == "bidder"` → load bidder → refuse
  (401) when missing, archived, not registered, or `ver != tokenVersion`.
- `get_current_user` (existing) refuses tokens with `typ == "bidder"` (401). App tokens have no `typ` and are
  refused by `get_current_bidder`.

### 6.4 Signing an upload — `/ext/upload-url`

Request validation:
- `job`: 1–200 characters. Slug: lower-case, runs outside `[a-z0-9._-]` → `-`, trim `-`, `_` and `.` from both
  ends (so it can never look like a skipped `_` file), cut to 80, empty → `job`.
- `step`: integer 1–500. `trigger`: `step` | `next` | `submit`.
- `capturedAt`: ISO datetime **with** UTC offset (naive → 422). More than 24 h from server now → 422
  `capturedAt is not current`.

Key: `<folder>/<D>/<profile>/<job>/step<step>_<trigger>_<D>_<HHMMSS>.png`, where `D` and `HHMMSS` are taken from
`capturedAt` in its own offset (the bidder's local date and time — same as the extension today).

Signing: `S3BidStorage.presign_put(bucket, key, content_type="image/png", expires=300) -> str`
(`generate_presigned_url("put_object", Params={Bucket, Key, ContentType}, ExpiresIn=300, HttpMethod="PUT")`),
using the existing client (Backblaze endpoint, path style, SigV4). No network call. Missing bucket or storage
config → 503 `Uploads are not configured`.

### 6.5 Brute-force limit

In-process limiter (`app/bids/rate_limit.py`): failed `/ext/login` and `/ext/register` attempts are counted per
username and per client IP; 10 failures within 15 minutes → 429 with `Retry-After`. A success clears the
username's counter. Per process only (acceptable: one API process). The client IP is the first entry of
`X-Forwarded-For` when present (Railway's proxy), else `request.client.host`; otherwise every bidder would share
the proxy's IP and lock each other out. The forwarded entry is capped at 64 characters and limiter keys at 100
(username) / 64 (IP); past 10,000 keys, stale keys are swept on the next attempt.

## 7. Extension (Bid Track 3.0.0)

Put the folder under git before changing it (local repo; remote chosen by the user).

### 7.1 New `lib/api.js`
- `serverUrl` setting (default `https://engineersbackend-production.up.railway.app`), stored in `chrome.storage.local`.
- Token in `chrome.storage.local` (`authToken`) with the last `me` payload (`authBidder`).
- `register`, `login`, `logout` (clears token), `me`, `getUploadUrl`. A 401 from any call clears the token and
  marks the extension logged out.
- `localIsoWithOffset(date)`: e.g. `2026-10-03T11:29:07-03:00`.

### 7.2 Popup
- Logged out: tabs **Register** (invite code, username, password) and **Log in** (username, password); error
  text from the API is shown under the form.
- Logged in: header `Puma · Upwork · uploading to puma/`, the existing record list, **Log out**.
- After a 401 (only if the stored token is still the one that was sent): logged-out view on the **Log in** tab with
  *"You were logged out — log in again. If your manager reset your login, ask them for a new invite code."*

### 7.3 Options page
- Remove the Backblaze section and its Test upload.
- Keep Downloads folder, auto-download toggles, auto-capture, show Save button.
- Add **Advanced → Server URL**.

### 7.4 Capture (`background.js` `capturePage`)
Replaces `buildScreenshotObjectKey` + `uploadPngToB2`:
1. Capture as today.
2. If logged in: `getUploadUrl({job: <last segment of record.downloadFolder>, step, trigger, capturedAt})`, then
   `PUT` the PNG bytes to `url` with the returned headers. Store the returned `key` as `capture.b2FileName`.
3. Save locally and update the record as today.
- Not logged in: no upload; toast *"Saved locally — log in to Bid Track so this bid counts."*
- Upload failure: `capture.b2Error` as today; the local copy is kept.

### 7.5 Cleanup
- Delete `lib/b2-upload.js`, `lib/b2-s3-upload.js`, `lib/aws4fetch.mjs`, `lib/b2-credentials.js`,
  `lib/b2-path.js` (if unused) and `scripts/`.
- Remove `b2*` defaults from `lib/storage.js`.
- `chrome.runtime.onInstalled` (reason `update`): remove `b2ApplicationKeySecret` and every `b2*` setting from
  storage.
- `manifest.json`: version `3.0.0`; drop the `side_panel` entry (its page does not exist).

## 8. App — Bidders page (`engineers_app`)

- **Add bidder**: the folder URL input is removed. On success an **Invite dialog** shows the code with **Copy**,
  the expiry, "You won't be able to see this code again", the instruction text, and **Copy instructions + code**
  (code + "Install Bid Track → click the icon → Register → enter this code and choose a username and password").
- **Edit bidder**: shows `Folder: puma (s3://bid-screenshots/puma/)` read-only.
- **Table**: new **Status** column — `Invited · expires Oct 10` (red when expired; `no code yet` when none),
  `Active · @username`, `Archived`.
- **Action menu**: `New invite code` (invited), `Reset login` (active; confirm: "Reset Puma's login? Their
  extension stops uploading until they register again with the new code. Their folder and bid history stay."),
  plus the existing Edit, History, Archive.
- Types in `src/api/endpoints.ts`; `createBidder` returns `{bidder, invite}`; new `newBidderInvite(id)` and
  `resetBidderLogin(id)`.

## 9. Rollout

1. Merge `feat/bid-b2` with §2.
2. Create a Backblaze key with write + list on the whole bucket; set `BID_STORAGE_*`, `BID_STORAGE_BUCKET` and the
   endpoint on Railway (API **and** cron services).
3. Deploy backend, then app. Existing bidders show "Invited · no code yet"; counting continues unchanged.
4. Run `python -m app.bids.folders --report`. Registration switches counting to the new folder for the whole
   week in progress, so register the listed bidders right after a weekly cutoff, or adjust them by hand.
5. Issue codes; send each bidder the 3.0.0 zip (load unpacked) with: Remove any older "Bid Track Local" extension
   first (or unzip this version over its folder and click Reload). Bidders register.
6. Revoke each old shared upload key (`puma-uploader`, …) as soon as that prefix's bidders have registered, so
   only the backend can write. Until then old installs keep uploading with their own key and those uploads count.

## 10. Error handling

| Situation | Behaviour |
|---|---|
| Wrong / expired / used code | 400, one generic message (no hint which). |
| Bidder archived or login reset | Next extension call 401 → logged-out view with the reset message; captures still saved locally. |
| Storage not configured | `POST /bidders` and `/ext/upload-url` → 503 with a message naming the setting. |
| Upload link expired before PUT | Backblaze 403 → `capture.b2Error`; the next capture of the same job uploads and still counts the bid. |
| Clock far off on the bidder's machine | 422 `capturedAt is not current`; shown as the capture's error. |
| Folder name clash | `-2`, `-3`… suffix; unique index guards races. |

## 11. Testing

Backend (`tests_unit`, no network; mongomock-motor for DB tests):
- `folder_slug`: accents, symbols, length, empty; clash suffixes; backfill from legacy URLs (`puma`, `bids/ana`,
  clash).
- Invites: format and entropy alphabet, normalization (`o`/`0`, `l`/`1`, dashes, case), expiry, single use,
  replacement, archived bidder.
- Register / login / me; username rules and uniqueness; password length; reset-login and archive invalidate
  tokens; bidder token refused by owner routes and app token refused by `/ext`.
- Upload path: date and time from `capturedAt` offset, job slug rules (leading `_` stripped), step and trigger
  bounds, naive or stale `capturedAt`, profile slug and `no-profile`.
- `presign_put`: signed URL host, path-style bucket/key, `X-Amz-Expires=300`, content type in the signed
  headers — computed locally.
- Rate limiter: 10 failures → 429, reset on success, window expiry.
- Owner routes: create returns invite, new code 409 on active, reset 409 on invited, status values.

Frontend: `tsc` + `vite build`; manual pass of the invite dialog, status column, New invite code, Reset login.

Extension: no test runner; manual checklist (register, login, logout, 401 after reset, capture uploads, offline
capture) plus the end-to-end run below.

End-to-end (this machine, backend on :8082, extension Server URL `http://localhost:8082`): create a test bidder →
register with the code → capture a real job page → object appears at
`<folder>/<date>/<profile>/<job>/step1_step_…png` in Backblaze → live count +1 → Reset login → next capture
refused → one test post to the Slack test channel.
