# Plan: Prometheus Phase 2 — Production Architecture

**Status: design only, nothing built yet.** This plan covers taking Prometheus Phase 2 (CRUDEOILM two-lot scale-out, calibrated in `prometheus_backtest/phase2/`) from backtest to live/paper trading. Every design decision below is either a direct port of a proven pattern from Iris/Athena (cited with file:line) or an explicit deviation with its own rationale — nothing here should read as "seems reasonable," it should read as "here's the precedent, here's why we're following or diverging from it."

Calibrated config this plan builds around (from `prometheus_backtest/phase2/configs_p2.py`, set 2026-08-27, cross-validated on both CRUDEOILM and CRUDEOIL): `THRESHOLD_MODE='pct'`, `SL_PCT=1.8`, `TARGET1_PCT=1.0`, `TARGET2_MODE='flat_pct'`, `TARGET2_FLAT_PCT=2.3`, `ST_PERIOD=10`, `ST_MULTIPLIER=3.0`, `MIN_ENTRY_TIME='09:15'` (checked at fill time), `MAX_ENTRY_BEFORE_CLOSE_MIN=60`, `EOD_SQUAREOFF_BEFORE_CLOSE_MIN=15`, 2 lots per entry (1 lot each leg), rule 7 (trend-flip closes remaining lot(s) and is itself the next entry, same-bar resolution).

MCX websocket LTP feed already live-tested this session (`SmartWebSocketV2`, `MCX_FO=5`, CRUDEOILM token 565900 — real tick received, LTP 7924.0). **The order-update websocket's MCX support has NOT been tested** — that's an explicit action item in §2.

---

## 0. Process architecture — standalone, not Leto-routed

**Decision: Prometheus runs as its own top-level cron entry (`prometheus.py`), separate from `leto.py`, not one of Leto's routed strategies.**

Leto's entire routing logic (`leto.py` → `_route`) is Nifty-VIX-banded: Artemis/Athena/Iris get selected based on VIX ≤16 / 16-25 / >25. Prometheus's signal (ST_15 on CRUDEOILM) has no relationship to Nifty VIX — forcing it through Leto's router would mean either (a) inventing a fake routing rule with no signal basis, or (b) always-on regardless of VIX, which defeats the purpose of routing through Leto at all. Prometheus is a parallel, independent system: different exchange (MCX vs NSE/BSE), different underlying, no regime gating — it should be live whenever MCX is open, full stop.

What Prometheus still needs from the Leto-adjacent ecosystem, despite not being routed by it:
- **Same AngelOne account, same shared rate limits.** The mutual-exclusion guardian (`iris_functions.py:718-742`, `check_no_active_strategies()`) exists specifically because concurrent logins/sessions on the same broker account contend for the same rate-limit budget and can disrupt each other's sessions — this applies to Prometheus exactly as much as it does to Apollo/Athena/Artemis, even though it's a different exchange segment. **Action item:** extend `check_no_active_strategies()` (or wherever the equivalent lives per strategy) to also check `prometheus_production/data/prometheus_state.csv`, and give Prometheus its own equivalent check of the other four before it starts its own session. This is a small, explicit cross-cutting change, not something to leave implicit.
- Same credentials file, same Slack app/token.

**Folder structure** (mirrors `iris_production/`, `athena_production/` exactly, per the repo's established per-strategy convention):
```
prometheus_production/
  prometheus.py              # cron entry point — own login, own session, own teardown
  prometheus_configs.py      # NOT configs.py — repo convention (CLAUDE.md "Module naming")
  prometheus_state.py        # PrometheusState dataclass + load/save
  prometheus_functions.py    # order placement, fill tracking, Slack, candle fetch/retry
  prometheus_logger_setup.py
  data/                      # prometheus_state.csv, prometheus_active.flag, trade_logs/ (all gitignored)
  logs/
```

**Config duplication, not import, from the backtest** — matches the existing repo pattern exactly: `iris_production/iris_configs.py` and `iris_backtest/configs.py` are two separate files with independently-set values, never cross-imported. `prometheus_production/prometheus_configs.py` should be its own file, manually set to mirror the calibrated backtest values at go-live time, with a comment recording which backtest run/date it was calibrated from (e.g. "calibrated 2026-08-27, see prometheus_backtest/phase2/data_sweep/sweep_p2_summary.csv and plans/prometheus-phase2-production.md"). A live import dependency on the backtest module would be a novel pattern not used anywhere else in the repo, and would make production behavior silently follow future backtest experimentation — wrong direction of coupling.

**Cron schedule**: MCX session runs 09:00 to a configurable `CLOSING_TIME` (§3) — 23:30 when US DST is in force, 23:55 when it isn't; the same variability the drawdown-analysis EOD discussion already observed empirically (most days' last real bar around 23:15, some running later). Cron entry starts ahead of 09:00, independent of Leto's 09:15 NSE-hours start.

