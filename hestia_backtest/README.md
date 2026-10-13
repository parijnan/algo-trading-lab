# hestia_backtest

The integrated Hestia backtest: Prometheus, Selene, Helios and Typhon backtested together at their live configs, in rupees for one unit each, compared against what the live engines actually did. Refreshed by the local `hestia-backtest` skill (`.claude/skills/`, gitignored). Research and reporting only: nothing here is read by Hestia or any engine.

| File | Job |
|---|---|
| `hb_config.py` | Engine registry (instrument, rupees per point for one unit, backtest file), paths, match tolerances |
| `data_check.py` | Is the live contract tree current, and has a contract expired since the newest Fyers file (`data_pipeline/data/mcx_fyers/`) |
| `refresh_engines.py` | Re-runs the four engine backtests (Selene, Helios, Typhon production-parity; Prometheus Fyers Phase 3 sweep and live exits) |
| `build_ledger.py` | Combined closed-trade ledger and per-engine summary into `outputs/` |
| `compare_actual.py` | Pairs each live trade (Delos snapshot from `hestia_performance/pull_snapshot.py`) with its backtest twin |
| `build_page.py`, `page_template.html` | The artifact page (equity, drawdown, engine table, monthly heatmap, correlation, collapsible actual-versus-backtest table) |
| `outputs/` | Tracked results: `integrated_trades.csv`, `engine_summary.json`, `comparison.csv`, `comparison.json` (`outputs/logs/` is ignored) |

Data sources: Selene, Helios and Typhon read each day from the Fyers expired-contract file where Fyers has it and from the Angel One live tree otherwise (their loaders). The Prometheus Fyers track reads Fyers, Angel One inside the 2026-03-13 to 2026-06-29 void, and (since 2026-10-13) Angel One for every day after the last Fyers day, tagged `angelone_tail_fill`; the tail shrinks by itself as contracts expire and are downloaded. Numbers are gross of brokerage and charges. Tests: `tests/test_hestia_backtest.py`.
