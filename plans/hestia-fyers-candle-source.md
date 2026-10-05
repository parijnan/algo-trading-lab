# Hestia: Fyers as a minute-candle source, with Angel One fallback

**Status (2026-10-03): the daily token is automated and delivered to Delos (section 4); Phase 0, the offline verification, is done (section 7a); Phase 1 (shadow) is live on Delos since 2026-10-05 (section 7b); Phase 2 (rescue) is built and tested, enabling it is the owner's call (section 7c).** This document is for review and revision before any code is written. Sections marked *open decision* need an answer from the strategy owner. Related history: `plans/fyers-mcx-data-integration.md` (validation, backfill, and the finding that headless Fyers auth is impossible under the SEBI framework), `hestia_core/README.md` (Hestia's own data path).

## 1. Goal and constraint

Reduce Hestia's dependence on Angel One's `getCandleData`, which produces the AB1021 refusals, exhausted retry bursts and occasional gaps that block seeding (for example the missing GOLDPETAL 21:15 bar on 2026-09-29). When a fresh Fyers token is available on Delos that day, query Fyers for the one-minute candles and fall back to Angel One on any failure. When the token is stale or absent, use Angel One alone. Angel One order execution, the WebSocket feed and LTP polling are untouched.

The hard constraint: Fyers cannot be re-authenticated unattended (`plans/fyers-mcx-data-integration.md` section 3.6: `send_login_otp_v2` returns -1025 and `validate-refresh-token` is disabled for SEBI compliance). The access token expires daily at about 06:30 IST (the docs say so, and a decoded token agreed), not at midnight as this plan first assumed. Since 2026-10-03 a laptop cron job generates and delivers it (section 4), so on most days a fresh token is on Delos before Hestia's 08:55 start. It is still an opportunistic layer: the laptop has to be awake at 06:35, so the system must behave exactly as it does today, with no degradation, on every day without a fresh token.

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

- **Daily login (automated 2026-10-03; supersedes the 2026-10-02 manual skill as the daily path).** `run_fyers_auto_token.sh` (laptop, 06:35 IST, scheduled by a systemd user timer with `Persistent=true` since 2026-10-04, so a missed slot runs when the laptop is next awake) generates the day's token unattended — straight-through redirect off the persistent browser profile's live Fyers session, or a full TOTP auto-login when that session has expired — writes the token locally and to `hestia_data/fyers_token.json` on Delos (mode 600, over ssh stdin, atomically), and runs the same verify on Delos as the skill did (file mode, mtime and `issued_at` both today in IST, one live History call). Full story: `plans/fyers-auto-token.md`. The `fyers-token` skill (`.claude/skills/fyers-token/`) remains the manual recovery path when automation fails. Fyers now helps every day, with the same graceful no-token degradation when it can't. `expires_at` in the file is the next 06:30 IST (corrected 2026-10-03; it used to say midnight). Nothing on Delos reads the file until the candle source is built; its token gate will apply the same rule `fyers_token_refresh.check_token_file` already implements: mode 600, the file's mtime and `issued_at` both today in IST, and now before `expires_at`.
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
| 0 | **DONE 2026-10-03 (section 7a).** Verify offline: symbol resolution for CRUDEOILM, SILVERMIC, GOLDPETAL and NATGASMINI live contracts; whether Fyers returns the forming bar; zero-volume placeholder effect on 15-minute bars; documented rate limits | Fyers candles match Angel One's on OHLC for every instrument; placeholder handling understood |
| 1 | **Shadow.** Angel One remains the source of truth. When the token is fresh, Fyers is also queried each minute and per-minute diffs, latency and failures are written to a CSV. No decision is affected | Several sessions: at least 99% of minutes match, identical 15-minute Supertrend flips, failure and latency rates acceptable |
| 2 | **Rescue.** Angel One first; Fyers is tried only after Angel One's burst is exhausted, so Fyers can only add data | A week with no bad data and visible benefit (fewer deferred windows) |
| 3 | **Smart** (Fyers first) on **CRUDEOILM only** (Prometheus): the instrument most likely to flip (103 flips in 60 days) and so the one that gains most from faster, more reliable flip detection, and the one where Phase 1 produces the most evidence. Prometheus is live, so this phase starts only after Phase 1 and 2 have run clean | 1 to 2 weeks clean on CRUDEOILM |
| 4 | Smart on the other instruments, each only after its own shadow flip agreement meets the gate | Owner sign-off per instrument |

