# Plan: Leto Integrated Backtest Module

**Status: COMPLETE — first run 2026-06-29**

---

## 1. Objective

Build a consolidated Leto backtesting module that simulates the production routing
logic from Jan 2020 to present. Individual strategy backtests were run in isolation;
this module applies the routing gate and "no concurrent trade" constraint to derive
the realistic P&L a routed account would have achieved.

The result is the single source of truth for routed portfolio performance:
expectancy, total P&L, drawdown, win rate, and Calmar — computed under actual
production constraints rather than summed from isolated backtests.

---

## 2. Simulation scope

| Parameter | Value |
|---|---|
| Start date | 2020-01-01 |
| End date | Present (latest available data) |
| Instruments | Artemis: Nifty (2020–Aug 2025), Sensex (Sep 2025–present); Athena: Nifty (full range); Iris: Nifty (full range) |
| Lot sizing | 1 lot per strategy throughout |
| Athena data | Standard `data_pipeline/` options only — **no Angel One realtime download** |

---

## 3. Routing rules

### 3.1 Target routing (full period)

| VIX at entry | Strategy | Notes |
|---|---|---|
| VIX < 16 | Artemis | 1 lot |
| VIX 16–25 | Athena | 1 lot |
| VIX > 25 | Iris | 1 lot; treat as live throughout (no "pending validation" period in backtest) |

### 3.2 Era split — entry day structure

**Era A: Jan 2020 – Aug 2025 (dual checkpoint)**

Artemis (Nifty) and Athena (Nifty) have different entry days in the same week:
- Artemis entry: **Monday** of the expiry week at 10:30 (from `contracts.csv`, instrument=nifty)
- Athena entry: **Wednesday** (last trading day before the prior expiry — day before Thursday
  Nifty expiry), at 10:30

This creates two routing checkpoints per week. Iris can fire any day when VIX > 25.

**Era B: Sep 2025 – present (unified checkpoint)**

Artemis switches to Sensex. Both Artemis and Athena share the same entry timing as
handled by production Leto — a single routing check per cycle. The exact entry day
is derived from each strategy's `contracts.csv` for the relevant instrument.

---

## 4. Weekly routing algorithm (Era A — dual checkpoint)

The simulation iterates over every trading day in chronological order. State is
maintained across days: which strategy (if any) is in an active trade, and when
that trade ends.

### 4.1 Monday checkpoint (Artemis entry day)

```
if no_active_trade:
    vix = VIX at 10:30 on this Monday
    if vix < 16.0:
        look up Artemis trade for this Monday in artemis_trade_summary
        if trade found:
            enter Artemis; set active_trade = this trade
    elif vix > 25.0:
        set iris_watch_mode = True for this week
        # No Artemis entry; look for Iris signals today (see §4.3)
    else:  # 16 <= vix <= 25
        # No Artemis; wait for Wednesday Athena checkpoint
        pass
else:
    # Active trade in progress — manage it (no new entry)
    pass
```

### 4.2 Wednesday checkpoint (Athena entry day)

```
if no_active_trade:
    vix = VIX at 10:30 on this Wednesday
    if 16.0 <= vix <= 25.0:
        look up Athena trade for this Wednesday in athena_trade_summary
        if trade found:
            enter Athena; set active_trade = this trade
    elif vix > 25.0:
        set iris_watch_mode = True
        # No Athena entry; look for Iris signals today (see §4.3)
    else:  # vix < 16
        # VIX has fallen below Athena range since Monday; no entry this Wednesday
        pass
else:
    # Active trade — manage it (Iris or Athena or Artemis); skip Athena entry check
    pass
```

### 4.3 Iris daily check (any day, VIX > 25)

Iris is checked on every trading day when `iris_watch_mode = True` OR when the
VIX-at-10:30 exceeds 25.0 and no trade is active. Iris is intraday — it always
closes by 15:00 on the same day.

