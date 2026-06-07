# Plan: Aphrodite — Intraday Iron Condor (VIX < 11)

**Status: FEASIBILITY COMPLETE — architecture decision pending**

---

## 1. Objective

Deploy an intraday short-premium iron condor on Nifty weekly options during Apollo phases
(VIX open < 11), using the ₹52L of capital that sits idle while Apollo runs on ₹8L margin.
Enter mid-morning after Apollo is seated, close before end of day — no overnight exposure.

---

## 2. Feasibility Gate

### 2.1 Frequency

93 priceable low-VIX days over 6 years (2020–2026), approximately 15/year but highly
regime-dependent:

| Year | Days | Notes |
|------|------|-------|
| 2020–2022 | 0 | VIX never sustained below 11 |
| 2023 | 38 | Extended low-vol regime |
| 2024 | 4 | Brief dip |
| 2025 | 46 | Extended low-vol regime |
| 2026 | 8 | Jan only (data to Jun 2026) |

VIX < 11 arrives in clusters, not uniformly. Expect 0–50 viable sessions per year.

### 2.2 Cost-adjusted edge

Structure tested: sell 2σ OTM CE + PE, hedge at 3σ. Slippage model: 1pt/leg × 4 legs ×
entry + exit = 8pts round-trip.

| DTE | N | Gross median | Net median | Net > 0 | Net > 5 | Verdict |
|-----|---|-------------|------------|---------|---------|---------|
| 5 (Friday, prior week) | 19 | 19.4 pts | **+11.4 pts** | 100% | 100% | ✓ Viable |
| 4 (Monday) | 23 | 12.7 pts | **+4.7 pts** | 96% | 39% | ~ Marginal |
| 3 | 16 | 10.0 pts | +2.0 pts | 62% | 25% | ~ Marginal |
| 2 (Tuesday) | 19 | 4.2 pts | −3.8 pts | 11% | 0% | ✗ Not viable |
| 1 (Expiry Thursday) | 16 | 0.7 pts | −7.3 pts | 0% | 0% | ✗ Not viable |

Slippage consumes the entire edge at DTE ≤ 2. DTE 5 (Friday with full week ahead) is
the only clearly viable entry day.

**Practical entry filter:** DTE ≥ 4 AND net credit ≥ 5pts after slippage → **32 days over
6 years, ~5 entries/year.**

### 2.3 Breakeven win rate

With SL at 2× net credit collected:

```
Breakeven WR = SL / (SL + credit) = 2c / (2c + c) = 67%
```

67% is structurally achievable: in VIX < 11, realized intraday moves are small and 2σ
OTM strikes are rarely threatened. Requires validation via backtest.

### 2.4 Data availability

Far-OTM strikes (2σ and 3σ) have data on 97–100% of low-VIX days in
`data_pipeline/data/nifty/options/`. No data gaps that would block a backtest.

---

## 3. Design Parameters (pending architecture decision)

### 3.1 Instrument and expiry

Nifty weekly options. Entry day must satisfy DTE ≥ 4 (at least 4 trading days to the
Thursday expiry). This means: Friday of the prior week (DTE 5) or Monday (DTE 4). Tuesday
is Nifty's bank-holiday-adjusted expiry week squeeze — consistently 0% positive, excluded.

### 3.2 Entry condition

- VIX open < 11
- Apollo session active (optional coupling — see §3.7)
- DTE ≥ 4
- Net credit ≥ 5pts after slippage (computed at entry scan time)
- Entry at open of 10:31 candle (after Apollo entry is confirmed)

### 3.3 Strike selection

Anchor to VIX-implied daily move:

```
daily_1σ = spot × (VIX / 100) / √252
ce_sell  = round((spot + 2σ) / 50) × 50
ce_hedge = round((spot + 3σ) / 50) × 50
pe_sell  = round((spot - 2σ) / 50) × 50
pe_hedge = round((spot - 3σ) / 50) × 50
```

### 3.4 Stop loss

SL fires when net unrealised loss > 2× net credit collected. No DTE-based multiplier
(position does not survive overnight). Executes at open of next 1-min candle.

No 09:15 gap-open guard needed — entry is at 10:31.

### 3.5 Exit rule

Hard square-off at 15:00 (before ELM window). No overnight carry.

Optional: exit earlier if residual value < 10% of credit (theta has done its job).

### 3.6 Lot sizing

1 lot (same as Artemis/Athena under the routing map). Scale with account growth after
live data validates the backtest.

### 3.7 Coupling to Apollo

**Option A — Coupled:** only enter if Apollo has an active position. Ensures both are
running simultaneously; Aphrodite never fires on a VIX < 11 day when Apollo's trend-flip
didn't trigger.

**Option B — Decoupled:** enter on any VIX < 11 day meeting the criteria, regardless of
Apollo. Captures more sessions; Apollo and Aphrodite are independent.

Decision pending. Option B is cleaner architecturally (no inter-strategy state).

---

## 4. Architecture Decision (open)

**5 entries/year** is the key question. Does it warrant a full production strategy with its
own broker session, state machine, feed management, and order-resilience stack?

**Option A — Full automation:** new `aphrodite_production/` module, mirrors Athena/Artemis
structure. Build cost: ~2–3 sessions. Only justified if entries/year grows with data or
lot sizing scales.

**Option B — Semi-manual rule:** no production code. On applicable days (Friday/Monday
low-VIX, DTE ≥ 4, credit clears threshold), manually assess and enter via Hermes. Aphrodite
becomes a checklist, not a strategy. Low build cost; works at 5/year frequency.

---

## 5. Backtest (pending architecture decision)

Before building production code, validate:

1. **Intraday win rate** — what fraction of DTE ≥ 4 low-VIX days expire worthless intraday?
2. **SL frequency** — how often does the 2× credit SL trigger before 15:00?
3. **Exit timing** — does closing at 15:00 vs earlier (e.g., 14:00) materially affect net?
4. **Credit filter** — confirm net > 5pt filter doesn't exclude most profitable days

Script: `aphrodite_backtest/backtest.py` (to be created).

---

## 6. Go/no-go gate

- [ ] Architecture decision (auto vs semi-manual)
- [ ] Backtest validating 67%+ intraday win rate on DTE ≥ 4 days
- [ ] Confirm slippage model is realistic for far-OTM Nifty options (bid-ask spread check)
- [ ] Paper trade ≥ 3 sessions before live

---

## 7. Files

| File | Status |
|------|--------|
| `plans/aphrodite.md` | This document |
| `aphrodite_backtest/` | Not yet created |
| `aphrodite_production/` | Not yet created |