Rollback at any phase: set `mode='angel'` and restart (restarts are unrestricted), or drop the kill-switch flag for an immediate effect. Each phase ships as its own commit with tests, and the plan and `hestia_core/README.md` are updated in the same commit as the code, per repo convention.

## 7a. Phase 0 results (2026-10-03, offline, read-only)

Scripts: `research/fyers_mcx_validation/phase0_hestia_candles.py` (minute level) and `phase0_flip_agreement.py` (Supertrend flips). Fyers History calls only, compared with the Angel One files in `data_pipeline/data/mcx/`, using Hestia's own resampler and each engine's own Supertrend settings.

- **Symbols.** All eight live contracts (front and next, four instruments) resolve as `MCX:<UNDERLYING><YY><MON>FUT`; for example SILVERMIC30NOV26 is `MCX:SILVERMIC26NOVFUT` and NATGASMINI27OCT26 is `MCX:NATGASMINI26OCTFUT`.
- **Alignment.** Minute timestamps line up exactly: shifting either source by one minute collapses the match to near zero.
- **Coverage.** Fyers has every minute Angel One has, plus extra zero-volume placeholder minutes (about 0% to 3% of minutes: 5 for the Crude Oct contract, over 200 for some thin next-month contracts), the same placeholders found in the 2026-09-15 validation.
- **Minute-level agreement is close but not exact.** Highs and lows match about 95% of the time, opens 76% to 82%, closes 82% to 92%. The differences are mostly 1 to 2 ticks at the median, with rare large outliers (a few dozen ticks for Typhon and Prometheus, a few thousand rupees on Selene), and they do not depend on volume. Open and close differing while high and low do not is the pattern two feeds capturing slightly different first and last prints in a minute would produce. Volumes agree on the median but are rarely identical.
- **Supertrend flips, the result that matters.** Over the last 21 to 60 days on the front contracts, 168 Angel flips: 159 (95%) land on the identical 15-minute bar in Fyers, 5 (3%) are one bar off, and 4 (2%) have no counterpart. Helios 15 of 15 identical, Prometheus 99 of 103, Selene 27 of 29, Typhon 18 of 21. The trend itself disagrees on 0 bars for Helios, 14 of 2,056 for Prometheus, 8 of 780 for Selene and 14 of 780 for Typhon.
- **Reading it.** Neither source is "true": both are feed prints. They are statistically equivalent, not identical, so a flip can land a bar apart or disappear in about 5% of cases. That is the same size of gap that already exists between the live engines (Angel One) and the backtests they were calibrated on (Fyers-preferred data), so moving live to Fyers would narrow that gap rather than widen it. A series that mixes sources within a bar window (keep-first, after a fallback) should behave like either pure series to within the same tick noise.
- **Against the original exit gate** ("Fyers candles match Angel One's on OHLC for every instrument"): not met literally, because opens and closes differ by ticks. It needs restating; see decision 7.
- **Not testable offline** (markets closed): the forming-bar behaviour, how soon a closed minute becomes available, and live reliability. Phase 1 measures these.

## 7b. Phase 1 as built (2026-10-03)

Code: `hestia_core/fyers_shadow.py`, hooks in `hestia_core/live_data.py` (`begin` at the minute tick, `angel_result` when the Angel One poll for that tick is done), wiring in `hestia_core/host.py`, config in `hestia_config.CANDLE_SOURCE` (default `mode='angel'`), a `--check` line in `hestia_core/preflight.py`, and `research/fyers_mcx_validation/phase1_shadow_report.py`. Tests: `tests/test_fyers_shadow.py`, `tests/test_hestia_live_data_shadow.py`, `tests/test_hestia_host_shadow.py`, `tests/test_phase1_shadow_report.py`, plus new preflight cases; each safety property was broken on purpose and the tests caught it. Shaped by an independent review before building:

