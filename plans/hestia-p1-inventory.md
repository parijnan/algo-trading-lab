# Hestia P1: inventory of the Prometheus code to be split, and what it changes in the plan

**Status: DONE 2026-09-28 (read-only).** Phase P1 of `plans/selene-production.md` §10. Method: the AST outline of `prometheus.py` (3,531 lines), `prometheus_functions.py` (1,605) and `prometheus_state.py` (96), plus a line-by-line read of the parts that decide the engine/Hestia boundary: `__init__`, `_setup`, `_teardown`, the main loop `run`, `_check_command_flag`, `_handle_new_15m_bar`, `_check_exit_conditions_ltp`, `_execute_exit_lot`, `_execute_entry`, `_finalize_new_position`, `read_today_cache`, `main`. Nothing was run against a broker and nothing on Delos was touched. One measurement was made locally and one test was added (§5).

**Not done, and why.** Refining the candle-call rate with the live sessions since 2026-09-15 needs those logs from Delos (the local copies are empty), so it stays optional. Checking Angel One's published SmartAPI limits is not done; only the caps already in the repo are verified.

---

## 1. Findings that change the plan

1. **Everything in one thread.** `run()` is a single 0.5 to 1 second tick that does, in order: the command flag, three pending-retry markers, the DPL check, the LTP stop check, the 1-minute candle poll and merge, the 15-minute boundary logic with its deferred-bar cutoff, the provisional-boundary check, rollover timing, the running trade row, and the dual-tracking poll. Under Hestia the data half (poll, merge, recovery, bar building, ST, quality flags) moves to Hestia's own threads and the engine gets small, fast callbacks (§1.5 of the plan).
2. **The retry is implicit, and this must become explicit.** Today an exit that does not confirm leaves `lot*_status == 'open'`, so the next tick's stop check fires the exit again (`_execute_exit_lot`'s docstring says so: "no separate retry loop needed"). Under the agreed model the engine sends one request and Hestia retries. So the engine needs a **request-pending state**: while an exit request is in flight the engine must not re-evaluate the stop into a second request. This is new engine state, and every place that today relies on "state still open, so retry" (the stop path, Rule 7, the roll transitions, the missed-flip retry, the `EXIT` command) has to be rewritten to "send once, wait for the outcome".
3. **`save_state()` runs on every LTP tick.** `_check_exit_conditions_ltp` writes the state file whenever it sees an LTP, about twice a second, only to keep `last_known_ltp` for restart recovery. With four engines the ledger should write on change, and LTP is not something to persist per tick.
4. **A crash triggers a normal shutdown today.** An unhandled exception in `run()` ends in `finally: self._teardown()`, which stops the feed, calls `terminateSession` through `_confirm_logoff`, and sends the session report, leaving any open position on the broker unmonitored. Under Hestia none of that may happen on an engine crash: no session termination (it would kill the other engines), and the auto-resume in §1.5 replaces it.
5. **Command semantics to keep, per engine (`_check_command_flag`).** `EXIT` liquidates and re-arms to `watching` without ending the session, drops any pending Rule 7 flip, and clears its flag only after a confirmed flat (an unconfirmed liquidation is retried by leaving the flag); `KILL` raises, teardown then leaves an open position untouched and hands control back ("manage it manually, or restart to resume monitoring it"); `DISABLE` is only a startup gate in `main()`. Under Hestia a per-engine `KILL` must end that engine's thread only, never the session, and a host-level `KILL` ends all engines with the same "position left open" semantics.
6. **State that is deliberately not persisted.** The pending markers (`_pending_flip`, `_pending_contract_transition`, `_pending_missed_flip`, `_pending_recovery`, `_pending_15m_boundary`), the rollover latches (`_rollover_*`) and all provisional-boundary bookkeeping are in memory only, by design ("a crash loses this and falls back to a safe default"). Recovery works by **reconstruction at `_setup()`** from the persisted state file, the broker's position book (`_reconcile_positions`), the missed-flip scan (`last_processed_boundary`) and missed-rollover recovery, not by persisting the markers. **This simplifies the plan's §1.6:** the engine's decision-state blob need only hold what Prometheus already persists plus the request IDs of §1.4, and auto-resume can reuse the same `_setup()` reconstruction. What needs new analysis is only a restart *inside* a session with a half-executed flip or roll (the ledger shows the old side closed and the new side partly open): today that is abandoned and re-derived on the next `_setup()`; the port must state explicitly what the resumed engine does with it.
7. **Import-time configuration blocks two engines with different symbols.** `prometheus_configs.py` resolves `SYMBOL` (from `instrument_override.json`), `LOT_SIZE`, `MARGIN_PER_UNIT`, `STATE_FILE`, `DYNAMIC_SIZING`, `STATIC_UNITS` and the rest as module globals at import, and the engine and its functions read them everywhere (`SYMBOL` appears in `prometheus.py` alone at least ten times). Hestia's shared services and the engines need **per-engine configuration objects**, not module globals. A straight port of Prometheus as the only engine works either way, but the shared services extracted from `prometheus_functions.py` must take the instrument as a parameter.
8. **Several files are singletons and would collide.** One private intraday cache (`prometheus_today_1m.csv`, holding rows for any token with a `token` column, pruned by date on read and rewritten whole by `_rewrite_today_cache_for_switch`), one 15-minute series dump (`prometheus_15m_series.csv`), one trade counter, one state file, one trades file, one command flag, one override file. Two engines writing the same cache from two threads would race on the whole-file rewrite. The cache and dumps must be per engine and per token (or in memory with a lock), and the counters, state and logs per engine.
9. **Provisional-boundary trading is the most delicate thing to port.** `_evaluate_provisional_boundary` (96 lines) can **act live on a bar that is not yet complete**, using the tick-aggregated OHLC, and `_reconcile_provisional` later checks that decision against the real bar and latches the feature off for the session on any disagreement. It mixes data (a provisional bar with a quality flag) and decision (act on it). It is live in Prometheus (`PROVISIONAL_BOUNDARY_ENABLED=True`) and was never part of the Selene backtest, but **the user has decided it applies to Selene too** (2026-09-28), so both engines use it; the design is in `plans/selene-production.md` §1.9, including a defect found in its margin guard: `band_dist_pct` is measured against the new band, so it passes 99.8% to 100% of flips and gates nothing (median 0.74% for SILVERMIC flips against a 0.15% threshold), whereas the closing price cleared the old band by 0.05% or less on 34% of SILVERMIC flips. **The user decided (2026-09-28) that the check is against the previous bar's supertrend, for both engines; fixed in Prometheus's live code the same day** (plan §1.9).
10. **Dead or crude-only code that does not need porting.** The opening-bar correction (`patch_opening_bar_if_artifact`, `fetch_crudeoil_opening_bar`, `_maybe_check_opening_bar`, `CRUDEOIL_REFERENCE_SYMBOL`) compares CRUDEOILM's 09:00 bar against CRUDEOIL's and is gated off (`OPENING_BAR_CORRECTION_ENABLED = False`); the 1-hour alignment filter (`_check_1h_alignment`, §17) is shelved (`ENTRY_FILTER_1H_ALIGN_ENABLED = False`). Neither needs a Hestia service. (Prometheus's own port can drop them or keep them dormant; recommendation: drop, since both are off.)
11. **Two copies of Supertrend agree exactly.** Production computes ST from a copy inside `prometheus_functions.py`; every backtest uses `apollo_production/technical_indicators.py` through `data_loader.compute_st`. The two class bodies differ in formatting and comments only, and I checked the numbers: on 19,407 real SILVERMIC 15-minute bars, ST values, trend and flips are **identical** for (10, 2.5), (10, 2.0), (10, 3.0) and (7, 2.5) (575, 778, 457 and 567 flips). A permanent test now pins this (§5). Hestia's data service should hold **one** implementation, and the replay harness relies on it.

---

## 2. Classification: `prometheus.py`

Destination key: **H-data** (Hestia data service), **H-exec** (execution service), **H-ledger** (position/order ledger and reconciliation), **H-risk** (margin, limits, DPL), **H-report** (Slack, reports, trade logs), **H-life** (session, calendar, flags, signals), **E** (the Prometheus engine: decision logic), **R** (roll-policy library, imported by engines), **drop**.

| Method (lines) | Destination | Note |
|---|---|---|
| `_load_slack_token`, `_tag`, `_slack_worker`, `_slack_flush`, `_slack` (77–171) | H-report | Module-level queue and worker; becomes the one shared queue |
| `__init__` (175–260) | split | State and pending markers to E; `feed` and `order_watcher` to Hestia; `signal.signal` to H-life (raises off the main thread) |
| `_handle_signal` (262) | H-life | |
| `_fetch_available_margin` (270) | H-risk | `rmsLimit()['data']['availablecash']`, one pool |
| `_calculate_margin_per_unit` (288) | E | The formula and constants are the engine's (Selene `price / 8 × 4`); the LTP comes from Hestia |
| `_calculate_units` (323) | E | Sizing policy over Hestia's live sizing config |
| `_check_margin_sufficient` (350) | H-risk | Becomes admission with a cross-engine reservation |
| `_check_command_flag` (384) | H-life | Semantics in §1 item 5 |
| `_check_rollover_tonight`, `_precompute_rollover_basis`, `_rollover_entry_suppressed`, `_check_rollover_timing`, `_execute_rollover_decision`, `_execute_rollover_reopen`, `_catch_up_contract_if_already_switched`, `_recover_missed_rollover`, `_execute_coincident_flip_transition`, `_retry_pending_contract_transition`, `_alert_pending_transition_stuck` (458–1571 in part) | R (with E) | The roll decision, veto, basis recalibration, coincident-flip logic. The retry halves disappear (Hestia retries); the alert halves go to H-report |
| `_start_dual_tracking`, `_do_rollover_prefetch`, `_do_rollover_topup_poll`, `_rewrite_today_cache_for_switch` (506, 951, 976, 1400) | H-data | Become `track(contract)`, `untrack` and the per-token cache |
| `_switch_to_new_contract_now` (554) | R (with H-data) | Decision in R; feed subscribe, seed and cache rewrite in Hestia |
| `_check_1h_alignment` (683) | drop | Shelved |
| `_harvest_tick_ohlc`, `_evaluate_provisional_boundary`, `_reconcile_provisional` (730–892) | split | Provisional bar plus quality flag from H-data; the act-on-it decision and reconciliation in E (§1 item 9) |
| `_reconcile_missed_flip`, `_retry_pending_missed_flip` (1186, 1274) | E | Scans Hestia's bars past the watermark; its retry becomes send-once |
| `_reconcile_positions` (1579) | H-ledger | Already keyed by `symboltoken` |
| `_setup` (1647) | split | Seed, DPL init, feed and order-watcher start, calendars to Hestia; contract choice, state resume, missed-roll and missed-flip recovery to E |
| `_confirm_logoff`, `_teardown` (1826–1935) | split | `terminateSession`, feed stop, session report to H-life and H-report; abandon-pending notes and the KILL "leave open" rule to E |
| `_send_session_report` (1937) | H-report | 183 lines of Slack formatting; per-engine report fields |
| `run` (2125) | H-life plus callbacks | §1 item 1 |
| `_recover_pending_windows`, `_merge_1m` (2340, 2353) | H-data | |
| `_maybe_check_opening_bar` (2374) | drop | §1 item 10 |
| `_get_contract_ltp`, `_get_ltp` (2404, 2951) | H-data | LTP with age, WS then REST fallback |
| `_fetch_dpl_circuit_limits`, `_refresh_dpl_circuit_limits`, `_check_dpl_circuit_hit` (2418–2509) | H-risk | Alert-only, per instrument |
| `_build_15m_bar`, `_handle_new_15m_bar` (2515, 2550) | split | Bar building, completeness alerts and ST to H-data with quality flags; flip and entry decisions to E |
| `_execute_rule7_flip`, `_retry_pending_flip`, `_alert_pending_flip_stuck` (2677–2808) | H-exec | Engine sends `flip`; netting, retry and stuck alerts are Hestia's |
| `_execute_entry`, `_finalize_new_position` (2814, 2844) | split | Levels, sizing, state in E; order, fill, ledger, partial-fill alert in Hestia. Lot logic (98 lines) disappears for Selene |
| `_minutes_since_session_open`, `_past_first_minute_guard`, `_past_min_entry_guard` (2963–3008) | E over H-data | Session-open time is Hestia data; the guards are strategy policy (Selene uses both, as the parity simulator does) |
| `_check_exit_conditions_ltp` (3010) | E | Stop and targets on Hestia's LTP; needs the request-pending state (§1 item 2) |
| `_execute_exit_lot`, `_apply_confirmed_lot_exit`, `_execute_exit_all` (3042–3172) | split | Order and fill confirmation in H-exec; state mutation on a confirmed outcome in E |
| `_finalize_trade`, `_append_running_row`, `_compute_trade_pnl`, `_send_trade_update` (3174–3323) | H-report and H-ledger | Fed by engine outcome events; per-minute running rows and the per-unit Rs convention |
| `_seconds_until_time`, `_wait_for_pid_exit`, `_seconds_until_evening_open`, `_login`, `main` (3330–3527) | H-life | Login, PID and stale-PID kill, holiday and evening-only gates, DISABLE gate, guardian, deferred start |

## 3. Classification: `prometheus_functions.py` and `prometheus_state.py`

| Item (lines) | Destination | Note |
|---|---|---|
| `_safe_concat`, `SupertrendIndicator`, `compute_st` (69–174) | H-data | One implementation (§1 item 11) |
| `_load_mcx_holidays`, `mcx_fully_closed_today`, `mcx_evening_only_today`, `next_trading_day`, `_count_trading_days_inclusive` (186–302) | H-life (calendar) | Shared by the roll library |
| `_contract_dict_from_row`, `resolve_contract_by_token` (305, 379) | H-data | Contract facts |
| `resolve_effective_contract` (325) | R | Front contract plus the 5-working-day early roll is a roll policy, not a fact |
| `resolve_live_sizing` (409) | H-life (config) | Reads the sizing override; engines read it through Hestia |
| `_merge_and_save`, `backfill_contract_if_needed` (447, 489) | H-data | Verify what still writes to shared pipeline files; the pipeline owns them |
| `read_today_cache`, `_rewrite_today_cache_file`, `clear_today_cache` (540–640) | H-data | Per token and per engine (§1 item 8) |
| `_tail_read_contract_csv`, `_resample_1m_to_Nmin`, `_find_Nmin_gaps`, `compute_st_for_contract`, `historical_basis_price`, `persist_15m_series`, `seed_st15` (643–992) | H-data | `_resample_1m_to_Nmin` anchors at the `SESSION_START_TIME` constant (09:00); evening-only handling is separate |
| `patch_opening_bar_if_artifact`, `fetch_crudeoil_opening_bar` (743, 770) | drop | §1 item 10 |
| `_check_candle_limit`, `sleep_until_next_boundary`, `fetch_one_minute_window` (1004–1078) | H-data and gateway | `_candle_counter` is an unlocked module dict, one copy per module |
| `resolve_thresholds`, `resolve_target2`, `target_fill_price`, `stop_fill_price` (1086–1121) | E | Strategy parameters; Selene has a stop only |
| `OrderFillWatcher`, `place_order`, `get_fill_price_and_qty`, `_check_ltp_limit`, `fetch_ltp_rest` (1129–1494) | H-exec and gateway | `_placed_order_ids` and `_ltp_counter` are module globals to make shared |
| `load_trade_counter`, `save_trade_counter`, `trade_log_filepath`, `append_trade_log_row`, `append_cumulative_trade` (1502–1573) | H-ledger and H-report | Per engine |
| `check_no_active_strategies` (1584) | H-life | Replaced by the registry and the session lock |
| `PrometheusState`, `save_state`, `load_state` | split | Ledger fields: status, direction, units, entry price and time, symbol, token, lots and lot exit fields. Decision fields: `sl_price`, targets, `recalibration_basis_price`, `signal_ts`, `signal_close`, `last_processed_boundary`. Transient: `last_known_ltp` |

## 4. Module-level state that must move or be made shared

`_candle_counter` and `_ltp_counter` (unlocked dicts, `prometheus_functions.py` lines 1001 and 1364), `_placed_order_ids` (ghost-recovery collision guard, line 1226), the Slack queue and `_slack_worker_started` (`prometheus.py` lines 101 to 102), the single-file paths listed in §1 item 8, and the import-time configuration of §1 item 7. Also the `sys.exit` calls in `main()` (lines 3428, 3488, 3494), `signal.signal` (line 259) and `os.kill` (stale PID, lines 3442 to 3449), and the two `terminateSession` calls (lines 1838, 3517). There is no mid-session re-login path anywhere: login exists only in `_login()` at start, and the JWT expires at midnight IST, so all engines end before then.

## 5. Checks made

- **Candle-call cadence** (from `prometheus_cron_20260901.log`, on this machine, DRY_RUN): one call per minute in steady state, 60 an hour in the quiet evening hours, 89 to 154 an hour with the day's 35% AB1021 failure rate, at most 6 attempts in a minute, longest gap between attempts 61 s and between successes 124 s. Recorded in the plan (§5).
- **Supertrend parity** (§1 item 11): identical on real SILVERMIC bars. New permanent test `tests/test_supertrend_production_backtest_parity.py` (12 cases, seeded random-walk bars with clustered volatility, compiles the production functions from source without importing `prometheus_functions`, so it has no config or logging side effects). Passes.

## 6. Effects on the plan and the next step

- **§1.6 (State)** simplifies as in §1 item 6: reconstruction at setup, not a persisted marker set.
- **§1.4 needs one sentence added:** the engine's request-pending state (§1 item 2) and that a stop path must not re-fire while a request is in flight.
- **§2 (components)** gains: per-engine and per-token cache files, per-engine configuration objects, write-on-change ledger.
- **New tests to port** (`tests/test_prometheus_*.py`, about 1,750 lines): DPL and freeze to the risk service, dynamic margin to sizing and admission, evening-only session and stale-PID wait to the lifecycle, position reconciliation to the ledger, session report and Slack worker to reporting, market close to the engine's missed-flip logic.
- **Next, P2: the interface spec.** Typed events, requests and outcomes, the data spec an engine registers, and the engine's request-pending states. The two data specs to write first are Prometheus's (one contract plus tracked next contract, ST 10 / 2.0 on 15 minutes, provisional boundary on) and Selene's (ST 10 / 2.5, provisional boundary on, with the corrected margin guard).
