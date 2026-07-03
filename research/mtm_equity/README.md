# MTM Equity Curve — Step 0 Diagnostic

**Poseidon plan gate.** Measures the existing book's true intraday mark-to-market
drawdown vs the realized-P&L drawdown (₹14,537) that `leto_backtest/analysis.py`
reports. The gap is the "hidden risk" number that determines whether a
diversifying trend overlay is justified.

**Status: COMPLETE.** Run `python research/mtm_equity/run.py` to reproduce.

---

## Methodology

1. **Load the routed trade roster** from `leto_backtest/data/leto_trade_log.csv`
   (347 entered trades, 2020–2026).
2. **Extract per-bar MTM** from each strategy's trade logs:
   - **Athena**: `cumulative_pl` column (points) — includes realised + unrealised.
   - **Artemis**: `(pe_pl + ce_pl) + (pe_add_pl + ce_add_pl) / 2` (points) — includes
     `booked_pl` from SL-closed legs. The last bar may differ from the summary's
     `total_pl_points` because expiry settlement happens after the last logged bar.
   - **Iris**: `unr_rs` column (rupees) — already in rupees, truncated at exit_ts.
3. **Auto-calibrate to rupees**: `factor = pl_rs / mtm_at_exit_bar`. If the last bar
   and `pl_rs` have opposite signs (3 Artemis trades with expiry settlement gaps),
   fall back to LOT_SIZE conversion. Always append a final exit point at `exit_ts`
   with value = `pl_rs` so every curve terminates exactly at the realised P&L.
4. **Build portfolio equity**: since `leto_backtest` validates no overlapping
   trades, the merge is a concat. Each trade's equity = cumulative realised P&L
   before this trade + current trade's MTM.
5. **Drawdown**: `running_max(equity)` → `equity - running_max`.

### Validation gates (all pass)
- ✅ **Reconciliation**: all 347 trades' final MTM = pl_rs (|diff| < 0.01)
- ✅ **Lossless merge**: realized DD reproduced (₹14,537) from MTM trade boundaries
- ✅ **No overlaps**: no two trades share a 1-min timestamp
- ✅ **Coverage**: 347/347 trades have logs (100%)

---

## Headline Result

| Metric | Realized (leto_backtest) | MTM (this diagnostic) |
|---|---:|---:|
| Max drawdown | ₹14,537 | ₹18,986 |
| Calmar | 22.20 | 17.00 |
| Total P&L | ₹3,22,733 | ₹3,22,733 |
| N trades | 347 | 347 |

**Gap: ₹4,449 (1.3× realized).** The true intraday MTM drawdown is 31% worse
than the realized-only figure. The MTM DD is 43 days (not recovered by end of
data), peaking 2026-05-20 and troughing 2026-06-15.

---

## Gate Assessment

Per the Poseidon plan's §1 gate rule:

> If MTM max drawdown is within ~1.5–2× the realized figure → the "hidden risk"
> argument is weak; the case for Poseidon then rests solely on the narrower
> proactive-window argument.

**1.3× is below the 1.5–2× band.** The full-sample "hidden risk" argument is
weak — the realized-P&L drawdown is not dramatically optimistic. The case for
Poseidon rests on the proactive-window replay below.

---

## Stress Window Replays

### 2020 COVID Proactive Window (Feb 24 – Mar 12)

This is the crux window — Artemis/Athena fully short-gamma before Iris's VIX>25
gate fires.

| Metric | Value |
|---|---:|
| Trades | 2 (Artemis +₹1,885, Athena +₹4,992) |
| Realized P&L | +₹6,877 |
| MTM equity range | ₹3,946 → ₹17,521 |
| Window max DD | ₹6,331 (36.1% from peak) |
| Deepest dip from window start | ₹2,031 |
| Peak → Trough | 1 day 20h (Mar 9 → Mar 11) |

**Finding:** Both trades closed positive (+₹6,877), but the MTM equity dipped
₹2,031 below the window start before recovering. The realized-P&L view hides
this dip — the equity was underwater mid-trade even though both trades ended
profitable. This is real but modest (₹2,031 on a ~₹12,000 starting equity).

### 2020 COVID Acute (Mar 13 – Apr 30)

| Metric | Value |
|---|---:|
| Trades | 20 (all Iris) |
| Realized P&L | +₹18,743 |
| Window max DD | ₹6,497 (18.8% from peak) |

Iris caught the crash well — 20 scalping trades, mostly profitable. The
reactive crisis sleeve worked as designed.

### 2022 Rate-Hike Spike (Jan – Jun)

| Metric | Value |
|---|---:|
| Trades | 27 (Athena + Iris) |
| Realized P&L | +₹39,286 |
| Window max DD | ₹15,200 (10.0% from peak) |
| Peak → Trough | ~71 days |

The worst window DD outside the overall max. Consecutive Athena losses in
Jan–Apr 2022 drove the equity down ₹15,200 from peak. The realized P&L was
strongly positive for the period, but the intraday MTM showed significant
drawdown.

### 2024 Election Vol (May – Jun)

| Metric | Value |
|---|---:|
| Trades | 8 |
| Realized P&L | +₹15,319 |
| Window max DD | ₹16,273 (7.0% from peak) |
| Deepest dip from window start | ₹3,416 |

### Overall Max DD Window (May 20 – Jun 15, 2026)

| Metric | Value |
|---|---:|
| Trades | 5 (4 Athena losses, 1 Artemis gain) |
| Realized P&L | -₹11,436 |
| Window max DD | ₹18,986 (5.6% from peak) |
| Peak → Trough | 26 days |

The overall max DD is driven by consecutive Athena losses in May–June 2026,
not by a crisis event. This is a strategy-level losing streak, not a
proactive-window gap.

---

## Conclusions

1. **The full-sample hidden risk argument is weak.** 1.3× gap is below the
   plan's 1.5–2× threshold. The realized-P&L Calmar (22.2) is not dramatically
   optimistic.

2. **The 2020 COVID proactive window DID hide a dip** (₹2,031 on ~₹12K
   starting equity, ~17%), but it's modest in absolute terms and recovered
   within 2 days. The proactive-window argument is real but narrow.

3. **The worst drawdowns are strategy-level losing streaks**, not
   proactive-window gaps. The overall max DD (₹18,986) is from consecutive
   Athena losses in 2026, not from a VIX-spike-before-Iris-fires scenario.

4. **Per the plan's §8 fallback:** since the MTM gap is small AND the
   proactive-window dip is modest, check first whether lowering Iris's
   VIX-activation threshold (a config change, not a new engine) covers the
   same gap more cheaply before building Poseidon.

---

## Files

| File | Purpose |
|---|---|
| `configs.py` | Paths, lot sizes, stress-window date ranges |
| `build_mtm.py` | Per-strategy MTM extractors + auto-calibration |
| `equity_curve.py` | Portfolio merge + drawdown computation |
| `run.py` | Entry point — builds curve, runs validation gates |
| `replay.py` | Stress-window replays |
| `data/portfolio_mtm_equity.parquet` | 1-min portfolio MTM equity curve |
| `data/per_trade_mtm.parquet` | Per-trade MTM curves |
| `data/mtm_vs_realized_summary.csv` | Headline metrics |
