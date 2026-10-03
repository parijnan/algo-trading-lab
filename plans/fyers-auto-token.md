# Fyers Automated Daily Token (unattended TOTP browser login)

**Status (2026-10-03): BUILT and live-proven end-to-end.** Both execution paths
ran unattended on 2026-10-03 through the exact cron wrapper (`run_fyers_auto_token.sh`):
full auto-login in 14s (client ID → Turnstile → TOTP → PIN → token) after session
expiry, plus local token write, live verification, and Delos push. Related:
`plans/fyers-mcx-data-integration.md` §2.3/§3.6 (the older "headless is blocked"
finding this supersedes), `plans/hestia-fyers-candle-source.md` §4 (the token's
consumer-side counterpart), `data_pipeline/README.md` (the operation notes).

## 1. Why this exists

Fyers access tokens die daily (~06:30 IST; docs: "expire daily at 6:30 AM") and
the SEBI retail-algo framework killed both previously-known unattended paths —
live-probed again 2026-10-02:

- **Refresh tokens**: `POST /api/v3/validate-refresh-token` → `-16 "Refresh token
  API is currently disabled to comply with SEBI regulations."` Fyers staff
  (community threads 22904, 2026-07) confirm this is permanent ("daily two
  factor authentication login is mandatory").
- **Raw-HTTP login replication**: the vagator login chain
  (`send_login_otp_v3` → `verify_otp` → `verify_pin_v2` → `api/v3/token` →
  `validate-authcode`) now requires a Cloudflare Turnstile token in a
  `fy_captcha_token` header; replaying the browser's exact request without it
  returns 401 `-1025`. No browser = no token, and no bare-urllib User-Agent
  also triggers Cloudflare 403.

The opening that makes automation legitimate: **TOTP auto-login is officially
endorsed.** Fyers staff hand out TOTP auto-login scripts to API users for
scheduled daily token generation (threads 23636, 22904, 13699, 2026). The
"not recommended" warnings apply to PIN-based headless login, not TOTP.

## 2. The account prerequisite (one-time, owner)

**External 2FA TOTP** must be active on the account:
`myaccount.fyers.in → ManageAccount → External 2FA TOTP → Enable` (scan QR with
an authenticator, confirm with a code). When active, the API login page swaps
its "OTP received on mobile/email" step for "Enter a 6-digit TOTP from your
authenticator app" — the login page's own JS switches on the server-side
`totp_enabled` flag, and the Profile API reports `totp: true`.

The secret goes in `fyers_totp_key` of `data/user_credentials.csv` (gitignored,
same file/class of secret as the Angel One TOTP in `leto.py`). Enabled and
validated 2026-10-02 (first-ever successful validation of this chain: the
2026-09-15 enablement had silently never completed — Profile reported
`totp: False` — and its key had never been validated against Fyers).

## 3. The two paths (both live-proven)

Persistent Chrome profile: `data_pipeline/data/fyers_browser_profile/`
(gitignored; it holds the Fyers web session cookie — treat as a credential,
mode 700 recommended).

1. **Straight-through (~10s, most days).** The profile's Fyers session cookie
   is alive → `generate-authcode` redirects straight to the registered redirect
   URI with a fresh `auth_code`. No login form, no Turnstile. Fyers web
   sessions persist for days-to-weeks (same mechanism the old manual
   `fyers-token` skill relied on).
2. **Full auto-login (~34s first run; 14s measured on the second real run).**
   Session expired → the script completes the login itself: client ID →
   Turnstile auto-solves in real Chrome (~1s, residential IP) → TOTP computed
   via `pyotp` (30s-window guard) → PIN → redirect with `auth_code`.

Exchange is plain HTTP (no captcha on `validate-authcode`): `appIdHash` =
SHA-256 of `app_id:app_secret`. The token is then live-verified with one
History call before anything reports success.

## 4. Gotchas found live (all handled in code, pinned in tests where testable)

- The 6-box TOTP form **auto-submits on the 6th digit** — typing must be slow
  (0.4s/box) or the input events don't register; never click the hidden Confirm
  button afterwards; poll for the PIN form instead.
- The registered redirect URI (`quant-grow.com`) is **deliberately unhosted
  with no DNS A record** — the final navigation raises `ERR_NAME_NOT_RESOLVED`
  *while carrying the auth_code in the address bar*. A navigation error is
  checked against `current_url` before being treated as retryable.
- Chrome's own DNS intermittently fails under Tailscale MagicDNS while the
  system resolver works — Fyers hosts are pre-resolved and pinned via
  `--host-resolver-rules`.
- Redirect classification is an **exact-host** predicate (`is_redirect()`),
  not a plain `startswith` (which would accept `quant-grow.com.evil.example`).
- The old repo claim "access tokens expire at midnight IST" is outdated:
  current docs say 6:30 AM, and the 2026-10-02-issued JWT decoded to an
  ~06:00–06:30 IST expiry. Either way, the cron slot only needs to be after
  expiry.

## 5. What runs where

- **Laptop** (this is deliberate — see §6): `data_pipeline/fyers_auto_token.py`
  (both paths, atomic token writes, Slack report, never logs a secret) inside
  `data_pipeline/run_fyers_auto_token.sh` (flock single-instance guard, stale
  profile-lock cleanup, Delos push over ssh stdin, remote verify), cron
  **06:35 IST daily** (after expiry, before Hestia's 08:55 start, before the
  09:15 open).
- **Writes**: `data/user_credentials.csv` (both Fyers token columns — the
  laptop downloaders' input) and `hestia_data/fyers_token.json` (the exact
  format `fyers_token_refresh.py` defines; mode 600, atomic — the Delos
  side's input), then pushes the latter to
  `delos:~/scripts/algo-trading-lab/hestia_data/` (ipv6 first, ipv4 fallback)
  and runs `fyers_token_refresh.py verify` remotely.
- **Fallbacks**: the semi-manual `fyers-token` skill stays as the manual
  recovery path when automation fails (e.g. TOTP secret rotated, profile
  corrupted). A missed day is degraded-gracefully by design — nothing on
  Delos breaks without a Fyers token (the Hestia candle-source plan's own
  constraint); the equities downloader keeps its existing manual ~weekly
  cadence and simply finds a fresh token whenever it runs.

## 6. Why the laptop, not Delos (Xvfb question, settled)

Delos would need Chrome + version-matched Chromedriver + Xvfb installed, the
profile copied over (session cookies may partially invalidate on IP change),
and — the real unknown — Turnstile behaviour from a Linode datacenter IP,
which Cloudflare distrusts far more than a residential ISP. The laptop already
has the proven environment (Chrome 154 + Selenium Manager driver, live
profile, residential IP, today's full-chain proof) and the job takes 10–35s
once a day. Delos needs only the resulting token file, which the wrapper
pushes in ~1s. Revisit Delos only if laptop-uptime at 06:35 becomes a real
constraint (the job is after-expiry but otherwise time-insensitive; any later
morning slot works equally well).

## 7. Operational notes

- **Laptop must be awake at the slot.** If asleep/off, that day's token is
  missed — tolerated by design (§5 fallbacks). If mornings prove unreliable,
  move the slot later (e.g. 08:00); the only hard constraints are after ~06:30
  expiry and before the consumer's first read (08:55 Hestia start on Delos).
- **Session-expiry cadence** is Fyers's own policy ("It has been a while since
  you signed in…" re-login prompts); measured multi-day persistence in
  practice. Each expiry just exercises path 2 — no owner action needed.
- **Secrets**: creds CSV and browser profile both gitignored; the module
  never prints tokens/auth codes (8-char fingerprints only); Slack messages
  carry only issuance/expiry/fingerprint. A test pins that no secret literal
  is embedded in the source.
- **Tests**: `tests/test_fyers_auto_token.py` (redirect predicate, TOTP
  window guard, auth-URL shape, no-secrets-in-source) +
  `tests/test_fyers_token_refresh.py` (exchange/verify/token-file; its
  `cmd_verify` staleness test now derives "stale" from real time — the
  hardcoded date collided with 2026-10-03, fixed same day).
- **Known-unrelated suite failure**: `test_helios_engine_replay.py` fails on
  this laptop (replay decisions over the AngelOne-only window with no live
  counterpart) — zero interaction with any Fyers file; investigate separately.

## 8. History of this finding (for future readers)

2026-09-15: both unattended paths found blocked (§1); a fully-manual browser
login remained the only working path; `data_pipeline/fyers_auth.py` committed
but dead. 2026-10-02: fresh research (docs spec pulled from the Redoc SPA,
login-bundle reverse-engineered, endpoints live-probed, community/staff
threads) found the vagator endpoints captcha-gated but fully scriptable in a
real browser; TOTP auto-login officially endorsed; the user's External 2FA
TOTP turned out never to have completed — re-enabled, and both paths proven
end-to-end. 2026-10-03: productionized, wrapper-proven including Delos push.
`fyers_auth.py` (raw-HTTP chain) stays in the repo as dead-but-documented
history — do not wire it into anything.
