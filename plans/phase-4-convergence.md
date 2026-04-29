# Phase 4: Strategic Convergence (Unified Nifty Ecosystem)

## Objective
To unify all three strategies (Artemis, Athena, Apollo) under a single underlying index (Nifty) and transition the orchestrator (`leto.py`) from a static, entry-time router into a dynamic, mid-trade state manager. This will allow the strategies to hand off seamlessly during volatility regime shifts, optimizing capital efficiency and maintaining strict risk controls.

## Background & Motivation
Artemis was originally moved to Sensex to capitalize on a 4-day holding period that avoided weekend gap risk, due to the divergence in expiry days (Nifty expiry moving to Tuesday). However, isolating Artemis on a different index prevents it from dynamically interacting with Athena and Apollo (which are Nifty-based). 
By moving Artemis back to Nifty, we can treat the three strategies as distinct *modes* of a single portfolio. If a market-neutral Artemis trade is threatened by a VIX spike and trend emergence, it should elegantly transition its capital and risk profile to Apollo's trend-following logic, rather than relying on sub-optimal, isolated adjustments.

## Scope & Impact
- **Artemis:** Re-engineered for Nifty (Tuesday expiry).
- **Risk Management:** Implementation of "Cross-Pollinated Hedging" (borrowing Athena's Smart Parachutes/Wings) to allow Artemis to hold through the weekend safely.
- **Orchestrator (Leto):** Upgraded to manage a unified portfolio state, tracking Greeks (Delta/Vega) and orchestrating handoffs based on continuous VIX and Supertrend polling.
- **Backtesting:** A completely new Phase 4 simulation engine that can test continuous, multi-strategy lifecycles to evaluate Win/Loss and Risk:Reward (RR) ratios.

## Proposed Solution: The Unified Portfolio Architecture

To crack the complexity of a unified portfolio while ensuring psychological comfort (balanced RR and Win Rate), we will take a step-by-step simulation approach.

### 1. The Artemis "Weekend Hedge" (Cross-Pollination)
Since Nifty expires on Tuesday, a standard weekly trade implies holding over the weekend. To protect Artemis's Iron Condor without sacrificing the optimal theta window:
- We will integrate Athena's **Safety Wings** (buying far-OTM options) or the **Smart Parachute** (buying a directional hedge if spot moves aggressively).
- *Alternative tested:* **Dynamic Weekend Liquidation** (Leto forces closure on Friday at 15:15 if VIX > threshold, re-entering Monday).

### 2. Strategy Handoff Mechanics (The Simulation Race)
We will build `leto_phase4_simulation.py` to backtest three distinct handoff architectures simultaneously to see which yields the best RR and Win/Loss:
- **Tier 1 (Hard Liquidation):** When VIX crosses a threshold (e.g., <16 to >16) and Apollo's Supertrend fires, Leto forces Artemis to close all 4 legs, takes the realized P&L, and deploys Apollo fresh.
- **Tier 2 (Leg Morphing):** If Artemis's Call SL is breached (bullish breakout), Artemis closes the threatened Call spread, but Leto leaves the safe Put spread open and uses the freed capital to buy Apollo's bullish Debit Spread.
- **Tier 3 (Unified Greeks Manager - The Big Goal):** Leto acts as a central risk manager. Artemis, Athena, and Apollo don't execute trades; they generate *Target Deltas*. Leto looks at the current portfolio Delta, receives the new Target Delta from the active strategy, and executes the minimum required leg adjustments to match the new Greek profile.

## Phased Implementation Plan

### Phase 4.1: Artemis Nifty Translation & Hedging Simulation
1. Create `artemis_backtest_phase4/` focused entirely on Nifty.
2. Integrate Nifty's Tuesday expiry logic into the data loaders.
3. Code and backtest the **Cross-Pollinated Hedging** (Smart Parachute & Wings) vs **Dynamic Weekend Liquidation**.
4. Analyze the Win/Loss and RR to lock in the new baseline Artemis strategy.

### Phase 4.2: The Leto Simulation Engine (Handoffs)
1. Build `leto_phase4_simulation.py` capable of reading the 1-minute Nifty and VIX datasets chronologically.
2. Implement the **Hard Liquidation** handoff logic. Run a 3-year backtest to establish a multi-strategy baseline.
3. Implement the **Leg Morphing** handoff logic. Compare capital efficiency and drawdown against Hard Liquidation.

### Phase 4.3: The Unified Portfolio (Advanced)
1. If Leg Morphing proves too rigid, abstract the execution layer.
2. Build a `PortfolioState` class in Leto that tracks net Delta and net Vega.
3. Convert Apollo, Athena, and Artemis into "Signal Engines" that output desired Greek states rather than explicit strike orders.

## Verification & Testing
- Every phase requires rigorous historical backtesting.
- Success is defined *not* strictly by absolute return, but by a smoothed equity curve, acceptable drawdowns, and a Win/Loss ratio that supports trading psychology.
- We will explicitly log "Handoff Events" in the backtest to scrutinize slippage and execution feasibility.

## Migration & Rollback
- Live production code (`apollo_production`, `athena_production`, `artemis_production`) remains completely untouched during the research phase.
- Once Phase 4 backtests are validated and approved, we will build a new `leto_phase4.py` orchestrator for live deployment, allowing us to easily revert to the legacy orchestrator if needed.
