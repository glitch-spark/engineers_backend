# Bidder Extension Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bidders register in the Bid Track extension with an invite code and their screenshots land in their own Backblaze folder via backend-signed uploads, with no storage setup.

**Architecture:**
- **Backend** (FastAPI + Beanie):
  - Bidders get an automatic `folder`, invite codes and a bidder login.
  - A separate `/ext` router issues 30-day bidder JWTs and pre-signed Backblaze PUT links for paths the backend builds itself.
- **App:** the Bidders page shows invite codes and login status.
- **Extension:** swaps its own Backblaze client for the backend API and a login popup.
- **Counting** (listing folders) is unchanged.

**Tech Stack:** FastAPI, Beanie/Motor, pydantic 2.13, python-jose, passlib/bcrypt, boto3 (Backblaze S3-compatible), pytest + mongomock-motor; React 18 + TS + SWR; Chrome MV3 extension (plain ES modules).

**Spec:** `engineers_backend/docs/superpowers/specs/2026-10-03-bidder-extension-login-design.md`

**Repos:**

| Repo | Branch | Tasks |
|---|---|---|
| `engineers_backend` | `feat/bid-b2` | Task 1 |
| `engineers_backend` | `feat/bidder-login` (rebased onto `feat/bid-b2` after Task 1) | Tasks 2–6 |
| `engineers_app` | `feat/bidder-login`, created in Task 7 from `feat/bid-b2` | Task 7 |
| `Engineer/Bid Track (BackBlaze)` | `main`, git-initialised in Task 8 | Tasks 8–9 |

**Backend test command** (from the backend root):

```bash
.venv/Scripts/python.exe -m pytest tests_unit/<file> -v
```

Full suite:

```bash
.venv/Scripts/python.exe -m pytest tests_unit -q --ignore=tests_unit/test_resume_experience_roles.py --ignore=tests_unit/test_style_serializer.py --ignore=tests_unit/test_template_annotate_removals.py
```

The 3 ignored files already fail on the base branch.

Every new test file starts with the env preamble used by the other `tests_unit/test_bids_*.py` files (`MONGODB_URI`, `AUTH_SECRET` via `os.environ.setdefault`). DB tests use the `db` / `api` fixtures pattern from `tests_unit/test_bids_db.py`.

## Global Constraints

- Upload key: `<folder>/<D>/<profile>/<job>/step<step>_<trigger>_<D>_<HHMMSS>.png`. `D` and `HHMMSS` come from `capturedAt` in its own UTC offset.
- Folder slug:
  - NFKD, drop accents, lower-case.
  - Runs outside `[a-z0-9]` → `-`, trim `-`.
  - Max 40 characters (re-trim a trailing `-`); empty → `bidder`.
  - Clash → `-2`, `-3`, … (archived bidders count).
- Profile segment: same slug of `Account.name`; no profile (or empty slug) → `no-profile`.
- Job slug:
  - Lower-case; runs outside `[a-z0-9._-]` → `-`.
  - Strip `-`, `_`, `.` from both ends.
  - Max 80; empty → `job`.
- Invite code format and lifetime:
  - `<TAG>-XXXX-XXXX`. `TAG` = first 4 of the folder's `[A-Z0-9]` upper-cased, padded with `X`.
  - The `X` characters come from `0123456789ABCDEFGHJKMNPQRSTVWXYZ` via `secrets`.
  - Valid 7 days, single use.
  - Only `sha256(normalized)` hex is stored.
- Invite normalization: upper-case, remove spaces and `-`, `O→0`, `I→1`, `L→1`.
- Bidder JWT: HS256 with `AUTH_SECRET`, claims `{sub: <bidder id>, typ: "bidder", ver: <tokenVersion>, exp: now+30 days}`.
- Upload link: `PUT`, `ContentType="image/png"`, `ExpiresIn=300`.
- Upload request limits:
  - `step` 1–500; `trigger` ∈ `step|next|submit`.
  - `job` 1–200 characters.
  - `capturedAt` must carry an offset, and be within 24 h of now.
- Credentials: username 3–40 characters of `[a-z0-9._-]` (input lower-cased); password ≥ 8 characters.
- Brute-force limit:
  - 10 failed `/ext/login` or `/ext/register` attempts per username and per client IP in 15 minutes → 429 with `Retry-After`. Success clears that username.
  - Client IP = first `X-Forwarded-For` entry, else `request.client.host`.
- Status (derived): `archived` if `archivedAt`; `active` if `registeredAt`; else `invited`.
- `tokenVersion += 1` on Reset login and on archive.
- Exact API messages:

  | Message | Status | When |
  |---|---|---|
  | `Invite code is invalid or expired` | 400 | Any register failure about the code |
  | `Username is taken` | 409 | |
  | `Wrong username or password` | 401 | Includes archived bidders |
  | `capturedAt is not current` | 422 | |
  | `BID_STORAGE_BUCKET is not set` | 503 | `POST /bidders` |
  | `Uploads are not configured` | 503 | `/ext/upload-url` |
  | `Already registered — use Reset login` | 409 | |
  | `Not registered yet — use New invite code` | 409 | |
  | `Bidder is archived` | 409 | |
  | `Too many attempts — try again later` | 429 | |

- Extension default server URL: `https://engineersbackend-production.up.railway.app`.
- Commits end with the session's Co-Authored-By trailer. Never push.

## Review Focus