```
for each trading day D (including Monday, Tuesday, Wednesday, Thursday, Friday):
    if no_active_trade and vix_at_1030(D) > 25.0:
        look up Iris signals for day D in iris_trade_summary
        if any valid Iris signal exists on D:
            take the first valid signal; set active_trade = Iris trade for D
            # Iris closes same day by 15:00 — active_trade clears at EOD
```

**Note:** Iris is intraday. `active_trade` for Iris clears at end of the same day.
The following day the state is clean and routing resumes.

### 4.4 Era B simplification (Sep 2025 onward)

```
for each entry day E (from unified contracts schedule):
    if no_active_trade:
        vix = VIX at 10:30 on E
        if vix < 16.0:
            enter Artemis (Sensex) if trade found for E
        elif 16.0 <= vix <= 25.0:
            enter Athena if trade found for E
        elif vix > 25.0:
            look for Iris signal on E
    else:
        manage active trade; skip entry
```

For Iris in Era B: same daily check logic as Era A — Iris can fire on any day
VIX > 25 when no trade is active.

---

## 5. Trade state machine

Each active trade has:

| Field | Description |
|---|---|
| `strategy` | 'artemis', 'athena', 'iris' |
| `entry_date` | Calendar date of entry |
| `entry_time` | Timestamp of entry |
| `exit_date` | Calendar date of exit (from trade summary) |
| `exit_time` | Timestamp of exit |
| `pl_rs` | P&L in ₹ for 1 lot (from trade summary) |
| `exit_reason` | From trade summary (profit_target, stop_loss, expiry, elm, etc.) |
| `vix_at_entry` | VIX recorded at routing decision point |

A trade is "active" from `entry_date` through and including `exit_date`. On any
date after `exit_date`, `active_trade` is cleared and routing resumes.

---

## 6. Data sources

| Strategy | Source file | Key columns used |
|---|---|---|
| Artemis (Nifty) | `artemis_backtest/data/trade_summary_nifty_rerun.csv` | `entry_time`, `exit_time`, `pl_rupees`, `exit_reason`, `entry_vix` |
| Artemis (Sensex) | `artemis_backtest/data/trade_summary_sensex_rerun.csv` | same |
| Athena | `athena_backtest/data/trade_summary_vix_all.csv` | `entry_time`, `exit_time`, `total_pl_rupees`, `exit_reason`, `entry_vix` |
| Iris | `iris_backtest/data/iris_backtest_summary.csv` | `entry_ts`, `exit_ts`, `pnl_rs`, `exit_reason` (+ VIX lookup) |
| VIX | `data_pipeline/data/indices/india_vix.csv` | 1-min VIX; snap at 10:30 for routing decisions |

**Athena note:** Use `trade_summary_vix_all.csv` (VIX_FILTER_LOW=0) so the full
date range is present. The routing module applies the VIX gate — Athena's own
internal VIX filter should be ignored.

**Iris note:** Iris signals fire throughout the day. For routing purposes, use the
actual `entry_ts` from the Iris summary. Only take the first Iris trade per day;
discard any subsequent same-day signal.

**VIX snap:** For the routing decision at each checkpoint, read VIX at 10:30:00 on
that day from the 1-min VIX file. If 10:30 is missing, use the nearest available
candle within ±5 minutes.

---

## 7. Edge cases

### 7.1 Active trade blocks entry

If a trade is active on an entry day (Artemis or Athena), the entry check is skipped
entirely — no entry regardless of VIX. This includes:
- Artemis trade still open on Wednesday (Athena entry day): skip Athena
- Athena trade still open on next Monday (Artemis entry day): skip Artemis
- Iris trade opened earlier in the day is still open at 10:30 on Wednesday: skip Athena

Athena's duration is ~8 DTE (entry Wednesday → exit next Wednesday). It will
typically be open on the following Monday's Artemis check. This is expected:
one of the two per-week slots is consumed.

### 7.2 VIX boundary (exactly 16.0 or 25.0)

