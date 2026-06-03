# Iris Backtest

Signal research and strategy backtest for the Iris scalping strategy.
All output files are gitignored — regenerate by running the scripts below.

---

## Track A — Signal Comparison

**Outcome:** ST_FAST selected (5-min ST flip + 15-min regime alignment).
200 signals/year, WR 55.6% at 15-min, RR 1.42 — best quality of 8 candidates tested.

### Signals tested

| Signal | Timeframe | Edge |
|---|---|---|
| **ST_FAST** ✓ | 5-min entry + 15-min regime | RR 1.34–1.42, positive median at all horizons |
| ST_RAPID | 3-min entry + 9-min regime | Slightly worse than ST_FAST at every horizon |
| EMA_CROSS | 3-min | RR ~1.0 — no edge |
| BB_SQUEEZE | 5-min | Marginal edge but negative median at short horizons |
| ORB_15/30/45/60/75 | Opening range | RR degrades past 30-min; median near-zero |
| ATR_BURST | 3-min | Direction proxy (close>open) too noisy |
| ROC_BURST | 1-min | RR 1.01 at 120-min — noise |
| RANGE_BREAK | Daily regime + 1-min | 11 signals/year — unusable for scalping |

### Run signal comparison
```bash
python iris_backtest/research/run_all.py               # all 8 signals
python iris_backtest/research/run_all.py --signal ST_FAST  # single signal
python iris_backtest/research/compare.py               # comparison table
```

---

## Track B — Strategy Backtest

### Instrument
Nifty weekly options, ITM-150 (CE for bullish / PE for bearish), nearest expiry.
- Mean delta: ~0.70 — genuine optionality, not futures-like
- Median entry premium: ~213 pts (~₹13,871/lot)
- Liquidity: 370+ active bars/day at ITM-150 depth

### Calibrated exit parameters

| Parameter | Value | Basis |
|---|---|---|
| Profit target | 10% of entry premium | Backtest sweep — highest median ₹ at 30-min hold |
| Stop loss | 25% of entry premium | Tail insurance; fires on <2% of trades |
| Max hold | 30 minutes | Time-of-day + exit distribution analysis |
| Skip window | 10:45–11:30 | Post-opening dead zone (WR 31–43%, 3 consecutive windows) |

### Final backtest result (7.3 years, stop=25%, target=10%, max_hold=30m, skip 10:45–11:30)

| Bucket | N | WR% | Avg ₹/lot | Median ₹/lot |
|---|---|---|---|---|
| 1. Profit target | 478 | 100% | +₹1,444 | +₹1,378 |
| 2. Stop loss | 24 | 0% | -₹3,718 | -₹3,565 |
| 3. Trend flip | 42 | 9.5% | -₹1,412 | -₹1,562 |
| 4. Max hold | 628 | 34.9% | -₹436 | -₹332 |
| **TOTAL** | **1,172** | **59.8%** | **+₹229** | **+₹414** |

**Morning dominance:** 09:15 window (first 15 min) — WR 67.8%, 52% profit-target rate,
contributes 48% of total P&L on 30% of trades. Iris is a morning strategy.

### Run strategy backtest
```bash
# Full backtest with per-trade logs (takes ~5 min)
python iris_backtest/research/run_full_backtest.py

# Parameter sweep (stop × target × max_hold combinations)
python iris_backtest/research/run_strategy_backtest.py

# Exit bucket analysis at specific params
python iris_backtest/research/exit_bucket_analysis.py

# Time-of-day / VIX correlation (requires iris_backtest_summary.csv)
# Run inline analysis scripts from the session transcript
```

### Output files (gitignored — regenerate as needed)
- `data/signal_comparison.csv` — signal candidate metrics
- `data/options_sim_results.csv` — option fill prices at 5/15/30-min horizons
- `data/strategy_sweep.csv` — parameter sweep results (35 combinations)
- `data/iris_backtest_summary.csv` — one row per trade with MFE/MAE/trail metrics
- `data/trade_logs/` — per-minute option price logs for every trade (1,172 files)