1. **A bidder types the code in lower case, with spaces, or with `o`/`l` instead of `0`/`1`.** Registration should still succeed. Test: `test_register_accepts_a_sloppily_typed_code` (Task 5).
2. **The owner renames a bidder after inviting them.** The folder (and so their uploads and counts) must not move. Test: `test_rename_keeps_the_folder` (Task 4).
3. **The owner changes the bidder's profile after registration.** The next upload goes under the new profile segment, with no re-login. Test: `test_upload_uses_the_current_profile` (Task 6).
4. **A bidder in UTC+5:30 or UTC-3 captures just after their local midnight.** The date folder is *their* date, not the server's. Test: `test_key_uses_the_bidders_local_date` (Task 6).
5. **An archived bidder still has the extension open.** Login says only "Wrong username or password", and existing tokens are refused. Tests: `test_archived_bidder_cannot_log_in` and `test_archive_revokes_tokens` (Task 5).

---

### Task 1: Counting fixes on `feat/bid-b2`

**Files:**
- Modify: `app/bids/storage.py`. Replace the `TEST_UPLOAD_PREFIX` check in `job_first_seen` with "skip names starting with `_`", and remove the constant.
- Modify: `app/bids/slack_format.py:96`. Make the footer word follow the count: `1 bidder` / `N bidders`.
- Test: `tests_unit/test_bids_storage.py`, `tests_unit/test_bids_slack_format.py`

**Interfaces:** none new.

- [ ] **Step 1: Switch branch** — `git switch feat/bid-b2`.

- [ ] **Step 2: Write the failing tests**
```python
# test_bids_storage.py
def test_any_underscore_file_is_not_a_bid():
    objs = [obj("puma/_aws4_test_1791030694686.png", utc(2026, 10, 3)),
            obj("puma/_node_test_1791030767411.png", utc(2026, 10, 3)),
            obj("puma/2026-10-03/upwork/_bid_track_connection_test_x.png", utc(2026, 10, 3)),
            obj("puma/2026-10-03/upwork/acme_1/step1_step_x.png", utc(2026, 10, 3))]
    assert list(job_first_seen(objs)) == ["puma/2026-10-03/upwork/acme_1/"]

# test_bids_slack_format.py
def test_daily_footer_says_bidder_for_one():
    _, messages = format_report("daily", START, END, [row(PEDRO, "Ana", 3)], OWNERS, CT, "https://a")
    assert texts(messages)[-1].startswith("*Total: 3 bids · 1 bidder*")
```
The existing assertion `"*Total: 142 bids · 4 bidders* · …"` must keep passing.

- [ ] **Step 3: Run to verify failure** — `pytest tests_unit/test_bids_storage.py tests_unit/test_bids_slack_format.py -v` → the 2 new tests FAIL.

- [ ] **Step 4: Implement both changes.**

- [ ] **Step 5: Run the full suite** → all pass.

- [ ] **Step 6: Commit, then rebase the feature branch**
```bash
git add app/bids/storage.py app/bids/slack_format.py tests_unit/test_bids_storage.py tests_unit/test_bids_slack_format.py
git commit -m "fix(bids): skip all _-prefixed files and say 1 bidder"
git switch feat/bidder-login && git rebase feat/bid-b2
```
Expected: the rebase applies the spec and plan commits cleanly.

---

### Task 2: Bidder login fields, folder assignment, backfill

**Files:**
- Create: `app/bids/folders.py`
- Modify: `app/models/bidder.py` (new fields + 3 indexes), `app/config.py` (`BID_STORAGE_BUCKET`), `app/main.py` (run the backfill in `lifespan` after `init_db()`, in try/except with `log.exception`)
- Modify: `.env.example` and the `docs/bids.md` env block. Add `BID_STORAGE_BUCKET=bid-screenshots`, with its comment on its own line. Bump the key count in `test_env_examples_have_no_inline_comments_in_bid_values` (`tests_unit/test_bids_windows.py`) from 10 to 11.
- Test: `tests_unit/test_bids_folders.py`

**Interfaces:**
- Produces, on `Bidder`:
  - `folder: Optional[str] = None`
  - `inviteCodeHash: Optional[str] = None`, `inviteExpiresAt: Optional[datetime] = None`
  - `username: Optional[str] = None`, `passwordHash: Optional[str] = None`, `registeredAt: Optional[datetime] = None`
  - `tokenVersion: int = 0`
  - Indexes: `IndexModel([("folder", 1)], unique=True, sparse=True)`, and the same for `username` and `inviteCodeHash`.
- Produces in `app/config.py`: `BID_STORAGE_BUCKET: Optional[str] = None`, blank → `None`.
- Produces in `app/bids/folders.py`:
  - `slug(text: str, max_len: int = 40, empty: str = "bidder") -> str`
  - `profile_segment(profile_name: Optional[str]) -> str`. It equals `slug(profile_name or "", empty="no-profile")`.
  - `async def unique_folder(name: str) -> str`. It returns the first free one of `slug(name)`, `slug(name)-2`, …, checking all bidders.
  - `async def backfill_folders() -> int`. It returns the number updated.
  - `folder_url(bucket: str, folder: str) -> str`. It returns `f"s3://{bucket}/{folder}/"`.