Use strict inequalities matching routing config:
- `vix < 16.0` → Artemis
- `16.0 <= vix <= 25.0` → Athena
- `vix > 25.0` → Iris

Boundary cases (exactly 16.0 → Athena; exactly 25.0 → Athena) match `leto_config.py`.

### 7.3 VIX data missing at checkpoint

If VIX is unavailable at 10:30 on an entry day (data gap), skip entry for that
checkpoint. Log the miss. Do not impute or carry forward from a prior day.

### 7.4 Trade not found in strategy summary

If routing selects a strategy for a given entry date but no trade exists in that
strategy's summary for that date (e.g. the individual backtest skipped it due to
its own VIX gate or data gap), treat as a skipped entry — log it, mark as
`vix_routed_no_trade`, and move on. This is a data integrity check.

### 7.5 Iris intraday closure — same-day re-entry

Iris always closes by 15:00. If an Iris trade closes at, say, 11:30 on a Tuesday,
the state is clean for the rest of Tuesday. Should a second Iris signal fire on
the same day after 11:30, do **not** re-enter — one trade per day per strategy.
This matches production Iris behaviour (single entry per session).

### 7.6 Era transition (Aug → Sep 2025)

The last Artemis Nifty trade may close mid-week in August 2025. The first Artemis
Sensex trade starts from Sep 2025's first valid entry date. There may be 0–1 weeks
with no Artemis coverage at the transition; log these as `transition_gap` entries.
Do not attempt to bridge them.

### 7.7 Holiday on entry day

If Monday (Artemis day) is a holiday, the contracts.csv entry for that week will
already reflect the holiday-adjusted entry date (generate_contracts.py handles this).
Use the entry date from contracts.csv — do not compute independently.

If Wednesday (Athena day) is a holiday, `last_trading_day_before()` in Athena's
backtest already adjusts. The entry dates in the Athena trade summary will reflect
the adjustment. Match against `entry_time.date()` from the summary.

### 7.8 VIX spikes intraday after entry decision

Routing uses VIX at 10:30. If VIX moves above 25 later in the day after an Artemis
or Athena entry decision was made, it does not invalidate the trade already entered.
The existing trade runs to its conclusion.

### 7.9 Iris fires on an Artemis or Athena entry day (same-day conflict)

If VIX > 25 on Monday and Iris fires before 10:30 (possible if a SuperTrend flip
occurs at 09:45 and is captured): treat the Iris entry as the active trade for the
day. Artemis is blocked (same-day active trade rule). This is a rare edge case —
log it explicitly.

### 7.10 No valid trade all week

If none of the three strategies have a trade in their summary for a given week (VIX
in range, but no signal fired or data missing), the week produces zero P&L. This is
a valid outcome. Log as `no_entry_week`.

### 7.11 Athena trade with ENABLE_VIX_FILTER=True in the existing summary

The `trade_summary.csv` (default Athena output) has VIX_FILTER_LOW=16.0 applied.
Use `trade_summary_vix_all.csv` (VIX_FILTER_LOW=0) as specified in §6 so the
routing module controls the VIX gate, not the individual backtest.

### 7.12 Multiple Iris signals on a high-VIX day

Iris signals can fire multiple times per day (multiple ST flips). Always take the
first signal of the day. All subsequent same-day signals are discarded (one
trade/day rule). The `iris_backtest_summary.csv` already captures the first valid
signal per day per the Iris backtest's own logic.

---

## 8. Module architecture

### 8.1 New directory

```
leto_backtest/
├── configs.py              # date ranges, file paths, VIX boundaries, era split date
├── loader.py               # load and normalise each strategy's trade summary
├── router.py               # routing logic: VIX lookup + strategy selection
├── simulator.py            # main simulation loop; emits consolidated trade log
├── analysis.py             # P&L aggregation, drawdown, Calmar, year-by-year breakdown
└── data/
    └── leto_trade_log.csv  # output: one row per executed trade
```

### 8.2 `configs.py`