- **Measured at the tick, on both sides.** The Fyers measurement starts at the minute tick, not after the Angel One job, so its timing never includes Angel One's. The Angel One side is recorded too (attempts, exhaustion, seconds from the tick until the just-closed minute was present), because the case for Phases 2 and 3 is Fyers succeeding where Angel One exhausted and arriving no later at the boundary minutes. Boundary minutes (xx:00, 15, 30, 45) are reported separately.
- **Isolated.** Its own daemon pool (never joined at exit, so a hung call cannot delay the engine join, the drain or `terminateSession`), at least one worker per active token so retry waits never queue behind each other, a per-token in-flight guard, copies of the Angel One frames, no kernel posts, no alerts, no Slack. A 429 is never retried. The forming minute is dropped from Fyers results as it is from Angel One's.
- **Compared against what the engines acted on.** The report compares Fyers-built Supertrend against Hestia's own `15m boundary` log lines, not the pipeline files (those come from a later historical fetch) and not the intraday cache (pruned on read). The Fyers series is about 20 days of Fyers history plus the day's shadow minutes as they were seen live, because the Supertrend is path dependent.
- **Not enabled on 2026-10-05.** Monday is the first live session for Helios and Typhon and the first with zero poll stagger; three new things at once would make a problem hard to attribute. Enable by adding `'CANDLE_SOURCE': {'mode': 'shadow'}` to the `delos` entry of `TRADING_HOSTS` after Monday's session has been checked. Reversible at once: delete that entry, or touch `hestia_data/flags/fyers_off.flag` (no restart needed, stops every Fyers call at the next poll).
- **Gate for Phase 2.** The suggested gate (section 9) plus, from the availability tables: Fyers must have the just-closed minute no later than Angel One at boundary minutes in most polls, and must have it in a meaningful share of the polls where Angel One exhausted.

## 7c. What the first live day showed, and Phase 2 as built (2026-10-05)

**Phase 1 results, 09:01 to 13:15.** 880 polls per side. Fyers returned the just-closed minute on its first attempt every time, median 0.07 s; Angel One had it in 91% of polls (median 2.5 s, 90th percentile 7.7 s, worst 21.8 s). In the 78 polls where Angel One lacked the minute Fyers had it every time. Boundary-to-bar delay at the 15-minute boundaries: median 2.75 s, 90th percentile 7.7 s, but 68 to 76 s on some NATGASMINI and SILVERMIC boundaries where an Angel One burst exhausted. Retry load with zero stagger was about twice Thursday's staggered rate over the same window (7.1 against 3.8 AB1021 warnings a minute, 84 against 39 exhausted bursts); nothing stayed unrecovered.

**The caveat that shapes Phase 2: Fyers's first answer for a minute is provisional.** The recorder kept each minute's first-seen value, and 22% (NATGASMINI) to 82% (SILVERMIC) of those differed from the finalized value, mostly in the last prints of the minute (for example SILVERMIC 13:14: first-seen 107 lots and a high of 229,338, finalized close 229,572, Angel One 1,171 lots and a high of 229,596). Against Fyers's FINALIZED history, only 10 of the 68 completed 15-minute bars differed from Angel One (against 39 when the first-seen values were used), by one or two ticks except three SILVERMIC bars (13:00 close 229,531 against 229,572; 09:30 open 228,600 against 228,578; 09:45 open 228,300 against 228,290). So "Fyers has the minute 2.4 s earlier" is true of availability, not of final value. Any mode that decides on Fyers data must not trust a minute until it has settled. Measured the same afternoon with `research/fyers_mcx_validation/settle_probe.py` (10 boundaries x 4 contracts, snapshots at +0.1, 1, 2, 4, 8, 15, 30 and 60 s, each compared with the finalized value): at +0.1 s only 54% of snapshots had the final OHLC and volume (SILVERMIC 44%, NATGASMINI 100%), and at +1.0 s and every later offset 100% did (40 of 40); the just-closed minute was present in every snapshot, even at +0.1 s. So a minute settles within about a second, and a Fyers-first mode must wait roughly 1 to 2 s after the minute closes, which still beats Angel One's median of 2.75 s and removes its 70 s tail. The sample is small (one contract set, ten minutes), so Phase 3 should re-measure on more of the day.