- [ ] **Step 1: Write the failing tests**
```python
@pytest.mark.parametrize("text,expected", [
    ("Puma", "puma"), ("Ana Silva", "ana-silva"), ("José Ñandú", "jose-nandu"),
    ("  --Ana__2!! ", "ana-2"), ("", "bidder"), ("!!!", "bidder"), ("x" * 50, "x" * 40),
    ("a" * 39 + " b", "a" * 39),          # cut at 40 leaves a trailing "-", re-trimmed
])
def test_slug(text, expected):
    assert slug(text) == expected

def test_profile_segment():
    assert profile_segment("Upwork") == "upwork"
    assert profile_segment("AI Full-stack") == "ai-full-stack"
    assert profile_segment(None) == "no-profile" and profile_segment("!!") == "no-profile"

async def test_unique_folder_appends_a_suffix(db):     # archived bidders still hold their folder
    owner = await make_user()
    await make_bidder(owner, "Ana", folder="ana", archived=True)
    await make_bidder(owner, "Ana 2", folder="ana-2")
    assert await unique_folder("Ana") == "ana-3"
    assert await unique_folder("Puma") == "puma"

async def test_backfill_takes_the_folder_from_legacy_urls(db):
    owner = await make_user()
    puma = await make_bidder(owner, "Puma", screenshotFolderUrl="s3://bid-screenshots/puma/")
    nested = await make_bidder(owner, "Ana", screenshotFolderUrl="s3://shots/bids/ana/")
    clash = await make_bidder(owner, "Puma B", screenshotFolderUrl="https://f005.backblazeb2.com/file/b/puma/")
    done = await make_bidder(owner, "Raj", folder="raj")
    assert await backfill_folders() == 3
    got = {b.id: b.folder for b in await Bidder.find({}).to_list()}
    assert (got[puma.id], got[nested.id], got[clash.id], got[done.id]) == ("puma", "bids-ana", "puma-2", "raj")
    assert await backfill_folders() == 0                 # idempotent
```
- Copy the `db`, `make_user` and `make_bidder` helpers from `test_bids_db.py`, with `make_bidder` taking `**kw` for `folder` / `screenshotFolderUrl`.
- Backfill order is by `_id`, so `puma` keeps `puma` and the clash gets `-2`.

- [ ] **Step 2: Run to verify failure** — `pytest tests_unit/test_bids_folders.py -v` → ImportError.

- [ ] **Step 3: Implement the model fields and indexes, the setting (validator like `_normalize_bid_endpoint`: strip, blank → None), and `app/bids/folders.py`.**
  - The backfill slug for a legacy URL is the `parse_folder_url` prefix without its trailing `/`, with `/` → `-`, run through `slug`.
  - A URL that doesn't parse slugs the bidder's name instead.

- [ ] **Step 4: Run the new file, then the full suite** → all pass (including the env-example test with 11 keys).

- [ ] **Step 5: Commit** — `feat(bids): bidder folders, login fields and legacy backfill`.

---

### Task 3: Invite codes

**Files:**
- Create: `app/bids/invites.py`
- Test: `tests_unit/test_bids_invites.py`

**Interfaces:**
- `ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"`, `INVITE_TTL = timedelta(days=7)`
- `generate_code(folder: str) -> str`
- `normalize_code(code: str) -> str`
- `hash_code(code: str) -> str` (sha256 hex of `normalize_code(code)`)
- `issue_invite(bidder, now: datetime) -> str`. Sets `bidder.inviteCodeHash` and `bidder.inviteExpiresAt = now + INVITE_TTL`, returns the code, does **not** save.
- `clear_invite(bidder) -> None`. Sets both fields to `None`.
- `invite_is_live(bidder, now: datetime) -> bool`. Requires a hash, not expired, not archived, not registered. A naive `inviteExpiresAt` from Mongo is treated as UTC.

