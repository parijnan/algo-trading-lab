# Athena Backtest Suite

Backtesting and optimization for the Nifty Double Calendar Condor strategy.

## Strategy: Phase 2 (Optimized)

The current "Phase 2" configuration represents a 67% improvement over the baseline by addressing the inherent downside skew of the Indian market while maintaining high theta efficiency.

### Core Mechanics
- **Structure:** 4-leg Double Calendar (Sell 0.30 Delta Weekly, Buy same strike Monthly).
- **Strike Selection:** Matching Strikes (CE Long = CE Short, PE Long = PE Short) for maximum time decay capture.
- **DTE Guard:** Buy legs are rolled to the next month if the monthly expiry is < 16 days away.

### Risk Management (The "Smart Parachute")
Phase 2 introduces asymmetric risk management to protect against runaway rallies while saving on static hedge costs.

1. **PE-Only Safety Wing:** A 0.05 Delta monthly PE is bought at entry. CE wings are disabled to reduce net debit and margin.
2. **Emergency CE Hedge (Smart Parachute):** 
   - **Entry:** If `Spot >= CE Sell Strike + 150 pts`, a **0.35 Delta Monthly CE** is bought immediately.
   - **Exit (Salvage):** If the market reverses and `Spot <= CE Sell Strike`, the hedge is sold to preserve core profit.
   - **Limit:** Maximum 1 attempt per trade to limit whipsaw costs.

### Performance (Lot Size 65)
- **Total P&L:** +157,336 ₹ (on 1 lot, 5-year backtest)
- **Win Rate:** 63.9%
- **Reward:Risk:** 1.42
- **Max Drawdown (Consec):** 3 losses

## Usage

### 1. Precompute Data
Generate the resampled 75-minute and 15-minute caches used for indicator checks:
```bash
python athena_backtest/precompute.py
```

### 2. Run Backtest
Execute the main backtest engine (uses parameters defined in `configs.py`):
```bash
python athena_backtest/backtest.py
```

### 3. Review Results
Results are saved to `athena_backtest/data/trade_summary.csv`.

---
*Note: Phase 1 legacy configurations are preserved in `configs_phase1.py` and `backtest_phase1.py` for reference.*