**Logging — same granularity as `mcx_live_downloader.py`, already built and proven, not a fresh Iris port.** The overnight live-downloader work this session already established the exact pattern Prometheus needs: per-attempt call-level logging (not just per-cycle outcomes), a dated file handler alongside console output (`force=True` on `logging.basicConfig()` — a real bug was hit and fixed here: a module imported earlier in the chain can silently claim the root logger first), and resilience-state visibility (pending-recovery counts, reconnect attempts). `prometheus_logger_setup.py` carries this forward directly, plus Iris-specific touches this repo already relies on: explicit state-transition logs (watching→in_trade, lot1/lot2 status changes), and critical events logged at the same point they're mirrored to Slack so the log and the Slack history never disagree about what happened. **Every log line names the instrument** (CRUDEOILM/CRUDEOIL) rather than leaving it implicit — once §6 makes the instrument Slack-switchable, a log/support investigation needs to know which contract was live at any given timestamp without cross-referencing `instrument_override.json`'s edit history.

**MCX needs its own holidays file — separate from `data/holidays.csv`.** The root `holidays.csv` (`holiday, date, day` columns, read by `leto.py`'s `_check_market_hours`-equivalent) is NSE/BSE's calendar — full-day closures only, and MCX's calendar genuinely differs (different commodity-exchange holiday list, and MCX's morning/evening split means a given date can close only *one* session rather than the whole day, which the existing schema has no way to express). **Decision: `data_pipeline/data/mcx_holidays.csv`**, its own file, checked by Prometheus's own startup/daily gate rather than reusing Leto's NSE/BSE check:

```
date, morning_session_closed, evening_session_closed, holiday_name
```

Boolean pair rather than a single flag, so a morning-only or evening-only closure is representable, not just full-day. **Populated and verified — `data_pipeline/data/mcx_holidays.csv` now exists with the full 2026 MCX calendar (17 dates), user-supplied from a broker's published list** (automated fetch was blocked: MCX's own page returned a 403 from Akamai edge protection, and Claude in Chrome's browser tool refused `mcxindia.com`/`groww.in`/`zerodha.com`/`icicidirect.com` outright as a category-level restriction on financial-site domains — manual sourcing was the only path). The schema held up exactly against the real data, no adjustment needed. **One genuine surprise worth flagging for whoever implements the check logic**: 16 of 17 dates close the *morning* session and leave evening open (the common pattern) — but 2026-01-01 (New Year's Day) is the sole exception, **morning open, evening closed**. Don't hardcode an assumption that "morning is the one that closes" — always check both booleans independently, exactly as the two-column schema requires. Location matches `data/holidays.csv`'s own convention exactly: gitignored, not tracked in git (`data/holidays.csv` itself is `git ls-files`-confirmed untracked, hand-maintained per-machine) — `data_pipeline/data/mcx_holidays.csv` is already covered by the existing blanket `data_pipeline/data/` gitignore pattern, no new entry needed.

---

## 1. Supertrend seeding