- [ ] **Step 1: Write the failing tests**
```python
def test_code_shape():
    code = generate_code("puma")
    assert re.fullmatch(r"PUMA-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{4}-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{4}", code)
    assert generate_code("ab")[:5] == "ABXX-" and generate_code("ana-silva")[:5] == "ANAS-"
    assert len({generate_code("puma") for _ in range(200)}) == 200

def test_normalize_forgives_typing():
    assert normalize_code(" puma-7k2q-9xmd ") == "PUMA7K2Q9XMD"
    assert normalize_code("PUMA-7K2Q-9XMO") == normalize_code("puma 7k2q 9xm0")
    assert normalize_code("ILL1") == "1111"
    assert hash_code("puma-7k2q-9xmd") == hash_code("PUMA7K2Q9XMD")

def test_issue_and_live():
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    b = SimpleNamespace(folder="puma", inviteCodeHash=None, inviteExpiresAt=None, archivedAt=None, registeredAt=None)
    code = issue_invite(b, now)
    assert b.inviteCodeHash == hash_code(code) and b.inviteExpiresAt == now + timedelta(days=7)
    assert invite_is_live(b, now + timedelta(days=6, hours=23))
    assert not invite_is_live(b, now + timedelta(days=7))
    b.inviteExpiresAt = b.inviteExpiresAt.replace(tzinfo=None)          # as read back from Mongo
    assert invite_is_live(b, now)
    b.registeredAt = now
    assert not invite_is_live(b, now)
    clear_invite(b)
    assert b.inviteCodeHash is None and b.inviteExpiresAt is None
```
- [ ] **Step 2: Run to verify failure** → ImportError.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(bids): invite codes`.

---

### Task 4: Owner routes — automatic folder, invite, reset login, status

**Files:**
- Modify: `app/bids/schemas.py`.
  - `BidderIn` loses `screenshotFolderUrl` and its validator.
  - Add `bidder_status(b) -> str`.
  - `bidder_out` gains `folder`, `status`, `username`, `inviteExpiresAt` (`None` when `inviteCodeHash` is `None`) and `registeredAt` (ISO UTC via `_iso_utc`).
- Modify: `app/routers/bidders.py`.
  - `create_bidder`, `update_bidder`, `archive_bidder` change.
  - New `POST /{bidder_id}/invite` and `POST /{bidder_id}/reset-login`.
- Test: `tests_unit/test_bids_db.py`. Update `body()` (drop `screenshotFolderUrl`), `make_bidder` (accept `**kw`, default `folder=name.lower()`) and the existing create test for the new response shape. Add the tests below. Remove the URL tests from `tests_unit/test_bids_schemas.py`, since `parse_folder_url` keeps its own tests.

**Interfaces:**
- Consumes: Task 2 `unique_folder`, `folder_url`, the `BID_STORAGE_BUCKET` setting; Task 3 `issue_invite`, `clear_invite`.
- Produces:
  - `POST /bidders` → `{"bidder": <bidder_out>, "invite": {"code": str, "expiresAt": iso}}`
  - `POST /bidders/{id}/invite` and `POST /bidders/{id}/reset-login` → `{"code", "expiresAt"}`
  - Create inserts, and on a `DuplicateKeyError` for `folder` retries `unique_folder` (at most 5 attempts).
  - Archive sets `archivedAt`, `tokenVersion += 1` and `clear_invite` (still idempotent; a repeat changes nothing).

- [ ] **Step 1: Write the failing tests** in `test_bids_db.py`.
  - The `api` fixture does `monkeypatch.setenv("BID_STORAGE_BUCKET", "bid-screenshots")` then `get_settings.cache_clear()`, and calls `cache_clear()` again on teardown.
  - The 503 test does `monkeypatch.delenv("BID_STORAGE_BUCKET", raising=False)`, sets it to `""`, and clears the cache.
```python
async def test_create_assigns_folder_and_returns_an_invite(api):
    me = await make_user(); api.login(me)
    res = (await api.post("/bidders", json=body(name="Puma"))).json()
    assert res["bidder"]["folder"] == "puma"
    assert res["bidder"]["screenshotFolderUrl"] == "s3://bid-screenshots/puma/"
    assert res["bidder"]["status"] == "invited" and res["bidder"]["inviteExpiresAt"].endswith("Z")
    assert re.fullmatch(r"PUMA-\w{4}-\w{4}", res["invite"]["code"])
    second = (await api.post("/bidders", json=body(name="Puma"))).json()
    assert second["bidder"]["folder"] == "puma-2"

async def test_create_without_bucket_is_503(api, monkeypatch): ...   # BID_STORAGE_BUCKET None → 503 "BID_STORAGE_BUCKET is not set"

async def test_rename_keeps_the_folder(api):
    me = await make_user(); api.login(me)
    b = (await api.post("/bidders", json=body(name="Puma"))).json()["bidder"]
    res = (await api.put(f"/bidders/{b['_id']}", json=body(name="Puma Cruz", screenshotFolderUrl="s3://x/evil/"))).json()
    assert (res["name"], res["folder"], res["screenshotFolderUrl"]) == ("Puma Cruz", "puma", "s3://bid-screenshots/puma/")

async def test_new_invite_replaces_the_old_code(api): ...       # second code differs; hash changed; 200
async def test_new_invite_on_active_is_409(api): ...            # registeredAt set → 409 "Already registered — use Reset login"
async def test_reset_login(api):
    # active bidder (username/passwordHash/registeredAt set, tokenVersion=0) → 200 {code, expiresAt}
    # reloaded: username/passwordHash/registeredAt None, tokenVersion == 1, status "invited", folder unchanged
