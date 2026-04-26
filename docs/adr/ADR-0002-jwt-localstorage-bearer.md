# ADR-0002: JWT in localStorage, sent as `Authorization: Bearer`

- Status: Accepted
- Date: 2026-04-25

## Context

After the split, the SPA is hosted on Vercel and the FastAPI backend is hosted elsewhere (Render/Railway/Fly), so frontend and backend live on **different origins**. The existing Next app uses NextAuth with a same-origin session cookie, which stops being viable once the API moves off-domain.

We need a token transport that:
1. Works cross-origin without dragging in third-party-cookie complexity.
2. Lets us keep using the existing `AUTH_SECRET` so JWTs remain interchangeable during the cutover (no forced re-login).
3. Is simple enough for a small team to reason about.

Two realistic options: (a) `HttpOnly`, `Secure`, `SameSite=None` cookies issued by the API; (b) JWT returned in the login response body, stored in `localStorage`, sent on every request as `Authorization: Bearer <token>`.

## Decision

Use **option (b)**. The SPA stores the JWT in `localStorage`; an `apiFetch` wrapper adds `Authorization: Bearer <token>` to every request. CORS on the backend allowlists the Vercel origin (and `http://localhost:5173` for dev) with `allow_credentials=False`. JWTs are HS256, signed with the same `AUTH_SECRET` the Next app uses today; default expiry 12h.

## Consequences

**Positive**
- No SameSite/CSRF gymnastics; `allow_credentials=False` keeps the CORS surface narrow.
- One token store, one transport — easy to inspect in devtools, easy to mock in tests, easy to rotate by clearing one key.
- Secret continuity means existing tokens remain decodeable after cutover.

**Negative**
- `localStorage` is reachable from any script running on our origin, so an XSS bug becomes a token-theft bug. We accept this in exchange for transport simplicity. Mitigations: a strict CSP on the SPA, no `dangerouslySetInnerHTML` against untrusted input, dependency hygiene on the SPA.
- Logout is client-side only (clear `localStorage`); a stolen token stays valid until expiry. Short expiries (12h) and the option to add a server-side denylist later bound the blast radius.

**Reversible if** XSS exposure becomes a material concern — switching to `HttpOnly` cookies is a contained change (one transport layer in `apiFetch`, one CORS adjustment, one cookie-setting branch in the login route).