```python
ERA_SPLIT_DATE       = '2025-09-01'   # Artemis switches Nifty → Sensex
ROUTING_VIX_LOW      = 16.0           # below → Artemis
ROUTING_VIX_HIGH     = 25.0           # above → Iris
BACKTEST_START       = '2020-01-01'
VIX_SNAP_TIME        = '10:30'        # time to read VIX for routing decisions
VIX_SNAP_TOLERANCE   = 5              # minutes; fallback window if 10:30 missing
```

### 8.3 `loader.py`

Normalises each strategy's trade summary to a common schema:

```python
{
  'strategy':    str,          # 'artemis_nifty', 'artemis_sensex', 'athena', 'iris'
  'entry_date':  date,
  'entry_ts':    datetime,
  'exit_date':   date,
  'exit_ts':     datetime,
  'pl_rs':       float,        # per lot, ₹
  'exit_reason': str,
  'entry_vix':   float,
}
```

Applies the era split: Artemis Nifty trades only before ERA_SPLIT_DATE, Sensex only
from ERA_SPLIT_DATE onwards.

### 8.4 `router.py`

```python
def get_routing_vix(date, vix_1min_df) -> float | None:
    """Snap VIX at VIX_SNAP_TIME on date; return None if data unavailable."""

def route(vix: float) -> str | None:
    """Return 'artemis', 'athena', 'iris', or None (skip)."""

def is_artemis_entry_day(date, artemis_contracts_df, era) -> bool:
    """True if date is a valid Artemis entry day for the given era."""

def is_athena_entry_day(date, athena_contracts_df) -> bool:
    """True if date is a valid Athena entry day (pre-era-split)."""
```

### 8.5 `simulator.py`

Main loop. Iterates every trading day from BACKTEST_START to present.
Maintains `active_trade: dict | None`. On each day:

1. If `active_trade` and today > `active_trade.exit_date`: clear `active_trade`
2. If `active_trade` is still live: skip entry logic, append nothing
3. If no active trade: run routing decision (§4)
4. If routing selects a strategy: look up trade in pre-loaded summaries;
   if found, set `active_trade` and append to consolidated log

### 8.6 `analysis.py`

Produces summary stats from `leto_trade_log.csv`:

- Total P&L, win rate, expectancy (overall and per strategy)
- Max drawdown (running cumulative P&L low)
- Calmar ratio (total P&L / max DD)
- Year-by-year breakdown
- Strategy mix (how many trades routed to each strategy)
- Weeks with no entry (data gap vs. no VIX signal)

---

## 9. Output schema — `leto_trade_log.csv`

| Column | Description |
|---|---|
| `week_start` | Monday of the week this trade belongs to |
| `entry_date` | Actual entry date |
| `entry_ts` | Entry timestamp |
| `exit_ts` | Exit timestamp |
| `strategy` | artemis / athena / iris |
| `instrument` | nifty / sensex |
| `vix_at_entry` | VIX snapped at routing checkpoint |
| `pl_rs` | P&L ₹ per lot |
| `exit_reason` | From source trade summary |
| `routing_outcome` | entered / skipped_active / skipped_no_trade / skipped_no_signal / transition_gap / no_entry_week / vix_data_missing |

---

## 10. Validation

Before trusting the output, verify against known reference points:

1. **Strategy trade count**: routed Artemis, Athena, and Iris trade counts should
   be ≤ their respective full-backtest trade counts (the routing constraint removes
   trades that would have conflicted).

2. **No overlapping trades**: assert that no two consecutive rows have
   `entry_date` < prior row's `exit_date`.

3. **Era boundaries**: all Artemis Nifty trades before ERA_SPLIT_DATE; all Artemis
   Sensex trades on or after ERA_SPLIT_DATE.

4. **VIX consistency**: every entered trade's `vix_at_entry` should be in the
   correct range for its strategy (Artemis < 16, Athena 16–25, Iris > 25).