async def test_reset_login_on_invited_is_409(api): ...          # "Not registered yet — use New invite code"
async def test_archive_bumps_token_version_and_clears_invite(api): ...  # tokenVersion 1, inviteCodeHash None; second DELETE leaves tokenVersion 1
async def test_invite_actions_on_archived_are_409(api): ...     # both → "Bidder is archived"
async def test_another_users_bidder_invite_routes_404(api): ... # both new routes → 404
```
Each `...` test asserts exactly what its comment states.

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** `bidder_status` order: archived, then active, then invited.
- [ ] **Step 4: Run `test_bids_db.py`, `test_bids_schemas.py`, then the full suite** → all pass.
- [ ] **Step 5: Commit** — `feat(bids): automatic bidder folders, invites and reset login`.

---

### Task 5: Bidder accounts — tokens, register, login, me, brute-force limit

**Files:**
- Modify: `app/security.py`. Add `create_bidder_token(bidder_id: str, version: int) -> str`, with `BIDDER_TOKEN_DAYS = 30`.
- Modify: `app/deps.py`.
  - `get_current_user` returns 401 `invalid token` when `payload.get("typ") == "bidder"`.
  - Add `async def get_current_bidder(credentials) -> Bidder`.
- Create: `app/bids/rate_limit.py`, `app/routers/bidder_ext.py` (`router = APIRouter(prefix="/ext", tags=["bidder-ext"])`).
- Modify: `app/bids/schemas.py`. Add `RegisterIn`, `LoginIn`, and `bidder_me(b, profile_name) -> dict` with keys `name, folder, profileName, username, status`.
- Modify: `app/main.py`. Include `bidder_ext.router`.
- Test: `tests_unit/test_bidder_ext.py` (mongomock `db` plus an ASGI client over **both** `bidders.router` and `bidder_ext.router`; no dependency override for `get_current_bidder`), `tests_unit/test_bids_rate_limit.py`

**Interfaces:**
- `get_current_bidder` returns 401 `invalid token` for:
  - a missing or invalid bearer token;
  - `typ != "bidder"`;
  - an unknown bidder, an archived one, or one with `registeredAt` None;
  - `ver != tokenVersion`.
- `class FailureLimiter(limit: int = 10, window: timedelta = timedelta(minutes=15))`:
  - `retry_after(keys: list[str], now: datetime) -> Optional[int]` (seconds until the oldest failure for the most-blocked key leaves the window; `None` if none is at the limit)
  - `fail(keys: list[str], now: datetime) -> None`
  - `clear(key: str) -> None`
  - Module-level `LIMITER = FailureLimiter()`.
- `client_ip(request) -> str` (first `X-Forwarded-For` entry, stripped, else `request.client.host`, else `"unknown"`).
- Limiter keys: `f"user:{username}"` and `f"ip:{client_ip}"`.
- `POST /ext/register` body `{inviteCode, username, password}`:
  - Validation: username lower-cased, `^[a-z0-9._-]{3,40}$`; password ≥ 8. Failures are 422 with field `loc`.
  - Finds the bidder by `hash_code` and requires `invite_is_live`.
  - Sets `username`, `passwordHash`, `registeredAt`, and `clear_invite`.
  - Returns `{"token", "bidder": bidder_me}`.
- `POST /ext/login`, `GET /ext/me` as in the spec §6.2.

- [ ] **Step 1: Write the failing tests**
```python
# test_bids_rate_limit.py
def test_limits_after_ten_failures_and_recovers():
    lim, t0 = FailureLimiter(), datetime(2026, 10, 3, tzinfo=timezone.utc)
    for i in range(9):
        lim.fail(["user:a", "ip:1"], t0 + timedelta(seconds=i))
    assert lim.retry_after(["user:a", "ip:1"], t0 + timedelta(seconds=10)) is None
    lim.fail(["user:a", "ip:1"], t0 + timedelta(seconds=9))
    assert 0 < lim.retry_after(["user:a"], t0 + timedelta(seconds=10)) <= 900
    assert lim.retry_after(["ip:1"], t0 + timedelta(minutes=15, seconds=1)) is None
    lim.clear("user:a")
    assert lim.retry_after(["user:a"], t0 + timedelta(seconds=10)) is None

def test_client_ip_prefers_forwarded_for():
    assert client_ip(SimpleNamespace(headers={"x-forwarded-for": " 1.2.3.4, 10.0.0.1"}, client=None)) == "1.2.3.4"
    assert client_ip(SimpleNamespace(headers={}, client=SimpleNamespace(host="9.9.9.9"))) == "9.9.9.9"

# test_bidder_ext.py  (owner creates the bidder via POST /bidders to get a real code)
async def test_register_login_me(ext): ...                 # register → token; /ext/me → {name, folder, profileName, username, status:"active"}; login works
async def test_register_accepts_a_sloppily_typed_code(ext):   # code.lower(), spaces for dashes, "0"→"o" in the random part → 200
async def test_register_code_is_single_use(ext): ...       # second register with same code → 400 "Invite code is invalid or expired"
async def test_register_expired_code(ext): ...             # inviteExpiresAt in the past → 400 same message
async def test_register_rejects_bad_credentials(ext): ...  # "ab" and "Bad Name!" usernames, 7-char password → 422; taken username → 409 "Username is taken"
async def test_login_wrong_password(ext): ...              # 401 "Wrong username or password"
async def test_archived_bidder_cannot_log_in(ext): ...     # archive via owner route → login 401 same message
async def test_archive_revokes_tokens(ext): ...            # token from before archive → /ext/me 401
async def test_reset_login_revokes_tokens(ext): ...        # token from before reset → 401; register again with new code → 200, same folder
async def test_tokens_do_not_cross(ext): ...               # bidder token on GET /bidders (real get_current_user) → 401; app user token (create_access_token) on /ext/me → 401
async def test_eleventh_failed_login_is_429(ext): ...      # 10 wrong passwords → 11th 429 "Too many attempts — try again later" with Retry-After
```
- The `ext` fixture builds one app with `bidders.router` (with `get_current_user` overridden to the owner, exactly as in `api`, including the `BID_STORAGE_BUCKET` env) and `bidder_ext.router` (no override; real bidder tokens). It also resets `bidder_ext.LIMITER` to a fresh `FailureLimiter()`.
- `test_tokens_do_not_cross` builds a second app with `bidders.router` and **no** overrides, to show the real `get_current_user` refuses a bidder token.

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.**
  - The register and login handlers check `LIMITER.retry_after` first (→ 429 with `Retry-After` header).
  - On failure they call `LIMITER.fail`; on success `LIMITER.clear(f"user:{username}")`.
  - Use `hash_password` / `verify_password` from `app/security.py`. Wrong username and wrong password follow the same path (no username enumeration).
- [ ] **Step 4: Run both new files, then the full suite** → all pass. Check that the existing `get_current_user` callers still pass (the full suite covers this).
- [ ] **Step 5: Commit** — `feat(bids): bidder accounts for the extension`.

---

### Task 6: Signed uploads — `/ext/upload-url`

**Files:**
- Create: `app/bids/upload_keys.py`
- Modify: `app/bids/storage.py`. Add `S3BidStorage.presign_put(bucket: str, key: str, content_type: str = "image/png", expires: int = 300) -> str` using `generate_presigned_url("put_object", Params={"Bucket", "Key", "ContentType"}, ExpiresIn=expires, HttpMethod="PUT")`; a `BidStorageError` on a client `ValueError`, as `_get_client` already does.
- Modify: `app/routers/bidder_ext.py`. Add `POST /ext/upload-url`, body `UploadUrlIn{job: str (1–200), step: int (1–500), trigger: Literal["step","next","submit"], capturedAt: AwareDatetime}`.
- Modify: `docs/bids.md`. Add a "Bidder extension" section: invites, statuses, Reset login, the backend key needing `listBuckets`, `listFiles` and `writeFiles` on the whole bucket, and the `/ext` routes.
- Test: `tests_unit/test_bids_upload_keys.py`; extend `tests_unit/test_bidder_ext.py` and `tests_unit/test_bids_storage.py`.

**Interfaces:**
- `job_slug(job: str) -> str`
- `build_upload_key(folder: str, profile_name: Optional[str], job: str, step: int, trigger: str, captured_at: datetime, now: datetime) -> str`. Raises `ValueError("capturedAt is not current")` when `abs(captured_at - now) > 24h`.
- Route responses:
  - `{"url", "key", "expiresAt": iso, "headers": {"Content-Type": "image/png"}}`
  - 503 `Uploads are not configured` when `BID_STORAGE_BUCKET` is unset, the provider is not `s3`, or `presign_put` raises `BidStorageError`.
  - 422 `capturedAt is not current`.
  - Naive `capturedAt` → pydantic 422.
- The route loads the bidder's **current** profile name at request time (`Account.get(bidder.profileId)`).

- [ ] **Step 1: Write the failing tests**
```python
# test_bids_upload_keys.py
NOW = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)