**Fine settle probe (2026-10-05, 13:45-14:25 IST, 40 boundaries, 10 per contract, offsets +0.1 to +1.0 s).** Share of snapshots equal to the finalized OHLC and volume: +0.1 s 60%, +0.2 s 80%, +0.3 s 90%, +0.4 s 95%, +0.5 s and +0.6 s 97.5%, +0.8 s and +1.0 s 100%. CRUDEOILM is slowest (40% at +0.1 s, 100% at +0.8 s). Before +0.4 s the wrong values are almost all the close, off by a few ticks (CRUDEOILM median 4 ticks, worst 10 = 0.11%; SILVERMIC worst 41 ticks = 0.018%), and the same wrong close tends to repeat across +0.1 to +0.3 s, so repeated early pulls do not fix it. Call latency p50 122 ms, p99 360 ms, none rate-limited. Decision (user, 2026-10-05): `settle_s` is 0 for rescue; the first-poll offset for Phase 3 (a single pull near +0.8 s, or two pulls at +0.5 s and +1.0 s that must agree) is deferred until Phase 3 is coded, and observation continues.

**Phase 2 (rescue) as built.** `mode='rescue'` records everything shadow records and adds a fallback to the Angel One fetch (details in `hestia_core/README.md`): after `rescue_after_attempts` failed Angel One attempts (default 5, the whole burst), Fyers is asked for the window and may supply only minutes the engine lacks, only if the just-closed minute is in its answer and has settled `settle_s` seconds (default 0: by the time the 5-attempt Angel One burst has failed, several seconds have passed and the minute has settled; see the fine probe below). It cannot override a successful Angel One answer, and with Angel One answering normally it is never called. Limit of the default: it only helps once the burst has exhausted (several seconds in), so it removes the 68 to 76 s tail but not the typical 2 to 4 s of retries; lowering `rescue_after_attempts` rescues sooner at the cost of using Fyers for more windows. Smart mode (Fyers first, with Angel One as fallback) is Phase 3 and would remove the typical delay too, but only once settle behaviour is understood.

## 8. Risks and things not yet verified

- The reliability of Fyers's live History endpoint under Hestia's cadence has never been measured (phase 1 measures it).
- Whether Fyers returns the currently forming minute, and how quickly a minute becomes available after it closes, are unknown. The existing `_closed_minutes` drops the forming minute either way, but a slow Fyers would lose the race to Angel One and add latency for nothing.
- Zero-volume placeholder bars from Fyers make windows look complete where Angel One's would be partial. That is probably good, but it changes what "complete" means for `find_gaps` and the deferred-bar logic, so it needs an explicit decision.
- Mixed-source series: if the two sources disagree by even a tick on a boundary bar, a Supertrend flip could differ from a pure-Angel run. Phase 1 exists to bound this.
- Coverage depends on the owner logging in daily. On days without a token the benefit is zero, by design.
- Fyers could change or disable the History endpoint or its access rules, as it did with refresh tokens. The fallback design contains this, but it should be expected.

## 9. Decisions

**Resolved**
- **Token (decision 1, 2026-10-03):** reuse the current order-scoped token. No data-only app has been created. The mitigations in section 4 stand (mode 600, never logged, only the History endpoint is called).
- **Sequence (decision 2):** shadow, then rescue, then smart.
- **Seeding and backfill (decision 4):** stay on Angel One. The purpose is the live-polling phase: the AB1021 errors and the delay in detecting flips while monitoring. Seeding starts well before the open and is not where the delay matters. `fetch_history` and start-up gap-fill are not touched.
- **Pilot (decisions 6 and 7):** CRUDEOILM (multiplier 2.0), the instrument most likely to flip. Shadow still records all four instruments' active contracts, because it changes no decision and the flip-agreement figures are what later phases will be gated on. The gate suggested in section 7a (at least 95% of flips on the identical bar and none missing outright, per instrument, with acceptable failure and latency) stands unless revised after seeing live shadow data.
- **Daily login:** automated (section 4); a systemd timer with catch-up delivers the token at 06:35, or as soon as the laptop is next awake and logged in after a missed slot. Coverage is every day the laptop is on at some point, with a possibly later (mid-session) delivery.
- **Freshness rule:** file mtime and `issued_at` both today in IST, and now before `expires_at` (next 06:30 IST).

**Still open:** none that block Phase 1.
