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

**Artemis 1 lot = Athena 1 lot = Apollo 1 lot.**

The original analysis used margin as the normalizer (1 Artemis ≈ 5 Apollo lots, same margin blocked), which mechanically favoured Apollo at 4×. This was revised after drawdown analysis: Apollo's max historical drawdown at 4 lots is ₹73k against ₹80k position capital — a 91% drawdown on deployed capital. Short-premium strategies (Artemis, Athena) have bounded max loss; their margin *is* roughly their risk capital. Apollo's risk capital must be sized off drawdown, not margin. On a risk-capital basis (25% DD rule), Apollo at 4 lots requires ₹2.9L — its return on risk capital (~33%) is actually lower than Athena (~52%) and Artemis (~38%). All three strategies therefore run at 1 lot.

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

## 5. Routing map (final — all strategies 1 lot)

| VIX Band | Strategy | Lots | Expectancy | Next best |
|----------|----------|------|------------|-----------|
| VIX < 11 | **Apollo** | 1 | +₹685 | Artemis×1 = −₹892 |
| VIX 11–12 | **Artemis** | 1 | +₹681 | Apollo×1 = +₹353 |
| VIX 12–13 | **Artemis** | 1 | +₹658 | Athena×1 = +₹468 |
| VIX 13–14 | **Artemis** | 1 | +₹483 | Apollo×1 = +₹316 |
| VIX 14–15 | **Artemis** | 1 | +₹439 | Apollo×1 = +₹280 |
| VIX 15–16 | **Artemis** | 1 | +₹1,456 | Athena×1 = −₹993 |
| VIX 16–18 | **Athena** | 1 | +₹1,930 | Apollo×1 = −₹83 |
| VIX 18–20 | **Athena** | 1 | +₹599 | Apollo×1 = +₹161 |
| VIX 20–22 | **Athena** | 1 | +₹1,174 | Apollo×1 = +₹719 |
| VIX 22–25 | **Athena** | 1 | +₹1,074 | Apollo×1 = +₹146 |
| VIX > 25 | **Skip** | — | −₹12 | — |

### Key departures from current routing

- **VIX < 11**: Apollo replaces Artemis (Artemis −₹892, Apollo +₹685 — only band where Apollo wins on equal footing)
- **VIX 11–16**: Artemis retained across all five bands; removing the Apollo 4× multiplier eliminates Apollo's edge here
- **VIX 16–25**: Athena retained across all four bands; Apollo never beats Athena 1:1 above VIX 16
- **VIX > 25**: Skip; Apollo is net-negative there (−₹12/trade)
- **Net change**: Apollo deploys in one band only (VIX < 11) instead of six; the original three-strategy structure is largely preserved with finer band boundaries

---

## 6. Drawdown analysis (routed bands, 1 lot each)

| Strategy | Trades | Win rate | Total P&L | Max drawdown | Max consec | Worst run | Calmar |
|----------|--------|----------|-----------|--------------|------------|-----------|--------|
| Athena (16–18, 22–25) | 57 | 54.4% | +₹87,747 | −₹7,014 | 4 | −₹5,541 | 12.51 |
| Artemis (12–13, 15–16) | 58 | 72.4% | +₹60,507 | −₹8,011 | 3 | −₹7,400 | 7.55 |
| Apollo (< 11 only) | 37 | — | — | — | — | — | — |

Apollo at 1 lot in VIX < 11 (37 trades, +₹685/trade expectancy): its drawdown profile in
this narrow band alone has not been separately computed. Total P&L ≈ +₹25k, and given
N=37 this band contributes modestly to overall system P&L.

### Why not 4 lots for Apollo

At 4 lots, Apollo's max historical drawdown across all routed bands is ₹73,411 against
₹80k position capital — a 91% drawdown on deployed capital. The correct comparator is
risk capital sized off drawdown (25% DD rule): Apollo at 4 lots requires ₹2.9L, implying
~33% annual return on risk capital. Athena and Artemis have bounded max-loss (spread width),
so their margin *is* their risk capital; they achieve ~52% and ~38% respectively. On a
consistent risk-capital basis Apollo's scaling advantage disappears. All three run at 1 lot.

---

## 7. Apollo routed performance (vs unfiltered, 1 lot)

| | Unfiltered (all VIX) | Routed (VIX < 11 only) |
|-|---------------------|------------------------|
| Trades | 608 | 37 |
| Expectancy | +₹154/trade | +₹685/trade |

Apollo is deployed in VIX < 11 only under the 1-lot routing map. The 4-lot routed run
(343 trades across 6 bands, +₹1,42,396) remains in `data/trade_summary_phase2_routed.csv`
as a reference for if/when lot scaling is revisited.

---

## 8. Implementation steps for Leto

### 8.1 New VIX boundaries in `leto_config.py`

Replace the two-threshold system with an 11-band lookup:

```python
# New routing bands (replace VIX_ARTEMIS_MAX / VIX_ATHENA_MAX)
VIX_ROUTING_MAP = [
    # (vix_low, vix_high, strategy, lots)
    (0.0,  11.0, 'apollo',  1),
    (11.0, 16.0, 'artemis', 1),
    (16.0, 25.0, 'athena',  1),
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

Apollo production runs 1 lot. The new routing keeps Apollo at 1 lot — no change required.

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