@pytest.mark.parametrize("raw,expected", [
    ("acme_12345", "acme_12345"), ("Filevine_0585-B264", "filevine_0585-b264"),
    ("__hidden.", "hidden"), ("a b/c?d", "a-b-c-d"), ("!!!", "job"), ("x" * 120, "x" * 80),
])
def test_job_slug(raw, expected):
    assert job_slug(raw) == expected

def test_key_layout():
    at = datetime(2026, 10, 3, 9, 30, 15, tzinfo=timezone(timedelta(hours=-3)))
    assert build_upload_key("puma", "Upwork", "acme_12345", 2, "next", at, NOW) == \
        "puma/2026-10-03/upwork/acme_12345/step2_next_2026-10-03_093015.png"
    assert build_upload_key("puma", None, "acme", 1, "step", at, NOW).startswith("puma/2026-10-03/no-profile/acme/")

def test_key_uses_the_bidders_local_date():
    ist = datetime(2026, 10, 4, 0, 5, tzinfo=timezone(timedelta(hours=5, minutes=30)))   # 18:35 UTC on Oct 3
    assert "/2026-10-04/" in build_upload_key("ana", "Backend", "j", 1, "step", ist, NOW)

def test_stale_capture_time_is_rejected():
    with pytest.raises(ValueError, match="capturedAt is not current"):
        build_upload_key("ana", None, "j", 1, "step", NOW - timedelta(hours=25), NOW)

# test_bids_storage.py
def test_presign_put_is_a_short_lived_png_put_on_backblaze(monkeypatch):
    # monkeypatch app.bids.storage.get_settings -> SimpleNamespace(BID_STORAGE_ENDPOINT_URL=
    # "https://s3.us-east-005.backblazeb2.com", BID_STORAGE_ACCESS_KEY_ID="005abc", BID_STORAGE_SECRET_ACCESS_KEY=
    # "K005secret", BID_STORAGE_REGION=None, AWS_REGION=None, AWS_ACCESS_KEY_ID=None, AWS_SECRET_ACCESS_KEY=None);
    # storage = S3BidStorage()  (real boto3 client; signing is offline, no network)
    url = storage.presign_put("bid-screenshots", "puma/2026-10-03/upwork/acme/step1_step_2026-10-03_093015.png")
    parts = urlsplit(url); q = parse_qs(parts.query)
    assert parts.netloc == "s3.us-east-005.backblazeb2.com"
    assert parts.path == "/bid-screenshots/puma/2026-10-03/upwork/acme/step1_step_2026-10-03_093015.png"
    assert q["X-Amz-Expires"] == ["300"] and "content-type" in q["X-Amz-SignedHeaders"][0]
    assert q["X-Amz-Credential"][0].startswith("005abc/") and "/us-east-005/s3/" in q["X-Amz-Credential"][0]