**Decision: maintain the running CRUDEOILM CSV (already exists — `data_downloader_mcx.py`'s incremental per-contract files, stitched by `load_futures_1min`), tail-read it each morning for past days, live-poll for today via the same 1-min mechanism already proven overnight (see §3), gap-check before trusting the seed, and never persist/resume the ST series itself across restarts.**

This directly follows Iris's Path A / Path B split (`iris_functions.py:275-347`), adapted for Prometheus's simpler single-timeframe signal:

- **Past days (Path A equivalent)**: tail-read the maintained CRUDEOILM 1-min CSVs — not a full read, mirroring `_tail_read_nifty_csv`'s measured 68x speedup (`iris_functions.py:254-272`). Reuse `load_futures_1min`/`resample_ohlcv` from `prometheus_backtest/phase2/data_loader_p2.py` for the actual stitching/resampling logic (already handles the multi-contract-file concatenation this session validated repeatedly) rather than reimplementing candle assembly from scratch.
- **Today (Path B equivalent)**: the same live 1-min poller from §3, started fresh each morning ahead of the 09:15 earliest-possible-signal window (see §3's timing note) — not a separate blocking pre-loop fetch. Today's bars accumulate into the exact same in-memory/on-disk series the rest of the day's polling will keep extending, so "seeding today" and "the live loop" are the same mechanism, not two.
- **Gap check before seeding**: refuse to seed (loud failure, not silent) if the reconstructed 15-min series isn't contiguous — same principle as `_find_5m_gaps`.
- **Never resume a persisted ST series.** `compute_st` (already built, reused throughout this session's backtesting) always recomputes from scratch over the full reconstructed history — this is already true of the existing code, and the production seed must preserve it. Iris's own docstring is explicit about why: *"the Supertrend ratchet path is history-dependent and resuming mid-stream would silently diverge from a from-scratch computation."* A human-readable dump of the resampled 15-min series with ST/trend columns (`prometheus_15m_series.csv`) is written for debugging (size-bounded — see §4), but must never be read back as authoritative. Note: `iris_15m_series.csv`, referenced as precedent in earlier drafts of this plan, turned out on inspection to be a one-off 229-row diagnostic dump from the 2026-08-10 hardening work, not an actively-managed production file — there's no existing size-management pattern to port for this file; §4 defines one from scratch.

**Contract-roll wrinkle — Iris doesn't have this, Prometheus does.** Iris's underlying (Nifty index) has one continuous series forever; Prometheus's CRUDEOILM is a chain of expiring futures contracts. The tail-read must cross the roll boundary correctly — pulling from the *old* contract file into the *new* one exactly as `load_futures_1min` already does for backtesting. This session directly observed the practical edge of this: right after a roll, the new contract file can have almost no standalone history (the September roll gave the new contract only that day's ticks). The seed logic must tail across files, not assume the current front-month file alone has enough history — reuse the existing stitching function, don't rebuild it MCX-naively.

**Seed window size**: Iris uses `SEED_DAYS=13` calendar days on 5-min bars (~195 bars/day × 13 ≈ 2,535 bars), sized generously beyond bare `ST_PERIOD` convergence. Prometheus's 15-min bars carry ~65 bars/trading day (09:00–23:30 minus the resample-per-day boundary). To land in the same generosity band as Iris (not just past the NaN warmup, but past where the ratchet path has actually settled), **recommend 15–20 calendar days** (≈975–1,300 fifteen-min bars) rather than a bare `ST_PERIOD=10`-derived minimum.

**Startup failure handling.** Prometheus's earliest possible signal can't fire before `MIN_ENTRY_TIME` (09:15) regardless of when the process starts — there's real slack before market open for seeding, WebSocket connect, and REST polling to all come up cleanly (unlike Iris, which has no equivalent buffer). That slack is about *time available*, not a reason to relax correctness: Prometheus has no "morning dominance" the way Iris does (69.8% of Iris's edge concentrates in its first 15 minutes, so a brief stale-seed window there is self-correcting) — Prometheus can fire a fresh entry at any point in the session, on a signal that's wrong if seeded stale. **Recommend: refuse to enter watching-for-signals mode until today's live poll (§3) has confirmed data, rather than Iris's "seed anyway, alert loudly" fallback.** If today's poll fails all retries, hold in a non-trading state and keep retrying in the background (same non-blocking pattern as §3) rather than trading on a seed that might be missing today's regime entirely.

**Data-pipeline change to support this — the post-market script becomes "up-to-date-aware."** `data_downloader_mcx.py` currently re-checks and (incrementally) re-downloads every enabled underlying on every run, including whichever instrument Prometheus is already live-feeding all day. **Recommend**: before downloading each underlying, check whether its file's last timestamp already covers that day's actual session close (not just "some data exists today") — skip with a log line if so, download normally otherwise. This is a small addition to the existing per-underlying loop, and it's instrument-agnostic by construction: it naturally skips whichever instrument is currently live-fed (CRUDEOILM today) without hardcoding a name, and if `configs.SYMBOL` is ever switched to CRUDEOIL (§0), the post-market script automatically finds *that* instrument already current and downloads CRUDEOILM (now the non-live one) instead, alongside the other ~27 commodities. Runs as a proper cron job on Delos, scheduled after MCX close — exact time should track the configurable `CLOSING_TIME` from §3 plus a safety margin, not a hardcoded clock time.

---

## 2. Order execution: market orders off own LTP, fill tracking via order-update WebSocket

**Confirmed uniform across all four current production strategies (Apollo, Athena, Artemis, Iris): SL/target/EOD/trend-flip exits are never resting limit orders. They're LTP-monitored conditions that fire a MARKET order when triggered.** `iris_functions.py:563-596` and `athena_engine.py:250` both hardcode `'ordertype': 'MARKET'`, `'triggerprice': '0'`. This is `plans/websocket-order-updates.md`'s implemented design, not a proposal — **Prometheus should follow the identical pattern**: watch the live LTP feed (already validated for MCX this session), and when a bar/tick crosses `lot1_target`/`lot2_target`/`sl_price`, or the EOD/trend-flip clock-and-signal conditions fire, place a market order immediately rather than resting a limit order on the exchange.

Why not resting limit orders: no existing strategy in this repo does it, and the backtest's own fill-price convention (`_target_fill_price`/`_stop_fill_price` in `backtest_p2.py`) already models "detect the breach, fill at the threshold or worse on a gap" — which is exactly what a market order fired off a live LTP breach produces in practice, not what a resting limit order would produce (a limit order either fills exactly at the limit or doesn't fill at all, and can miss a fast-moving breach entirely if price gaps through). Backtest/live parity is stronger with the market-order model.

**Fill tracking: the order-update WebSocket, not the LTP feed, not the `individual_order_details()` REST endpoint.**
- Angel One exposes a *separate* WebSocket (`SmartApi.smartWebSocketOrderUpdate.SmartWebSocketOrderUpdate`) distinct from the LTP feed (`SmartWebSocketV2`, `websocket_feed.py`) already validated for MCX. **This is unverified for MCX and must be tested before go-live** — same style of test as the LTP check already run this session (connect, place a small test order or use a paper/DRY_RUN order if the environment supports it, confirm an `AB05` event arrives for an MCX order).
- Reuse `OrderFillWatcher` (`iris_functions.py:476-556`, identically implemented in Athena) as-is — it's already strategy-agnostic. Key gotchas already discovered and fixed there, don't rediscover them: `AB00` (connection ack) arrives on `on_data` not `on_message`; `AB09` (after-market-delete) can fire before the real terminal `AB05` within ~1ms and must not be treated as terminal; `filledshares` is a string requiring `int()` cast; measured WS latency is 150–290ms.
- `get_fill_price()`'s pattern (WS fast path, 50ms poll, 30s budget, REST `orderBook()` fallback on WS-not-ready or timeout) is directly reusable, no strategy-specific logic needed.
- **`individual_order_details()` REST endpoint is confirmed broken** (`AB1007: Order not found` for genuinely-filled orders, both NFO and BFO — `plans/individual-order-details.md`) — don't build toward it as a fallback option; it was already tried and ruled out.

