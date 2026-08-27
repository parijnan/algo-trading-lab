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

**Cron schedule**: MCX session runs 09:00–~23:30 (variable close, see the drawdown-analysis EOD discussion — most days' last real bar is 23:15, some run to 23:45). Cron entry starts ahead of 09:00, independent of Leto's 09:15 NSE-hours start.

---

## 1. Supertrend seeding

**Decision: maintain the running CRUDEOILM CSV (already exists — `data_downloader_mcx.py`'s incremental per-contract files, stitched by `load_futures_1min`), tail-read it each morning for past days, live-poll for today, gap-check before trusting the seed, and never persist/resume the ST series itself across restarts.**

This directly follows Iris's Path A / Path B split (`iris_functions.py:275-347`), adapted for Prometheus's simpler single-timeframe signal:

- **Past days (Path A equivalent)**: tail-read the maintained CRUDEOILM 1-min CSVs — not a full read, mirroring `_tail_read_nifty_csv`'s measured 68x speedup (`iris_functions.py:254-272`). Reuse `load_futures_1min`/`resample_ohlcv` from `prometheus_backtest/phase2/data_loader_p2.py` for the actual stitching/resampling logic (already handles the multi-contract-file concatenation this session validated repeatedly) rather than reimplementing candle assembly from scratch.
- **Today (Path B equivalent)**: always a live `getCandleData` poll from market open to now — today's data won't be in the on-disk CSV until the overnight cron run. Blocking retry here is fine (pre-loop, same justification as Iris: `CANDLE_FETCH_RETRIES=5`, `CANDLE_FETCH_RETRY_INTERVAL=10s`, same values as Iris's proven hardened config — no evidence yet to justify different numbers for Prometheus).
- **Gap check before seeding**: refuse to seed (loud failure, not silent) if the reconstructed 15-min series isn't contiguous — same principle as `_find_5m_gaps`.
- **Never resume a persisted ST series.** `compute_st` (already built, reused throughout this session's backtesting) always recomputes from scratch over the full reconstructed history — this is already true of the existing code, and the production seed must preserve it. Iris's own docstring is explicit about why: *"the Supertrend ratchet path is history-dependent and resuming mid-stream would silently diverge from a from-scratch computation."* A human-readable seed dump (`prometheus_15m_series.csv`, mirroring `iris_15m_series.csv`) is fine to write for debugging, but must never be read back as authoritative.

**Contract-roll wrinkle — Iris doesn't have this, Prometheus does.** Iris's underlying (Nifty index) has one continuous series forever; Prometheus's CRUDEOILM is a chain of expiring futures contracts. The tail-read must cross the roll boundary correctly — pulling from the *old* contract file into the *new* one exactly as `load_futures_1min` already does for backtesting. This session directly observed the practical edge of this: right after a roll, the new contract file can have almost no standalone history (the September roll gave the new contract only that day's ticks). The seed logic must tail across files, not assume the current front-month file alone has enough history — reuse the existing stitching function, don't rebuild it MCX-naively.

**Seed window size**: Iris uses `SEED_DAYS=13` calendar days on 5-min bars (~195 bars/day × 13 ≈ 2,535 bars), sized generously beyond bare `ST_PERIOD` convergence. Prometheus's 15-min bars carry ~65 bars/trading day (09:00–23:30 minus the resample-per-day boundary). To land in the same generosity band as Iris (not just past the NaN warmup, but past where the ratchet path has actually settled), **recommend 15–20 calendar days** (≈975–1,300 fifteen-min bars) rather than a bare `ST_PERIOD=10`-derived minimum.

**Startup failure handling — stricter than Iris, deliberately.** Iris tolerates seeding with past-days-only data because it's a morning-dominant strategy (69.8% of its trend edge concentrates in the first 15 minutes) and the 2026-08-10 incident's stale-regime window was brief and self-correcting. Prometheus has no such "morning dominance" excuse — it can fire a fresh entry at any point in the session, on a signal that could be wrong if seeded stale. **Recommend: refuse to enter watching-for-signals mode until today's live poll (Path B) has confirmed data, rather than Iris's "seed anyway, alert loudly" fallback.** If Path B fails all retries, hold in a non-trading state and keep retrying in the background (same non-blocking pattern as §3) rather than trading on a seed that might be missing today's regime entirely.

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

## 3. Resilient 15-min candle polling

**Two-layer retry, directly ported from Iris's 2026-08-10 hardening** (`iris.py:410-487`, `plans/iris-signal-pipeline-hardening.md` §1):

1. **Inner burst** (already inside whatever `fetch_candles`-equivalent wraps `getCandleData`): 3 attempts, 1s apart.
2. **Outer, non-blocking, live-loop retry**: on failure beyond the inner burst, set `_next_candle_retry_at = now + CANDLE_FETCH_RETRY_INTERVAL` (10s, matching Iris) and **fall through to the loop's normal cycle** rather than blocking. This is the core hardening lesson from the referenced incident — blocking here would freeze exit-condition checks for however long the retry burns, which is unacceptable once a Prometheus position is open (SL/target/EOD checks are LTP-driven and must keep running regardless of whether a fresh 15-min candle is available).
3. **Missed-candle recovery**: same `_missed_candle_ts_list` pattern — a bar that exhausts all outer retries goes into a pending-recovery list, retried on every subsequent successful fetch, and merged into history with a **re-sort before recomputing ST** (a plain append would corrupt the ratchet for an out-of-order late arrival). **Same deliberate rule carries over: a recovered/late-arriving flip is never acted on** — only the current live bar's flip drives entries/exits, a flip discovered several minutes late is stale by the time it's found.
4. **Rate limiting**: reuse the same self-healing per-second bucket (`CANDLE_POLL_LIMIT=3`, matching Apollo/Athena/Iris) — this is a broker-account-wide constraint, not signal-specific, no reason for Prometheus to use a different value.
5. **Slack escalation**: same three-tier pattern — first failure → warning to `#error-alerts`, recovery → info to the same channel, retries exhausted → error-level alert. No new channel needed.

One difference worth naming explicitly: Iris's exit conditions are entirely LTP-driven once in a trade (option premium %, not candle-driven), and its entry conditions need a fresh 5-min candle. Prometheus is the same shape — SL/target1/target2/EOD are LTP-driven, but *entries* and *trend-flip exits* both need a fresh, confirmed-closed 15-min bar. A stalled candle feed delays new entries and delays trend-flip-driven exits, but never delays SL/target/EOD, which should be checked every loop tick off the live LTP regardless of candle-fetch state. The polling loop's structure should make this separation explicit, not implicit — candle-dependent logic and LTP-dependent logic should be two clearly separate blocks in the main loop, mirroring the priority order already established in `backtest_p2.py` (EOD → SL → lot1 target → lot2 target → trend-flip, in that order, stop-before-target on same-bar ties).

---

## 4. State file, running trade log, crash recovery

**State schema — closer to Athena's shape than Iris's**, since Prometheus tracks two independently-timed lots (much more like Athena's per-leg CE/PE state than Iris's single-position 12-field state). Proposed `PrometheusState` fields, mirroring `backtest_p2.py`'s `TradeState` dataclass directly (deliberate — the live state should be recognizable against the already-proven backtest state shape):

```
status              # watching | in_trade
direction           # bullish | bearish
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

**Running trade log — follow Athena's pattern, not Iris's (Iris has none — confirmed gap).** Athena writes one CSV per trade (`TRADE_LOGS_DIR/trade_{entry_dt:%Y-%m-%d_%H%M}.csv`), appending a row on **every polling cycle** while in-trade (not just entry/exit), showing running unrealized P&L build up, with the final row stamped with `exit_reason`. This is a genuine "running" log, not a summary. **For Prometheus, mirror the backtest's own per-trade log schema directly** — `trade_paths_p2.py` already produces `lot1_pnl_points/rs`, `lot2_pnl_points/rs`, `total_pnl_points/rs`, `running_mae`, `running_mfe` per bar for every backtested trade. Using the *same* column shape live (one row per live poll cycle instead of one row per historical 1-min bar) makes live-vs-backtest comparison a direct diff rather than a translation exercise — this reuse is a deliberate design win worth calling out, not incidental.

**Paths**: `prometheus_production/data/prometheus_state.csv`, `prometheus_active.flag`, `prometheus.pid`, `data/trade_logs/` — all gitignored, matching every other `_production/` folder's convention exactly.

---

## 5. Slack reporting

**Reuse the existing four-channel convention and Iris's message-style conventions directly** — no new channels needed, this is well-established infrastructure:

```python
SLACK_TRADEBOT_CHANNEL = "#tradebot-updates"   # session lifecycle: login, logout, WS status, seed complete
SLACK_TRADE_ALERTS     = "#trade-alerts"        # entries, exits, SL hits
SLACK_TRADE_UPDATES    = "#trade-updates"       # periodic in-trade P&L updates
SLACK_ERRORS_CHANNEL   = "#error-alerts"        # exceptions, feed/candle failures
```

- **Session lifecycle** → `#tradebot-updates`: startup, seed-complete (with the seeded 15-min trend, mirroring Iris's "✅ Supertrend seeded (N bars). 15m trend: X" message), shutdown/teardown. Guardian-check refusal (another strategy active) should also post here.
- **Entry/exit** → `#trade-alerts`, styled to carry Prometheus's two-lot shape (Iris's single-position message doesn't translate directly — needs both lots' targets/SL on entry, and separate lot1/lot2 exit messages plus one combined summary once both close): entry message states direction, entry price, `sl_price`, `lot1_target`, `lot2_target` (and its source — pivot/flat/no-pivot-fallback); each lot's exit message states which lot, exit reason, exit price, P&L; a final combined-total message once both lots are closed.
- **Periodic updates** → `#trade-updates`. Iris uses a 10-second cadence — appropriate for a scalper with an average few-minute hold. Prometheus's average backtested hold is ~200 minutes (calibrated recommended config, 217-trade run). **Recommend a much longer cadence — every 5 minutes, or on a meaningful P&L delta threshold — not Iris's 10s.** A 10s cadence over a 200-minute average hold would be pure Slack noise with no informational gain; this is a Prometheus-specific tuning decision, not a straight port.
- **Errors** → `#error-alerts`: candle-fetch failures/retries/recovery (§3's escalation tiers), seed failures, WS disconnects (via `SharedFeed`'s existing `alert_callback` — no change needed there, it's already strategy-agnostic), unhandled loop exceptions.
- **`[PAPER]` prefix** on every message when running in paper/DRY_RUN mode — adopt identically, it's a simple, proven convention.
- **Fire-and-forget delivery**: Iris's `_slack()` spawns a daemon thread per message with a bare `try/except: pass` around the actual API call — a failed Slack send never raises, never blocks, never crashes the strategy loop. Reuse this exactly; no retry or rate-limit handling exists for the Slack call itself in any current strategy, and there's no reason to add it here first.
- **Emoji convention** (meaningful, not decorative, per Iris's usage): ⚡ entry, ✅ exit/success, 🔄 regime/signal flip, ⚠️ warning, 🚨 critical/error, ⏹ stop, 📊 periodic update, ℹ️ info. Reuse the same set for visual consistency across all VPS strategies' Slack output.

---

## Rollout plan

1. **Verify the order-update WebSocket works for MCX** (§2's flagged gap) — the one piece of infrastructure this plan depends on that hasn't been tested yet, unlike the LTP feed.
2. **Backtest/live parity check before any real order placement.** This repo has hit backtest/live parity bugs before (Artemis's re-entry and ELM handling) — build the live decision loop, then run it against a slice of *historical* data with the LTP/candle sources swapped for replay, and diff its trade-by-trade output against `backtest_p2.py`'s output for the identical window. Don't trust "looks right" — diff it.
3. **DRY_RUN=True paper mode first**, mirroring Iris's `DRY_RUN` flag — real signals, real Slack alerts, simulated fills, no real orders. Confirms the whole pipeline (seeding, polling, state persistence, crash recovery, Slack) under real market conditions before any capital is at risk.
4. **Static 2-lot sizing at go-live**, no dynamic sizing — matches Iris's own "static 40 lots; dynamic sizing deferred" precedent. Revisit only after live/paper track record justifies it.
5. **CRUDEOILM only at go-live**, not CRUDEOIL — matches the backtest's primary calibration target; CRUDEOIL was cross-validation only, not a day-1 production target.

## Explicitly out of scope for this plan

- Dynamic position sizing
- Points-based (`THRESHOLD_MODE='points'`) production mode — backtest keeps both modes for comparison, production hardcodes `'pct'`
- CRUDEOIL production deployment
- Multi-strategy portfolio construction across Prometheus + the NSE/BSE strategies