# test_bidder_ext.py
async def test_upload_url_signs_the_bidders_path(ext): ...      # fake storage records (bucket, key); response key == "puma/<D>/upwork/acme_1/step1_step_<D>_<T>.png", headers Content-Type image/png
async def test_upload_uses_the_current_profile(ext): ...        # owner PUT changes profile → next upload-url key has the new profile segment
async def test_upload_url_validation(ext): ...                  # step 0 / 501, trigger "click", naive capturedAt → 422; 25h old → 422 "capturedAt is not current"
async def test_upload_url_requires_a_live_bidder_token(ext): ...  # no token / after archive → 401
async def test_upload_url_without_storage_is_503(ext): ...      # BID_STORAGE_BUCKET None → 503 "Uploads are not configured"
```
The route tests monkeypatch `bidder_ext.get_bid_storage` to a fake whose `presign_put(bucket, key)` returns `f"https://signed/{bucket}/{key}"` and records the call.

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** `build_upload_key` uses `slug` and `profile_segment` from Task 2.
- [ ] **Step 4: Run the new and extended files, then the full suite** → all pass.
- [ ] **Step 5: Commit** — `feat(bids): backend-signed screenshot uploads for the extension`.

---

### Task 7: App — invite dialog, status, actions (`engineers_app`)

**Files:**
- Branch: `git -C engineers_app switch feat/bid-b2 && git -C engineers_app switch -c feat/bidder-login`
- Modify: `src/api/endpoints.ts`.
  - `Bidder` gains `folder: string | null; status: 'invited' | 'active' | 'archived'; username: string | null; inviteExpiresAt: string | null; registeredAt: string | null`.
  - `BidderInput` drops `screenshotFolderUrl`.
  - New `interface BidderInvite { code: string; expiresAt: string }`.
  - `createBidder` returns `Promise<{ bidder: Bidder; invite: BidderInvite }>`.
  - New `newBidderInvite(id: string): Promise<BidderInvite>` (POST `/bidders/{id}/invite`) and `resetBidderLogin(id: string): Promise<BidderInvite>` (POST `/bidders/{id}/reset-login`).
- Create: `src/components/bidders/InviteDialog.tsx`. Props `{ open: boolean; bidderName: string; invite: BidderInvite | null; onClose: () => void }`.
- Modify: `src/components/bidders/BidderFormModal.tsx`.
  - Remove the folder URL input and its state.
  - In edit mode, show the line `Folder: <folder> (<screenshotFolderUrl>)` read-only.
  - `onSaved` becomes `(invite: BidderInvite | null, bidder: Bidder) => void`. The invite is set on create and `null` on edit.
- Modify: `src/pages/Bidders.tsx`.
  - A **Status** column after Name.
  - Row actions: `New invite code` (status `invited`), `Reset login` (status `active`, via `ConfirmDialog`).
  - Open `InviteDialog` after create, New invite code, and Reset login; revalidate the list after each.

**Exact copy:**
- Status cells:
  - `Invited · expires Oct 10`, in red text when `inviteExpiresAt` is past
  - `Invited · no code yet` when `inviteExpiresAt` is null
  - `Active · @<username>`
  - `Archived`
  - Date format `MMM d`, viewer-local.
- Invite dialog:
  - Title `Invite code for <name>`
  - Code in monospace with a `Copy` button
  - `Valid once, until <MMM d>.` / `You won't be able to see this code again.`
  - Instruction text `Install Bid Track → click the icon → Register → enter this code and choose a username and password.`
  - A second button `Copy instructions + code`, which copies `Bid Track invite for <name>: <code> (valid until <MMM d>). Install Bid Track → click the icon → Register → enter this code and choose a username and password.`
  - Toast `Copied` on each copy.
- Reset confirm: title `Reset login`, body `Reset <name>'s login? Their extension stops uploading until they register again with the new code. Their folder and bid history stay.`, confirm label `Reset login`, tone `danger`.

- [ ] **Step 1: Implement endpoints and types**, then run `npx tsc --noEmit -p .` → errors only where the pages haven't been updated yet, plus the 2 known `InterviewPrepLibrary.tsx` errors.
- [ ] **Step 2: Implement `InviteDialog`, the form and page changes.**
- [ ] **Step 3: Verify** — `npx tsc --noEmit -p .` → only the 2 known `InterviewPrepLibrary.tsx` errors. `npx vite build` → `built in`.
- [ ] **Step 4: Commit** — `feat(bidders): invite codes, login status and reset login`.

---

### Task 8: Extension — git, API client, signed uploads, cleanup

**Files** (paths relative to `Engineer/Bid Track (BackBlaze)`):
- Init: `git init -b main`.
  - `.gitignore` with `scripts/node_modules/`, `*.zip`, `.DS_Store`.
  - First commit `chore: import Bid Track 2.5.1` containing the current files as they are (excluding `scripts/node_modules`).
- Create: `lib/api.js`
- Modify: `background.js` (`capturePage` upload block, the `TEST_B2_UPLOAD` / `B2_SETTINGS_CHANGED` handlers, an `onInstalled` cleanup), `lib/storage.js` (drop `b2*` defaults and the `b2ApplicationKeySecret` merge), `options/options.html`, `options/options.js`, `options/options.css` if selectors become unused, `manifest.json` (version `3.0.0`, name `Bid Track`, remove `side_panel` and the `sidePanel` permission), `README.md` (install, register, Server URL; no Backblaze settings)
- Delete: `lib/b2-upload.js`, `lib/b2-s3-upload.js`, `lib/aws4fetch.mjs`, `lib/b2-credentials.js`, `lib/b2-path.js`, `scripts/`