**Partial-fill handling on the 2-lot entry — new problem, not covered by the existing orphan-fill pattern.** `plans/orphan-fill-cleanup.md`'s fix addresses *multi-leg* divergence (two separate opening orders filling different amounts) — Athena's spreads place two leg orders per entry. Prometheus's entry is a *single* order for 2 lots, so classic leg-divergence doesn't apply, but a partial fill on that single order (e.g. only 1 of 2 lots fills, on illiquid MCX conditions) is a structurally analogous risk with no existing precedent to copy. **Recommend: if only 1 lot fills, treat it as an intentional single-lot trade for that instance — lot2 simply never opens, don't retry for the missing lot.** Rationale: retrying introduces its own timing risk (entry price has moved by the time a retry fills), and this session's single-lot side-project comparison already showed single-lot economics are well-understood and acceptable as a degraded mode, not a crisis. This is a judgment call, not a ported pattern — flag it as a decision point when reviewing this plan.

---

## 3. Resilient polling: 1-min REST + WS, resampled to 15-min for ST_15

**Decision (supersedes an earlier "fetch native 15-min candles" draft of this section): reuse `mcx_live_downloader.py`'s already-proven 1-min boundary-aligned poller and parallel WS SNAP_QUOTE feed as-is, and derive ST_15 by resampling the accumulating 1-min series to 15-min bars in-memory — exactly the same `resample_ohlcv` → `compute_st` pipeline the backtest already uses.** This was live-validated overnight on 2026-08-28: 471 cycles, fired within 0ms of each minute boundary, 0.84% AB1021 hit rate (all recovered on first retry), 0 pending-recovery at session end; the parallel WS thread held its connection the entire session with 0 reconnects needed and logged 29,563 SNAP_QUOTE ticks. There's no reason to build a second, separate 15-min-native polling mechanism when a harder, more granular version of the same problem is already solved and proven — resampling in-memory is strictly less work than a fresh candle-fetch design, and it's the same relationship the backtest already has to its own 1-min source data.