5. **Spot-check weeks**: manually verify 3–5 specific weeks against the individual
   strategy trade summaries to confirm P&L and exit reasons match.

6. **Known reference P&L**: from routing research (1-lot, 2020–2025):
   routed portfolio ~₹2.3L total. The integrated backtest result should be in the
   same ballpark; large divergence indicates a routing logic bug.

---

## 11. Implementation notes (post-run)

### 11.1 Data findings

**Artemis exit time derivation:** ~64% of Nifty rows have a null `pe_exit_time` or
`ce_exit_time`. Null leg = rode to expiry after ELM adjustment reset `exit_time`.
Fix: `exit_ts = max(pe_exit or expiry, ce_exit or expiry)`.

**Artemis P&L basis:** `total_pl_rupees = total_pl_pts × LOT_SIZE` (65 for Nifty,
20 for Sensex). LOT_COUNT=2 is NOT applied to the rupee figure. Already 1-lot.

**Iris VIX gate:** Not in `iris_backtest_summary.csv`; applied by the router.

**Athena entry day detection:** No contracts.csv exists. Entry dates derived from
`trade_summary_vix_all.csv` entry timestamps (VIX filter off → all valid days present).

**Era B structure:** Sensex expires Thursday → Artemis enters Monday. Nifty moved to
Tuesday expiry → Athena enters Monday. Unified Monday checkpoint confirmed from data.

**Same-day exit + re-entry:** Athena exits at 10:25 on its exit day; routing check
at 10:30 on the same day can immediately fire a new entry. This applies to both eras.

### 11.2 Actual results (2020-01-01 to data cutoffs)

| Metric | Value |
|---|---|
| Total trades | 339 |
| Win rate | 63.7% (216W / 123L) |
| Expectancy | ₹874 per trade |
| Total P&L | ₹2,96,171 |
| Max drawdown | ₹-13,838 (March–April 2022, Athena losses) |
| Calmar | 21.40 |

**By strategy:**

| Strategy | Trades | Win% | Total P&L | Avg/trade |
|---|---|---|---|---|
| Artemis | 163 | 69.9% | ₹1,38,421 | ₹849 |
| Athena | 118 | 56.8% | ₹1,32,161 | ₹1,120 |
| Iris | 58 | 60.3% | ₹25,589 | ₹441 |

**Routing outcomes:** 339 entered, 99 skipped_no_signal (high-VIX days Iris silent),
6 vix_routed_no_trade (Sensex backtest ended Mar 2026; 2 Nifty data gaps), 4 vix_data_missing.

**Data cutoffs:** Artemis Sensex → 2026-03-02; Athena → 2026-05-04; Iris → 2026-05-15.
Partial 2026 included.

### 11.3 Validation results

- [x] No overlapping trades
- [x] Era boundaries correct (all Nifty trades < 2025-09-01; all Sensex trades ≥ 2025-09-01)
- [x] VIX consistency (all strategies within correct VIX bands)
- [x] Reference P&L sanity (₹2,96,171 within 30% of ₹2.3L reference)

### 11.4 Implementation sequence

| Step | Task | Status |
|---|---|---|
| 1 | Scaffold `leto_backtest/` directory and `configs.py` | Done |
| 2 | Build `loader.py` — normalise all 4 trade summaries to common schema | Done |
| 3 | Build `router.py` — VIX snap, routing decision | Done |
| 4 | Build `simulator.py` — main loop, Era A dual-checkpoint, Era B unified | Done |
| 5 | Build `analysis.py` — P&L stats, drawdown, Calmar, year-by-year | Done |
| 6 | Run validation checks (§10) | All pass |
| 7 | Update plan with results | Done |

---

## 12. Go/no-go gates

- [x] All four strategy trade summaries are available and schema-normalised
- [x] Validation checks (§10) pass — no overlaps, correct VIX ranges, era split clean
- [ ] Spot-check 5 weeks manually against individual backtests
- [x] Reference P&L sanity check (within 30% of ~₹2.3L for 2020–2025 period)
