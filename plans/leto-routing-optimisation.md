# Plan: Leto Routing Optimisation — Capital-Adjusted VIX Band Router

**Status: RESEARCH COMPLETE — implementation pending**

---

## 1. Objective

Replace Leto's current two-boundary VIX gate (Artemis < 16, Athena 16–25, Apollo > 25) with
an **11-band capital-adjusted routing map** that deploys the highest-expectancy strategy for
each VIX level, with per-strategy lot sizing normalised to the same capital base.

The current routing loses significant edge: Apollo is structurally superior in 6 of the 11
VIX bands, including several bands where Athena currently runs uncontested.

---

## 2. Current routing (to be replaced)

```
VIX ≤ 16.0  → Artemis  (1 lot)
VIX 16–25   → Athena   (1 lot)
VIX > 25    → Apollo   (1 lot)
```

Apollo is currently deployed at VIX > 25 only (live) and VIX > 16 (backtest). The research
reveals it has positive expectancy across multiple low-VIX bands that are currently given to
Artemis or left idle.

---

## 3. Research methodology

### 3.1 Capital normalisation

Per-lot capital requirements were derived from Apollo's net debit distribution:

| Statistic | Points | ₹ (1 lot = 65) |
|-----------|--------|----------------|
| Mean net debit | 115.2 | ₹7,489 |
| Mean + 2σ | 156.3 | ₹10,156 |
| p95 peak intraday exposure | 170.9 | ₹11,108 |
| Recommended capital (1 lot) | — | **₹15,000–20,000** |

Artemis and Athena are short-premium strategies requiring full SPAN margin on both short legs.
Capital ratios relative to Apollo:

- **1 lot Artemis ≈ 5 lots Apollo** (same capital)
- **1 lot Athena ≈ 6 lots Apollo** (same capital)
- **Athena ≈ 1.2× Artemis** → effectively equal at 1:1

### 3.2 Lot sizing convention

**Artemis 1 lot = Athena 1 lot = Apollo 4 lots** (chosen over 1:5 on drawdown grounds — see §5).

### 3.3 Data sources

All analysis uses:
- `athena_backtest/data/trade_summary_vix_all.csv` — Athena run with VIX_FILTER_LOW=0 (2020–2026, 302 trades)
- `artemis_backtest/data/trade_summary_nifty_rerun.csv` — Artemis Nifty rerun (2020–2025, 150 valid trades; 146 `skipped_vix` rows excluded)
- `apollo_backtest/data/trade_summary_phase2_vix_all.csv` — Apollo Phase 2 run with VIX_THRESHOLD=0 (2020–2026, 608 trades)
- `apollo_backtest/data/trade_summary_phase2_routed.csv` — Apollo filtered to recommended bands only (343 trades)

---

## 4. Per-strategy expectancy by VIX band (1 lot each)

| Band | Athena | Artemis | Apollo |
|------|--------|---------|--------|
| VIX < 11 | −₹1,846 (N=15) | −₹892 (N=3) | **+₹685** (N=37) |
| VIX 11–12 | −₹127 (N=34) | +₹681 (N=24) | **+₹353** (N=59) |
| VIX 12–13 | **+₹468** (N=30) | +₹658 (N=30) | −₹334 (N=66) |
| VIX 13–14 | +₹197 (N=39) | +₹483 (N=32) | **+₹316** (N=73) |
| VIX 14–15 | −₹640 (N=33) | **+₹439** (N=33) | +₹280 (N=52) |
| VIX 15–16 | −₹993 (N=27) | **+₹1,456** (N=28) | −₹482 (N=49) |
| VIX 16–18 | **+₹1,930** (N=31) | — | −₹83 (N=74) |
| VIX 18–20 | +₹599 (N=30) | — | **+₹161** (N=58) |
| VIX 20–22 | +₹1,174 (N=37) | — | **+₹719** (N=64) |
| VIX 22–25 | **+₹1,074** (N=26) | — | +₹146 (N=44) |
| VIX > 25 | — | — | −₹12 (N=32) |