**Interfaces** (`lib/api.js`, ES module):
- `DEFAULT_SERVER_URL = "https://engineersbackend-production.up.railway.app"`
- `getServerUrl(): Promise<string>`, `setServerUrl(url: string): Promise<void>` (storage key `serverUrl`; trailing `/` trimmed; blank → default)
- `getAuth(): Promise<{token: string, bidder: object} | null>` (storage keys `authToken`, `authBidder`)
- `register(inviteCode, username, password)` and `login(username, password)`. Each returns `bidder`, stores token and bidder, and clears `authNotice`.
- `logout(): Promise<void>`
- `me(): Promise<object>` (refreshes `authBidder`)
- `getUploadUrl({job, step, trigger, capturedAt}): Promise<{url, key, headers}>`
- `class ApiError extends Error { status }`. The message is the API's `detail` string when it is a string, else the first `detail[].msg`, else `HTTP <status>`.
- On any 401 from an authenticated call: remove `authToken` / `authBidder`, set `authNotice = "Your login was reset — ask your manager for a new invite code."`, and throw `ApiError`.
- `localIsoWithOffset(date: Date): string`, e.g. `2026-10-03T11:29:07-03:00`

**Capture block** (replaces `buildScreenshotObjectKey` + `uploadPngToB2` in `capturePage`):
1. Run only when `getAuth()` is non-null.
2. `getUploadUrl({job: <last "/" segment of record.downloadFolder>, step, trigger, capturedAt: localIsoWithOffset(capturedAt)})`
3. `fetch(url, {method: "PUT", headers, body: <PNG bytes from dataUrl>})`
4. On success, `b2FileName = key`. On any failure, `b2Error = <message>`.
- Not logged in: `b2FileName = ""`, `b2Error = "not logged in"`, and the content script's toast reads `Saved locally — log in to Bid Track so this bid counts.` when `result.b2Upload?.error === "not logged in"`.
- `onInstalled` with `reason === "update"`:
  - remove storage key `b2ApplicationKeySecret`;
  - remove every `b2*` property from the stored `settings` object.

- [ ] **Step 1: Git-init and the baseline commit**, as above.
- [ ] **Step 2: Implement `lib/api.js`, the capture block, the cleanup and the deletions.**
  - Options page: delete the Backblaze `<section>` and its handlers.
  - Add `Advanced → Server URL` (input + Save), using `getServerUrl` / `setServerUrl`.
- [ ] **Step 3: Verify syntax and dead references.**
  - `node --check background.js lib/api.js lib/storage.js options/options.js content/content.js` → no output.
  - `grep -rn "b2-upload\|b2-s3-upload\|aws4fetch\|b2-credentials\|b2-path\|TEST_B2_UPLOAD\|B2_SETTINGS_CHANGED" --include=*.js --include=*.html .` → no matches.
- [ ] **Step 4: Commit** — `feat: upload through the Engineers backend instead of Backblaze keys`.

---

### Task 9: Extension — popup register / log in / log out

**Files:** `popup/popup.html`, `popup/popup.js`, `popup/popup.css`

**Behaviour:**
- **Logged out** (`getAuth()` null): an `#auth` section with tabs `Register` and `Log in`.
  - Register: fields `Invite code`, `Username`, `Password`, button `Register`.
  - Log in: fields `Username`, `Password`, button `Log in`.
  - The `ApiError` message appears under the form.
  - If `authNotice` is set, show it above the tabs. Clear it once a login succeeds.
  - The existing status and record sections are hidden.
- **Logged in:** a header line `<name> · <profileName or "No profile"> · uploading to <folder>/`, a `Log out` button, and the existing sections.
  - Call `me()` on open to refresh the header. A 401 switches to the logged-out view, which then shows the notice.
- The title changes from `Bid Track Local` to `Bid Track`.

- [ ] **Step 1: Implement.**
- [ ] **Step 2: Verify** — `node --check popup/popup.js` → no output.
- [ ] **Step 3: Manual check against the local backend.** Load unpacked, then set Options → Server URL to `http://localhost:8082`; the backend on :8082 runs from `feat/bidder-login`.
  - Register with a code from the app.
  - Log out.
  - Log in.
  - After **Reset login** in the app, reopening the popup shows the notice.
  - Record the results in the task report.
- [ ] **Step 4: Commit** — `feat: register and log in from the popup`.

---

### Task 10: End-to-end validation (user-gated)

Run only with the user's go-ahead. It writes one object to the real bucket and posts once to Slack.

**Prerequisite:** a Backblaze key with `listBuckets`, `listFiles` and `writeFiles` on the **whole** `bid-screenshots` bucket in the backend `.env`. The current `puma-uploader` key can only reach `puma/`.

- [ ] **Step 1:**
  - Start the backend from `feat/bidder-login` on :8082 (detached, `JOB_APPLY_WORKER_DISABLED=1 RESUME_QUEUE_DISABLED=1 FRONTEND_ORIGIN=http://localhost:3001`).
  - Start the app from `feat/bidder-login` on :3001 (`VITE_API_BASE_URL=http://localhost:8082`).
- [ ] **Step 2:** Create bidder `ZZ E2E Test` → folder `zz-e2e-test`. In the extension, register with its code and capture one real job page.
- [ ] **Step 3:** Confirm `zz-e2e-test/<date>/<profile>/<job>/step1_step_….png` exists, using the read-only `validate_b2.py` from the scratchpad. Confirm Today = 1 on the Bidders page.
- [ ] **Step 4:** Reset login, then capture again → the toast shows the upload error and the popup shows the reset notice.
- [ ] **Step 5:** Send one `[TEST]` daily post with the scratchpad `slack_test_post.py`, pointed at the test bidder's folder.
- [ ] **Step 6:** Delete the test object and archive the test bidder. Report the results to the user.
