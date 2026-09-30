# Hestia: Fyers as a minute-candle source, with Angel One fallback

**Status (2026-09-30): PLANNING ONLY. Nothing is built or changed.** This document is for review and revision before any code is written. Sections marked *open decision* need an answer from the strategy owner. Related history: `plans/fyers-mcx-data-integration.md` (validation, backfill, and the finding that headless Fyers auth is impossible under the SEBI framework), `hestia_core/README.md` (Hestia's own data path).

## 1. Goal and constraint

Reduce Hestia's dependence on Angel One's `getCandleData`, which produces the AB1021 refusals, exhausted retry bursts and occasional gaps that block seeding (for example the missing GOLDPETAL 21:15 bar on 2026-09-29). When a fresh Fyers token is available on Delos that day, query Fyers for the one-minute candles and fall back to Angel One on any failure. When the token is stale or absent, use Angel One alone. Angel One order execution, the WebSocket feed and LTP polling are untouched.

The hard constraint: Fyers cannot be re-authenticated unattended (`plans/fyers-mcx-data-integration.md` section 3.6: `send_login_otp_v2` returns -1025 and `validate-refresh-token` is disabled for SEBI compliance). The access token expires at midnight IST whatever time it was issued. So a Fyers token exists on Delos only on days the owner logs in manually and copies it across. Fyers is therefore an opportunistic layer, and the system must behave exactly as it does today, with no degradation, on every day without a token.

## 2. What exists today

- **The seam.** Every candle call goes through `fetch_one_minute_window` (live polling, one window per token per minute, an inner burst of 5 attempts, a failed burst queued for recovery) and `fetch_history` (multi-day backfill, 2-day chunks) in `hestia_core/candle_fetch.py`. Both take the Angel One `gateway`, which enforces the 3-calls-a-second account-wide budget shared with Prometheus's live orders.
- **Merging.** `LiveData` keeps a per-token intraday cache (`hestia_data/cache`, keep-first merge), drops the still-forming minute (`_closed_minutes`), builds 15-minute bars, and refuses to seed over gaps (`find_gaps`).
- **Fyers plumbing.** A symbol-master resolver (`data_pipeline/data_downloader_fyers_mcx.py`), a working regular History API call (`fyers_st_probe/fyers_st_probe.py`: `GET https://api-t1.fyers.in/data/history`, epoch `range_from`/`range_to`, `cont_flag=0`), and the 2026-09-15 validation that Fyers minute bars match Angel One's on the agreed OHLC criteria, with a known convention difference (Fyers emits zero-volume placeholder bars).
- **The backtest already prefers Fyers data.** The parity backtests blend Fyers (preferred) and Angel One, so live candles from Fyers would sit closer to what the strategies were calibrated on.
- **Never done:** the multi-day live reliability run of the Fyers live endpoint (`plans/fyers-mcx-data-integration.md` section 3.4 built the probe and only smoke-tested it). The real failure rate, latency after a minute closes, and behaviour under Hestia's exact cadence are unknown.

## 3. Design

### 3.1 The CandleSource seam

A new `hestia_core/candle_sources.py` with one small interface (fetch a window of one-minute candles for a token, fetch history) and three implementations:

- **AngelCandleSource:** wraps today's `gateway.candles` call and the existing 5-attempt burst, unchanged.
- **FyersCandleSource:** calls the History API over HTTP with a short timeout (about 3 seconds) so a slow Fyers never delays a bar boundary. It resolves each Angel contract to a Fyers symbol (`MCX:<UNDERLYING><YY><MON>FUT`) through the symbol master, cached per day; a token it cannot resolve is served by Angel One. It has its own rate limiter and never touches the Angel One gateway budget.
- **SmartCandleSource:** Fyers first when the token gate passes, Angel One otherwise or on any failure. `fetch_one_minute_window` and `fetch_history` take a source instead of a gateway.

A useful side effect: on good days Angel One's candle budget is barely used, which leaves more headroom for Prometheus's LTP and order traffic.

### 3.2 The token gate (the "smart check")

Evaluated on every fetch, as a cheap stat plus cached parse, so a token that arrives mid-session starts being used at the next poll and one that expires stops being used.

- **Storage.** A dedicated `hestia_data/fyers_token.json` holding only `{app_id, access_token, issued_at}` (mode 600), not `data/user_credentials.csv`. Delos needs nothing else from the Fyers credentials.
- **Fresh means both** the file's modification date and the embedded `issued_at` date equal today's date in IST. The embedded stamp guards against `scp`/`rsync` preserving a laptop mtime or a timezone slip; the mtime check is the one requested. *Open decision:* keep both, or mtime only.
- **Circuit breaker.** An authentication failure (HTTP 401/403, or Fyers code -16 or similar) switches Fyers off for that token and every other, until the token file changes. Transient failures (timeout, 5xx, empty body, rate limit) fall back only for that window and do not trip the breaker.
- **Result acceptance.** A Fyers result counts only if it contains the expected latest closed minute. A stale or lagging Fyers response is treated as a failure and Angel One fills in. Windows may be filled by both sources; the existing keep-first cache decides.

### 3.3 Merge policy

No schema change. The first source to supply a closed minute wins, exactly as the cache behaves today. Source, latency and failure counts are logged and counted, not stored in the cache files. A mixed-source series is acceptable only if the two sources agree closely enough that the Supertrend does not change; that is what phases 0 and 1 measure.

### 3.4 Control

- **Config:** `hestia_config.CANDLE_SOURCE = dict(mode=..., instruments=[...])`, default `mode='angel'` (no behaviour change), overridable per host through `TRADING_HOSTS` the way engines are. Modes: `angel`, `shadow`, `rescue`, `smart`.
- **Kill switch:** a flag file in `hestia_data/flags/` that turns Fyers off at the next poll without a restart.
- **Visibility:** `python hestia.py --check` gains a line (`fyers token: fresh, issued 09:41 today` or `stale: Angel One only`). State changes (fresh to stale, breaker tripped, breaker cleared) are logged once and alerted at info level, never per poll.

## 4. Logistics

- **Daily login.** A laptop script (`data_pipeline/fyers_daily_login.py`) prints the Fyers login URL, takes the pasted redirect URL or auth code, runs the code-exchange step of the existing flow (`validate-authcode`), writes the token file locally, and copies it to Delos. The copy is a plain `scp` that the owner runs, so it goes through the owner's own permissions, not the assistant's. It takes roughly 30 seconds, and the weekly equities downloader needs the same token, so it replaces a step that already exists. Fyers helps only on days this is done.
- **Token timing.** Hestia starts at 08:55, so a token pushed before then is used from the first seed. A token pushed later is picked up at the next poll.
- **Security.** The current Fyers app has order-placement scope (`plans/fyers-mcx-data-integration.md` section 2.1), so a token on Delos could place orders. Mitigations: mode 600, never logged (a test fails if the token string appears in any log output, the SmartAPI logger precedent), and the source code hard-codes only the History endpoint. *Open decision:* create a second Fyers app with data-only scope for Delos, which removes the risk at the cost of a one-time app creation and consent, or reuse the current token.
- **No new cron, no crontab change, no repo secrets.** `hestia_data/` is already gitignored.

## 5. Failure handling

| Situation | Behaviour |
|---|---|
| No token file, or dated before today | Angel One only, exactly as today |
| Token expires or is revoked mid-session (401/403/-16) | Breaker trips, Angel One only until the file changes |
| Timeout, 5xx, empty response, rate limit | Fall back to Angel One for that window; do not trip the breaker |
| Fyers returns without the expected latest minute | Treated as failed for that window, Angel One fills in |
| Symbol not resolvable for a contract | That token is served by Angel One |
| Both sources fail | Same recovery queue as today |

## 6. Testing

- Unit tests for the token gate: fresh, stale, absent, mtime versus `issued_at` disagreement, timezone edges around midnight IST, token replaced mid-session, breaker trip and reset.
- Unit tests for SmartCandleSource over fake sources: every row of the failure table, plus both sources partially supplying a window.
- Each behaviour mutation-tested (break it, confirm a test fails, restore), per repo convention.
- A test that a token string never appears in captured log output.
- A replay comparison of Fyers-sourced versus Angel-sourced one-minute data through the Supertrend, requiring identical 15-minute flips.
- Full suite before each phase.

## 7. Rollout

| Phase | What | Exit gate |
|---|---|---|
| 0 | Verify offline: symbol resolution for CRUDEOILM, SILVERMIC, GOLDPETAL and NATGASMINI live contracts; whether Fyers returns the forming bar; zero-volume placeholder effect on 15-minute bars; documented rate limits | Fyers candles match Angel One's on OHLC for every instrument; placeholder handling understood |
| 1 | **Shadow.** Angel One remains the source of truth. When the token is fresh, Fyers is also queried each minute and per-minute diffs, latency and failures are written to a CSV. No decision is affected | Several sessions: at least 99% of minutes match, identical 15-minute Supertrend flips, failure and latency rates acceptable |
| 2 | **Rescue.** Angel One first; Fyers is tried only after Angel One's burst is exhausted, so Fyers can only add data | A week with no bad data and visible benefit (fewer deferred windows) |
| 3 | **Smart** (Fyers first) on paper-only instruments. GOLDPETAL and NATGASMINI first. SILVERMIC is not a safe pilot once Selene is live (2026-10-01) | 1 to 2 weeks clean |
| 4 | Smart on SILVERMIC, then CRUDEOILM last, since it feeds Prometheus, the live strategy | Owner sign-off |

Rollback at any phase: set `mode='angel'` and restart (restarts are unrestricted), or drop the kill-switch flag for an immediate effect. Each phase ships as its own commit with tests, and the plan and `hestia_core/README.md` are updated in the same commit as the code, per repo convention.

## 8. Risks and things not yet verified

- The reliability of Fyers's live History endpoint under Hestia's cadence has never been measured (phase 1 measures it).
- Whether Fyers returns the currently forming minute, and how quickly a minute becomes available after it closes, are unknown. The existing `_closed_minutes` drops the forming minute either way, but a slow Fyers would lose the race to Angel One and add latency for nothing.
- Zero-volume placeholder bars from Fyers make windows look complete where Angel One's would be partial. That is probably good, but it changes what "complete" means for `find_gaps` and the deferred-bar logic, so it needs an explicit decision.
- Mixed-source series: if the two sources disagree by even a tick on a boundary bar, a Supertrend flip could differ from a pure-Angel run. Phase 1 exists to bound this.
- Coverage depends on the owner logging in daily. On days without a token the benefit is zero, by design.
- Fyers could change or disable the History endpoint or its access rules, as it did with refresh tokens. The fallback design contains this, but it should be expected.

## 9. Open decisions

1. Data-only Fyers app for Delos, or reuse the current order-scoped token?
2. Sequence: shadow, then rescue, then smart (recommended), or straight to smart?
3. Is a daily manual login acceptable, knowing coverage follows the days it is done?
4. Should seeding and backfill (`fetch_history`, and gap-fill at start-up) also try Fyers first? It would have helped the 2026-09-29 missing-bar seed failure, but it widens the change.
5. Keep both the mtime check and the embedded `issued_at`, or mtime only?
6. Which instruments pilot in phase 3, given SILVERMIC goes live tomorrow?