---

## 5. Capital-adjusted routing map (final)

Position sizing: Artemis×1 = Athena×1 = Apollo×4

| VIX Band | Strategy | Lots | Adj. Expectancy | Next best |
|----------|----------|------|-----------------|-----------|
| VIX < 11 | **Apollo** | 4 | +₹2,742 | Artemis×1 = −₹892 |
| VIX 11–12 | **Apollo** | 4 | +₹1,412 | Artemis×1 = +₹681 |
| VIX 12–13 | **Artemis** | 1 | +₹658 | Athena×1 = +₹468 |
| VIX 13–14 | **Apollo** | 4 | +₹1,264 | Artemis×1 = +₹483 |
| VIX 14–15 | **Apollo** | 4 | +₹1,118 | Artemis×1 = +₹439 |
| VIX 15–16 | **Artemis** | 1 | +₹1,456 | Athena×1 = −₹993 |
| VIX 16–18 | **Athena** | 1 | +₹1,930 | Apollo×4 = −₹147 |
| VIX 18–20 | **Apollo** | 4 | +₹868 | Athena×1 = +₹599 |
| VIX 20–22 | **Apollo** | 4 | +₹2,877 | Athena×1 = +₹1,174 |
| VIX 22–25 | **Athena** | 1 | +₹1,074 | Apollo×4 = +₹584 |
| VIX > 25 | **Skip** | — | −₹49 | — |

### Key departures from current routing

- **Apollo now runs at VIX < 22** in 6 of the 11 bands (previously: VIX > 25 only)
- **Athena reduced** to two bands: 16–18 and 22–25 (previously: entire 16–25 range)
- **Apollo in 18–22** is the biggest surprise — 4-lot Apollo (+₹868/₹2,877) beats 1-lot Athena (+₹599/₹1,174) on capital-adjusted basis
- **VIX > 25** is now Skip; Apollo loses edge there (backtest: −₹12/trade)
- **VIX 12–13** is the only ambiguous band; Artemis chosen over Athena by ₹190/trade (within noise for N=30)

---

## 6. Apollo drawdown analysis (routed bands, 4 lots)

| Metric | Value |
|--------|-------|
| Max drawdown | −₹73,411 (2021) |
| Max consecutive losses | 8 |
| Worst 8-loss run | −₹43,732 |
| Calmar ratio | 7.76 |
| Total P&L (2020–May 2026) | +₹5,69,582 |

The max drawdown event (May–Aug 2021) was driven by sustained false breakouts in VIX 11–14,
with three trades firing on a single day (Jul 9) for a combined −₹33k. This was a
low-volatility grind regime, not a spike event.

### Why 4 lots over 5 lots

The Calmar ratio is identical at both scales (7.76) — 5 lots gives exactly 25% more P&L
and exactly 25% more drawdown, no risk-adjusted benefit. The deciding factor:

- At 5 lots: max drawdown **₹91,764** (crosses the ₹1L psychological/practical threshold)
- At 5 lots: worst single trade in VIX 18–20 hits **−₹41,308**
- At 4 lots: both stay below ₹75k; per-trade worst case under ₹34k

The strategy is improving over time (2023–2025 annual P&L is 3–5× better than 2020–2021).
Reassess scaling to 5 lots after 6–12 months of live routed data.

---

## 7. Apollo routed performance (vs unfiltered)

| | Unfiltered (all VIX) | Routed (recommended bands) |
|-|---------------------|--------------------------|
| Trades | 608 | 343 |
| Win rate | 40.3% | 44.9% |
| R:R | 1.75 | 1.92 |
| Expectancy | +₹154/trade | +₹415/trade |
| Total P&L | +₹1,00,035 | +₹1,42,396 |

Filtering to recommended bands adds ₹42,361 — the 265 removed trades were net-negative drag.

---

## 8. Implementation steps for Leto