**Two-layer retry, directly ported from `mcx_live_downloader.py` (itself ported from Iris's 2026-08-10 hardening, `iris.py:410-487`, `plans/iris-signal-pipeline-hardening.md` §1):**

1. **Inner burst**: 3 attempts, 1s apart, every attempt logged individually (not just the cycle's final outcome).
2. **Outer, non-blocking, live-loop retry**: on failure beyond the inner burst, the window goes to a pending-recovery list and the loop moves on immediately — never blocks. This is the core hardening lesson: blocking here would freeze exit-condition checks for however long the retry burns, which is unacceptable once a Prometheus position is open (SL/target/EOD checks are LTP-driven and must keep running regardless of whether a fresh candle is available).
3. **Missed-candle recovery**: pending windows retried every subsequent cycle, merged into history via dedup-and-sort (never a plain append — would corrupt the ST ratchet for an out-of-order late arrival). **Same deliberate rule carries over: a recovered/late-arriving flip is never acted on** — only the current live bar's flip drives entries/exits, a flip discovered several minutes late is stale by the time it's found.
4. **Rate limiting**: reuse the same self-healing per-second bucket (`CANDLE_POLL_LIMIT=3`, matching Apollo/Athena/Iris) — broker-account-wide constraint, not signal-specific.
5. **Slack escalation**: same three-tier pattern — first failure → warning to `#error-alerts`, recovery → info to the same channel, retries exhausted → error-level alert.
6. **Boundary-aligned, zero-buffer firing**: re-derive the sleep target from wall-clock time every cycle (no drift accumulation across a multi-hour session), fire exactly at each 1-min boundary with no artificial safety margin — resilient retry/backoff absorbs whatever doesn't succeed on the first try, exactly as validated overnight.

**Candle-dependent vs LTP-dependent logic stay two explicit blocks in the main loop**, mirroring the priority order already established in `backtest_p2.py` (EOD → SL → lot1 target → lot2 target → trend-flip, stop-before-target on same-bar ties): SL/target1/target2/EOD are LTP-driven and checked every loop tick regardless of candle-fetch state; *entries* and *trend-flip exits* both need a fresh, confirmed-closed 15-min bar (i.e. a fresh 15-min boundary reached by the resample, not just a fresh 1-min tick) and can be delayed by a stalled feed without delaying the LTP-driven checks.

**Named invariant — a prior session's last-candle flip must never become today's first entry.** Checked against the backtest before writing this rule in: `trade_summary_p2.csv` has **0 of 219 trades where `signal_ts`'s date differs from `entry_ts`'s date**, and all 26 day-open (09:15-fill) entries have their signal bar on that same day's 09:00 candle — confirming the rule already holds structurally, via two gates working together: a flip on a session's literal last bar is excluded from fresh-signal detection outright, and any flip within `MAX_ENTRY_BEFORE_CLOSE_MIN` of that day's close is rejected at signal time regardless of which bar it's on — so a late signal never survives, half-consumed, into the next session's first bar. **The live entry-detection logic must preserve both gates together, not just one** — either alone is insufficient (a bar could be the literal last one yet still >60min from an unusually early close, or vice versa on a day with a data gap near close).

**Configurable session close, with the entry cutoff derived from it — new requirement, not in earlier drafts.** MCX's closing time isn't fixed: 23:30 when US DST is in force, 23:55 when it isn't. Hardcoding either breaks twice a year. **`CLOSING_TIME` becomes a config parameter**, and the existing `MAX_ENTRY_BEFORE_CLOSE_MIN` (60 min) gate is redefined as an offset from it rather than a fixed clock time:

```
raw_cutoff       = CLOSING_TIME - MAX_ENTRY_BEFORE_CLOSE_MIN
LAST_ENTRY_TIME  = nearest_15min_boundary(raw_cutoff)
```

Entries only ever happen on 15-min bar opens, so the raw offset must snap to the grid rather than being used as a continuous cutoff. Verified against both stated scenarios: `CLOSING_TIME=23:30` → raw cutoff 22:30, already grid-aligned, `LAST_ENTRY_TIME=22:30` (60 min runway). `CLOSING_TIME=23:55` → raw cutoff 22:55, nearest 15-min boundary is 23:00 (5 min away, vs 10 min to 22:45), `LAST_ENTRY_TIME=23:00` (only 55 min runway) — both match exactly. **`EOD_SQUAREOFF_BEFORE_CLOSE_MIN` does *not* need this rounding treatment** — it's LTP-driven and checked every loop tick (not bar-gated), so `CLOSING_TIME - EOD_SQUAREOFF_BEFORE_CLOSE_MIN` is used as a plain clock-time trigger with no snapping needed. `SESSION_END_TIME` for the poller/WS thread itself (when to actually stop polling) should track `CLOSING_TIME` plus the same generous buffer `mcx_live_downloader.py` already uses, not a second hardcoded value.

---

## 4. State file, running trade log, crash recovery

**State schema — closer to Athena's shape than Iris's**, since Prometheus tracks two independently-timed lots (much more like Athena's per-leg CE/PE state than Iris's single-position 12-field state). Proposed `PrometheusState` fields, mirroring `backtest_p2.py`'s `TradeState` dataclass directly (deliberate — the live state should be recognizable against the already-proven backtest state shape):

```
status              # watching | in_trade
direction           # bullish | bearish
units               # sizing unit for this trade (1 unit = 2 lots, see §6) — persisted, not recomputed
entry_price
entry_ts
signal_ts, signal_close   # for the entry-slippage diagnostic, matching backtest
contract_expiry, symbol, token
sl_price
lot1_target, lot1_status (open/booked/stopped/eod/flip), lot1_exit_price, lot1_exit_ts, lot1_exit_reason
lot2_target, lot2_target_source, lot2_status, lot2_exit_price, lot2_exit_ts, lot2_exit_reason
last_known_ltp
last_updated        # stamped inside save_state(), matching AthenaState's convention
```

**Persist `sl_price`/`lot1_target`/`lot2_target` verbatim — do NOT recompute at restart.** This is a deliberate deviation from the repo's usual "recomputed fields don't need manual reconstruction" convention (Artemis's `option_sl`/`index_sl`, which recompute cleanly from *already-persisted* entry prices and strikes). Prometheus's targets/SL are also pure functions of `entry_price` + config percentages, so recomputing them at restart is *technically* possible — but doing so would mean a config change made between a crash and a restart silently changes the live risk parameters of an *already-open* trade, without anyone deciding that on purpose. Persisting them verbatim protects against that footgun at the cost of a few extra CSV fields. Flag this explicitly when reviewing — it's a real judgment call, not an oversight of the usual convention.

**Save trigger**: on every state-changing action (entry, lot1 exit, lot2 exit, trend-flip close+reentry) — not continuous, not every loop tick. Matches both Iris's and Athena's `save_state()` call pattern exactly.

**Crash recovery**: unconditional `load_state()` at startup (`Iris.__init__` calls it unconditionally — no separate "recovery mode" code path needed). If resuming with `status=='in_trade'`, resubscribe the LTP feed to the open position's token (`iris.py:293-296`'s pattern). **One addition beyond what Iris/Athena currently do, proposed here as a genuine gap rather than an oversight in their code**: if the crash happened between placing an exit order and confirming its fill (the OrderFillWatcher hadn't yet resolved `AB05`/`AB02`/`AB03` at crash time), the persisted state can't know whether that order actually filled on the exchange before the crash. **Recommend an explicit reconciliation step on restart when resuming `in_trade` status**: query the broker's actual open positions/order book and cross-check against the persisted state before trusting it blindly, rather than assuming the last-saved state is still accurate. Neither Iris's nor Athena's current state files have an intermediate "order in flight, fill uncertain" status to guard against this — it's a real robustness gap worth closing for Prometheus from day one rather than inheriting it silently.

**Running per-trade log — follow Athena's pattern, not Iris's (Iris has none — confirmed gap), naming per Apollo's convention.** Checked both existing conventions: Athena names its per-trade CSV purely from entry time (`trade_{entry_dt:%Y-%m-%d_%H%M}.csv`); Apollo includes the trade's own sequential ID too (`trade_{trade_id:04d}_{entry_dt:%Y-%m-%d_%H%M}.csv`, e.g. `trade_0001_2026-04-06_1230.csv`). **Prometheus follows Apollo's convention** — since the trade summary (below) already carries a `trade_id`, the filename should be derivable from either identifier, and the zero-padded ID sorts and greps more predictably than a bare timestamp. Content follows Athena's *behavior*: one CSV per trade, a row appended on **every polling cycle** while in-trade (not just entry/exit), showing running unrealized P&L build up, final row stamped with the exit reason(s) — a genuine "running" log, not a summary. **Column shape mirrors the backtest's own per-trade log directly** — `trade_paths_p2.py` already produces `lot1_pnl_points/rs`, `lot2_pnl_points/rs`, `total_pnl_points/rs`, `running_mae`, `running_mfe` per bar for every backtested trade; using the *same* columns live (one row per live poll cycle instead of one row per historical 1-min bar) makes live-vs-backtest comparison a direct diff rather than a translation exercise.

**Cumulative trade tracker — one row per completed trade, all trades, append-only.** Checked Apollo's `apollo_trades.csv` for precedent (`entry_time, exit_time, direction, expiry, buy_strike, sell_strike, option_type, buy_entry, sell_entry, buy_exit, sell_exit, net_debit, max_profit, pl_points, pl_rupees, exit_reason, entry_vix, entry_spot, max_unrealised_pl, lots`) — but Prometheus's shape is closer to the backtest's own two-lot `TradeState` than to Apollo's single-spread columns, and there's no crude-specific market-context column worth adding (WTI/USD-INR aren't tracked, and shouldn't be — Prometheus trades the instrument's own price action, not a macro view on it). **`prometheus_trades.csv` columns — identical to `trade_summary_p2.csv`, plus `units`:**

```
trade_id, contract_expiry, direction, units, entry_ts, entry_price, signal_ts, signal_close,
entry_slippage_points, sl_price, lot1_target, lot2_target, lot2_target_source,
lot1_exit_ts, lot1_exit_price, lot1_exit_reason, lot1_pnl_points, lot1_pnl_rs,
lot2_exit_ts, lot2_exit_price, lot2_exit_reason, lot2_pnl_points, lot2_pnl_rs,
total_pnl_points, total_pnl_rs
```

Same reasoning as the per-trade log: identical columns to the backtest's summary make live results diff directly against `backtest_p2.py`'s output (§ Rollout plan step 2's parity check), not a translation exercise. `trade_id` is a persistent counter across restarts (Apollo's `trade_counter.txt` pattern — a single integer file, incremented and saved on every new trade, read back at startup) rather than derived from row count, so a crash/restart never reuses or skips an ID.

**15-min ST/trend debug CSV — size-bounded, not authoritative (§1).** `prometheus_15m_series.csv`: one row per resampled 15-min bar (OHLC, `trend`, `trend_flip`, `mins_to_close`), appended as each new bar closes. **Retain the trailing 20 trading days (~1,300 bars)** — enough to see 2-3 ST regime cycles of context around the current trend without needing to open the full multi-month backing CSV, small enough to open/trim instantly. Trim-on-open: on startup, if the file exceeds the retention window, truncate to the trailing N rows before the first append of the session (not a continuous per-row check — trimming once at open is sufficient since the file only grows by ~65 rows/day). Never read back as an input to live ST computation — purely a debug view, per §1's "never resume a persisted ST series" rule.

**Paths**: `prometheus_production/data/prometheus_state.csv`, `prometheus_active.flag`, `prometheus.pid`, `prometheus_trades.csv`, `trade_counter.txt`, `prometheus_15m_series.csv`, `data/trade_logs/` — all gitignored, matching every other `_production/` folder's convention exactly.

---

## 5. Slack reporting

**Reuse the existing four-channel convention and Iris's message-style conventions directly** — no new channels needed, this is well-established infrastructure:

```python
SLACK_TRADEBOT_CHANNEL = "#tradebot-updates"   # session lifecycle: login, logout, WS status, seed complete
SLACK_TRADE_ALERTS     = "#trade-alerts"        # entries, exits, SL hits
SLACK_TRADE_UPDATES    = "#trade-updates"       # periodic in-trade P&L updates
SLACK_ERRORS_CHANNEL   = "#error-alerts"        # exceptions, feed/candle failures
```

- **Every message names the instrument explicitly — CRUDEOILM or CRUDEOIL, never left implicit.** Now that §6 makes the instrument Slack-switchable, "Prometheus" alone no longer tells an operator which contract is live; every session-lifecycle, entry/exit, periodic-update, and error message states the symbol (e.g. "⚡ *Prometheus [CRUDEOILM]* bullish entry..."), matching the same requirement placed on logging in §0.
- **Session lifecycle** → `#tradebot-updates`: startup, seed-complete (with the seeded 15-min trend, mirroring Iris's "✅ Supertrend seeded (N bars). 15m trend: X" message), shutdown/teardown. Guardian-check refusal (another strategy active) should also post here.
- **Entry/exit** → `#trade-alerts`, styled to carry Prometheus's two-lot shape (Iris's single-position message doesn't translate directly — needs both lots' targets/SL on entry, and separate lot1/lot2 exit messages plus one combined summary once both close): entry message states direction, **units** traded, entry price, `sl_price`, `lot1_target`, `lot2_target` (and its source — pivot/flat/no-pivot-fallback); each lot's exit message states which lot, exit reason, exit price, P&L; a final combined-total message once both lots are closed. Every message talks in units, not lots — a 1-unit entry is "1 unit (2 lots)" on first mention if lot count matters operationally, "1 unit" thereafter.
- **Periodic updates** → `#trade-updates`, **every 20 seconds — matching Artemis's and Athena's convention, not Iris's 10s.** Iris's 10s cadence is scalper-specific (few-minute holds); Artemis and Athena use 20s despite being weekly-hold strategies precisely because the channel is muted and cadence doesn't cost anything — **follow that convention here too**, overriding an earlier draft of this plan that recommended a much longer interval based on Prometheus's ~200-minute average hold. Consistency with the established repo-wide convention wins over a strategy-specific optimization that isn't actually needed.
- **Errors** → `#error-alerts`: candle-fetch failures/retries/recovery (§3's escalation tiers), seed failures, WS disconnects (via `SharedFeed`'s existing `alert_callback` — no change needed there, it's already strategy-agnostic), unhandled loop exceptions.
- **`[PAPER]` prefix** on every message when running in paper/DRY_RUN mode — adopt identically, it's a simple, proven convention.
- **Fire-and-forget delivery**: Iris's `_slack()` spawns a daemon thread per message with a bare `try/except: pass` around the actual API call — a failed Slack send never raises, never blocks, never crashes the strategy loop. Reuse this exactly; no retry or rate-limit handling exists for the Slack call itself in any current strategy, and there's no reason to add it here first.
- **Emoji convention** (meaningful, not decorative, per Iris's usage): ⚡ entry, ✅ exit/success, 🔄 regime/signal flip, ⚠️ warning, 🚨 critical/error, ⏹ stop, 📊 periodic update, ℹ️ info. Reuse the same set for visual consistency across all VPS strategies' Slack output.

**Interactive Slack actions — checked `slack_listener.py`'s current implementation.** Two mechanisms exist today, and they need different treatment for Prometheus:

- **Circuit breaker (`EXIT`/`KILL`/`DISABLE`)** is currently a *single global flag file* (`data/SLACK_COMMAND.flag`), read by every strategy's live loop (`iris.py:182`'s `_check_slack_commands()` pattern) — pressing the shared Kill Switch today would stop whichever of Iris/Athena/Artemis is live, all at once. **Decision: Prometheus gets its own dedicated flag file and buttons** (`data/prometheus_command.flag`, `btn_prometheus_exit`/`btn_prometheus_kill`/`btn_prometheus_disable`/`btn_prometheus_clear`), not the shared one — consistent with §0's independence argument (different exchange, no regime coupling): an operator managing the NSE/BSE side shouldn't accidentally also kill the MCX side, and vice versa. `reset_all_states()` needs a Prometheus branch added (clearing `prometheus_state.csv`) alongside its existing per-strategy resets.
- **Sizing override — Prometheus needs the same position sizer the other three strategies have, not just a config-level toggle.** (`btn_pos_sizing` modal → `sizing_override.json` per strategy, `SIZING_OVERRIDE_FILES` dict in `slack_listener.py`, one entry per folder.) Extend with a `'Prometheus': prometheus_production/data/sizing_override.json` entry and a `"Prometheus (Crude Oil)"` option in the modal's strategy dropdown. The existing modal's `dynamic`/`fixed` radio-button shape maps directly onto §6's `DYNAMIC_SIZING` boolean, and `write_sizing_override()`'s existing `(strategy, lot_calc, lot_count)` signature needs no change — Prometheus's config just reads the same `lot_count` key into its own `STATIC_UNITS`. **One real wrinkle worth deciding now, not glossed over**: today's modal has a single static "Lot Count" field label shared by all three strategy options — it's built once at `views_open` and never changes. Adding Prometheus means that label is wrong 1-of-4 times (it should read "Units" when Prometheus is selected, per §6's terminology decision). **Recommend**: add a `block_actions` handler on the strategy dropdown's `select_strategy` that calls `client.views_update` to swap the field's label between "Lot Count" and "Units" based on the current selection — a small, self-contained addition consistent with the modal's existing input-validation care, not a reason to compromise on the units/lots distinction that was deliberately introduced in §6.
- **Routing-override buttons don't apply** — Prometheus isn't Leto-routed (§0), so `btn_route_*` has no Prometheus equivalent.
- **Instrument switch — new action, Prometheus-only, no existing pattern to extend.** No other strategy in this repo trades a configurable underlying, so there's nothing to port here; this is a fresh addition. `btn_prometheus_instrument` opens a modal with two fields — **Instrument** (`static_select`, `CRUDEOILM` / `CRUDEOIL`) and **Margin per Unit** (`plain_text_input`, ₹) — submitted together, not separately, because the two are coupled: CRUDEOILM and CRUDEOIL have very different lot sizes (10 vs 100 barrels — `data_pipeline/data/mcx_instrument_master.csv`), so a margin figure that's sane for one is wildly wrong for the other, and letting an operator switch instrument without being prompted for the matching margin invites trading the wrong contract at the wrong size. Writes both fields to `prometheus_production/data/instrument_override.json` (`{"symbol": ..., "margin_per_unit": ...}`), read by `prometheus_configs.py` the same way `sizing_override.json` is (§6) — separate file from sizing override since the two are conceptually different toggles (which contract vs. how many units), even though both live in the same config-override family. The submission handler should echo back the new instrument and margin in its confirmation message to `#tradebot-updates`, and Prometheus's own startup logging (§0) should log which instrument it's running against every session regardless of whether an override is active, so a stale override is always visible rather than silently assumed.

---

## 6. Sizing: "units," dynamic vs static, and instrument configurability

**Terminology: the base sizing quantity is a "unit," not a "lot."** 1 unit = 2 lots (1 lot each leg) — the strategy as a whole trades 1 unit even though it places 2 lots with different targets. This threads through everywhere a human reads a number: config parameter names, state file (`units` field, §4), trade summary/logs (`units` column, §4), and every Slack message (§5). The broker API itself still takes lot counts — order placement converts `units × 2` to the actual lot quantity submitted, but that conversion happens at the API boundary only, never surfaces in anything human-facing.

**Dynamic sizing — boolean toggle, built now (moved in scope from an earlier draft's "out of scope" list), static at go-live (§ Rollout).** Checked how Artemis, Athena, and Apollo each compute lots from live margin:

- **Iris, Apollo, Athena** all call `self.obj.rmsLimit()['data']['availablecash']`, but Apollo/Athena additionally subtract already-committed `collateral` (`pure_cash = availablecash - collateral`) and take the `min()` of two separate lot-count estimates (by total margin, by pure cash) — because those strategies can have *other* concurrent open positions competing for the same account's margin.
- **Artemis** uses the simpler single-division form: `margin = float(self.obj.rmsLimit()['data']['availablecash']); lots = floor(margin / lot_capital)`.
- **Prometheus follows Artemis's formula, not Apollo/Athena's** — not just because it's simpler, but because the reason Apollo/Athena need the extra pure-cash constraint (guarding against another *own* open position's collateral) doesn't apply: Prometheus is the only strategy running on the MCX side of the account, so `availablecash` already reflects whatever's genuinely free.

```
DYNAMIC_SIZING     # bool config — True: compute live, False: use STATIC_UNITS
MARGIN_PER_UNIT    # Rs, config — overridable live via Slack (below), keyed to the current SYMBOL
STATIC_UNITS       # int config — manual fallback / go-live default

units = max(1, floor(rmsLimit()['data']['availablecash'] / MARGIN_PER_UNIT)) if DYNAMIC_SIZING else STATIC_UNITS
```

Computed once per fresh entry (mirroring Iris/Apollo/Athena's "only runs on a fresh trade" pattern — not recalculated mid-trade), persisted verbatim into `PrometheusState.units` (same "don't silently change an already-open trade's parameters" reasoning as §4's `sl_price`/targets decision).

**Starting configuration at go-live** (supersedes the Rollout plan's earlier general "static sizing" language with the actual values):

```
SYMBOL          = 'CRUDEOILM'
DYNAMIC_SIZING  = False
STATIC_UNITS    = 1
MARGIN_PER_UNIT = 100000   # Rs 1,00,000 — matches the backtest's own capital-basis convention
```

**Instrument configurability — switchable live via Slack, not just a config-file edit.** `configs.SYMBOL` (already the backtest's own pattern) is the production config's instrument switch, and §5 adds a dedicated `btn_prometheus_instrument` action so this doesn't require a code deploy to change — CRUDEOILM today, CRUDEOIL if ever switched. **`MARGIN_PER_UNIT` is coupled to the instrument, not a single flat constant**: CRUDEOILM and CRUDEOIL differ 10x in lot size (10 vs 100 barrels/lot), so a margin figure sane for one is wrong by roughly an order of magnitude for the other — §5's instrument-switch modal asks for both together specifically to prevent an operator switching contracts while leaving a stale, wrong-scale margin figure behind. This is the same switch §1's data-pipeline change (post-market script skipping whichever instrument is already live-fed) keys off of — flip `SYMBOL` and both the production strategy and the supporting data pipeline follow automatically, no separate configuration to update in two places.

---

## Rollout plan

1. **Verify the order-update WebSocket works for MCX** (§2's flagged gap) — the one piece of infrastructure this plan depends on that hasn't been tested yet, unlike the LTP feed.
2. **Backtest/live parity check before any real order placement.** This repo has hit backtest/live parity bugs before (Artemis's re-entry and ELM handling) — build the live decision loop, then run it against a slice of *historical* data with the LTP/candle sources swapped for replay, and diff its trade-by-trade output against `backtest_p2.py`'s output for the identical window. Don't trust "looks right" — diff it.
3. **DRY_RUN=True paper mode first**, mirroring Iris's `DRY_RUN` flag — real signals, real Slack alerts, simulated fills, no real orders. Confirms the whole pipeline (seeding, polling, state persistence, crash recovery, Slack) under real market conditions before any capital is at risk.
4. **Go-live config is §6's "Starting configuration" block exactly** (`SYMBOL='CRUDEOILM'`, `DYNAMIC_SIZING=False`, `STATIC_UNITS=1`, `MARGIN_PER_UNIT=100000`) — matches Iris's own "static 40 lots; dynamic sizing deferred" precedent for the sizing half, even though §6's dynamic-sizing mechanism and §5's instrument switch are both built and available, not deferred as code. Flip `DYNAMIC_SIZING=True` or switch `SYMBOL` only after live/paper track record justifies it, both via §5's Slack actions rather than a code deploy.

## Explicitly out of scope for this plan

- Points-based (`THRESHOLD_MODE='points'`) production mode — backtest keeps both modes for comparison, production hardcodes `'pct'`
- CRUDEOIL production deployment — the instrument switch is built (§6), flipping it isn't
- Multi-strategy portfolio construction across Prometheus + the NSE/BSE strategies
