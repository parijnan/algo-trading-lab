# Leto Integrated Backtest

Portfolio-level routed backtest that simulates the production Leto routing logic across all four strategies (Artemis, Athena, Iris, Apollo retired) from 2020 to present. Produces a single consolidated trade log with one row per executed trade.

---

## What it does

Replicates `leto.py`'s strategy-selection logic on historical data:

- Reads VIX at 10:30 on each routing checkpoint day
- Routes to Artemis (VIX < 16), Athena (16 ≤ VIX ≤ 25), or Iris (VIX > 25)
- Enforces the "no concurrent trade" constraint — an active trade blocks all new entries
- Handles the Era A / Era B structural difference (dual Mon/Wed checkpoints before Sep 2025; unified Monday from Sep 2025 onward)

## Era structure

**Era A (2020-01-01 – 2025-08-31):**
- Artemis (Nifty) enters Mondays; Athena enters Wednesdays
- Two independent checkpoints per week; each can be blocked by an active trade on the other

**Era B (2025-09-01 – present):**
- Nifty expiry moved to Tuesday → Athena enters Monday
- Sensex expiry Thursday → Artemis enters Monday
- Single Monday checkpoint for both strategies; Iris fires on any day with VIX > 25

## Running

```bash
python leto_backtest/run.py
```

Outputs `data/leto_trade_log.csv` (gitignored — generated on every run).

## Refreshing after a strategy backtest update

When any individual strategy backtest is re-run:

1. Re-run `python leto_backtest/run.py` — it reads the source files directly.
2. Before re-running Athena (`athena_backtest/backtest.py`), set `ENABLE_VIX_FILTER = False` in `athena_backtest/configs.py` so all entry dates are present for the router. Restore to `True` after.
3. After re-running Artemis Sensex (`INSTRUMENT = 'sensex'` in `artemis_backtest/configs.py`), copy the output: `cp artemis_backtest/data/trade_summary_sensex.csv artemis_backtest/data/trade_summary_sensex_rerun.csv`.

## Module layout

| File | Role |
|---|---|
| `configs.py` | Date ranges, file paths, VIX thresholds, era split date |
| `loader.py` | Normalise all strategy trade summaries to a common schema |
| `router.py` | VIX snap at 10:30, routing decision (artemis / athena / iris) |
| `simulator.py` | Main loop — Era A dual-checkpoint, Era B unified Monday |
| `analysis.py` | P&L stats, drawdown, Calmar, year-by-year breakdown, validation checks |
| `run.py` | Entry point |
| `data/leto_trade_log.csv` | Generated output (gitignored) |

## Output schema (`leto_trade_log.csv`)

| Column | Description |
|---|---|
| `week_start` | Monday of the week this row belongs to |
| `entry_date` | Actual entry date |
| `entry_ts` | Entry timestamp |
| `exit_ts` | Exit timestamp |
| `strategy` | artemis / athena / iris |
| `instrument` | nifty / sensex |
| `vix_at_entry` | VIX snapped at routing checkpoint |
| `pl_rs` | P&L in ₹ (1 lot) |
| `exit_reason` | From source trade summary |
| `routing_outcome` | entered / skipped_no_signal / vix_routed_no_trade / vix_data_missing |

## Current results (2020-01-01 to 2026-06-29 data cutoffs)

| Strategy | VIX Regime | Trades | Total P&L | Win Rate | Avg/trade |
|---|---|---|---|---|---|
| Artemis | < 16 | 166 | ₹1,44,989 | 71.1% | ₹873 |
| Athena | 16 – 25 | 123 | ₹1,52,155 | 61.0% | ₹1,237 |
| Iris | > 25 | 58 | ₹25,589 | 60.3% | ₹441 |
| **Total** | | **347** | **₹3,22,733** | **65.7%** | **₹930** |

| Metric | Value |
|---|---|
| Max drawdown | ₹14,537 |
| Calmar | 22.2 |
| Expectancy | ₹930 per trade |

Data cutoffs: Artemis Sensex → 2026-06-29 · Athena → 2026-06-08 · Iris → 2026-05-15.

## Data sources

| Strategy | Source file |
|---|---|
| Artemis Nifty | `artemis_backtest/data/trade_summary_nifty_rerun.csv` |
| Artemis Sensex | `artemis_backtest/data/trade_summary_sensex_rerun.csv` |
| Athena | `athena_backtest/data/trade_summary_vix_all.csv` (VIX filter off) |
| Iris | `iris_backtest/data/iris_backtest_summary.csv` |
| VIX | `data_pipeline/data/indices/india_vix.csv` |