### 8.1 New VIX boundaries in `leto_config.py`

Replace the two-threshold system with an 11-band lookup:

```python
# New routing bands (replace VIX_ARTEMIS_MAX / VIX_ATHENA_MAX)
VIX_ROUTING_MAP = [
    # (vix_low, vix_high, strategy, lots)
    (0.0,  11.0, 'apollo',  4),
    (11.0, 12.0, 'apollo',  4),
    (12.0, 13.0, 'artemis', 1),
    (13.0, 14.0, 'apollo',  4),
    (14.0, 15.0, 'apollo',  4),
    (15.0, 16.0, 'artemis', 1),
    (16.0, 18.0, 'athena',  1),
    (18.0, 20.0, 'apollo',  4),
    (20.0, 22.0, 'apollo',  4),
    (22.0, 25.0, 'athena',  1),
    (25.0, 999., 'skip',    0),
]
```

### 8.2 `leto.py` routing logic

Replace the `if vix <= VIX_ARTEMIS_MAX / elif vix <= VIX_ATHENA_MAX / else Apollo` chain with
a lookup against `VIX_ROUTING_MAP`. The lot count returned by the map is passed to the
strategy's entry function.

Each strategy must accept a `lots` parameter at entry. Apollo Phase 2 already does;
Artemis and Athena need to verify (Athena uses `LOT_SIZE` from configs — confirm the entry
function accepts an override).

### 8.3 Apollo Phase 2 VIX threshold

`apollo_backtest/configs_debit_phase2.py`: `VIX_THRESHOLD` currently gates Apollo to
high-VIX days only. In production, Leto owns routing — Apollo's own configs should not
re-filter by VIX. Confirm `apollo_production/configs_live.py` does not independently apply a
VIX gate that would block entry in the new lower-VIX bands.

### 8.4 Lot sizing for Apollo live

Apollo production currently runs 1 lot. When this routing is live, Apollo needs to run 4 lots.
Update `apollo_production/configs_live.py` accordingly — but only after paper-trading the new
routing for at least 2 weeks.

### 8.5 Manual override compatibility

The Slack override (Force Artemis / Force Athena) bypasses the routing map. This remains
valid; the override should continue to use the default 1-lot sizing for the forced strategy.
Apollo cannot be manually overridden (existing behaviour preserved).

---

## 9. Go/no-go gate for live deployment

- [ ] Paper trade the new routing for ≥ 2 weeks across all active VIX bands
- [ ] Confirm Apollo production does not apply an independent VIX gate below 16
- [ ] Confirm Athena and Artemis accept a `lots` override parameter (or default to 1 lot — acceptable since they only deploy 1 lot under this map)
- [ ] Confirm Apollo 4-lot margin is available for all routed bands (especially intraday peaks)
- [ ] First live session at 4 lots Apollo: monitor closely; have manual exit ready

---

## 10. Files generated by this research

| File | Description |
|------|-------------|
| `athena_backtest/data/trade_summary_vix_all.csv` | Athena backtest with VIX_FILTER_LOW=0 |
| `apollo_backtest/data/trade_summary_phase2_vix_all.csv` | Apollo Phase 2 with VIX_THRESHOLD=0 |
| `apollo_backtest/data/trade_summary_phase2_routed.csv` | Apollo trades in recommended bands only |
| `artemis_backtest/data/trade_summary_nifty_rerun.csv` | Artemis Nifty full rerun (2020–2025) |

These files are not committed (large CSVs, reproducible). Regenerate:

```bash
# Athena VIX-all (set VIX_FILTER_LOW=0.0 in configs.py, TRADE_SUMMARY_FILE to vix_all path)
python athena_backtest/backtest.py

# Apollo VIX-all (set VIX_THRESHOLD=0.0 in configs_debit_phase2.py, output to vix_all path)
python apollo_backtest/precompute_phase2.py
python apollo_backtest/backtest_debit_phase2.py
```
