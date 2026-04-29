# Artemis Phase 4.1 — Unified Nifty Strategy Research

This research module explores moving the Artemis Iron Condor strategy from Sensex back to Nifty to enable seamless integration with the Athena and Apollo modules.

## Strategy Logic (Phase 4.1)

### 1. The 4-Day Nifty Cycle
To preserve the aggressive theta decay window while avoiding weekend gap risk where possible:
- **Index:** Nifty 50
- **Expiry:** Tuesday 15:30
- **Entry:** Previous Thursday 10:30 (Holding: Thu, Fri, Mon, Tue)
- **ELM Exit:** Monday 15:15 (Day before expiry)

### 2. Cross-Pollinated Hedging: Weekend Smart Parachute
Borrowed from the Athena strategy, this mechanism protects the portfolio against extreme weekend gaps:
- **Trigger:** If the spot price gaps $\ge 2\%$ between Friday's close and Monday's open.
- **Action:** Leto/Artemis forces an immediate liquidation of all legs at the Monday open (09:15).
- **Goal:** Minimize catastrophic losses from weekend "black swan" events while allowing the trade to stay open for theta capture during normal regimes.

### 3. Execution Model
- **Deterministic Rules:** All entries, SLs, and exits are based on 1-minute OHLCV data.
- **Transparency:** Every decision is logged in `data/trade_logs/`.
- **Manual Fallback:** The logic is designed to be easily replicated manually via the broker's mobile or web app.

## Files
- `generate_contracts_p4.py`: Generates the Thu-Tue contract schedule.
- `configs_p4.py`: Configuration for Phase 4 research parameters.
- `backtest_p4.py`: The backtest engine implementing the new logic.
- `data_loader.py`: Shared data loading utility.

## How to Run
1. Generate the contracts:
   ```bash
   python artemis_backtest_phase4/generate_contracts_p4.py
   ```
2. Run the backtest:
   ```bash
   python artemis_backtest_phase4/backtest_p4.py
   ```
3. Review results in `data/trade_summary_p4.csv`.
