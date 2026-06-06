# Plan: TRIPLE_CONFIRM Live Integration — Athena

## Context

The Athena TC backtest (`backtest_tc.py`) initially showed +55.6% improvement over baseline. A date-keyed
lookahead bug was found and fixed (2026-06-06): the corrected improvement is **+11.1% (₹16,490)**, entirely
driven by two macro-event outliers (Budget 2021 CE, Jan 2022 PE). Non-outlier fires are net-negative.

This plan is filed for future reference. **Live implementation is not currently justified** — the edge is
not robust enough. Revisit if the signal's characteristics improve or the outlier-driven result can be
explained structurally.

---

## Critical Finding: Intraday Lookahead in Backtest

**Where:** `athena_backtest/backtest_tc.py:_build_tc_signal_map()` and per-candle consumption.

**Bug:** The signal map is date-keyed:
```python
# current code
df['date'] = df['signal_ts'].dt.date
return dict(zip(df['date'], df['direction']))   # {date → direction}

# per-candle check
tc_dir = tc_signal_map.get(ts.date())           # ts is 10:30 candle
```
A TC signal firing at 12:48 makes the 10:30 candle on that same date already see it. This is intraday lookahead.

**Impact on the two outlier trades (71% of total delta):**
| Trade | Actual TC signal_ts | Backtest entered chute at | Lookahead |
|---|---|---|---|
| 2021-01-27 CE chute | 2021-02-01 12:48 (bullish) | ~10:30 on 2021-02-01 | ~2.3 hrs |
| 2022-01-19 PE chute | 2022-01-20 13:00 (bearish) | ~10:30 on 2022-01-20 | ~2.5 hrs |

Both are highly directional days. Early entry captures a materially larger move. **The +55.6% headline was overstated.**

**✅ DONE (2026-06-06):** Fix applied in `backtest_tc.py`. Corrected result: +₹16,490 (+11.1%).

---

## Step 1: Fix Backtest Lookahead ✅ COMPLETE

**File:** `athena_backtest/backtest_tc.py`

Change signal map to be timestamp-keyed. Per-candle, only signals with `signal_ts <= ts` are visible.

```python
# New _build_tc_signal_map — returns list of (signal_ts, direction), sorted
def _build_tc_signal_map(path):
    df = pd.read_csv(path, parse_dates=['signal_ts'])
    return df[['signal_ts', 'direction']].sort_values('signal_ts').to_records(index=False)

# Per-candle lookup — find latest signal on or before current ts
def _get_tc_dir(signals, ts):
    """Return direction of last signal fired on or before ts, or None."""
    eligible = [(sig_ts, d) for sig_ts, d in signals if sig_ts <= ts]
    return eligible[-1][1] if eligible else None
```

Replace all `tc_signal_map.get(ts.date())` calls (lines 238 and 285) with `_get_tc_dir(tc_signals, ts)`.

✅ Done. Corrected result: baseline ₹149,130 → TC ₹165,620, delta **+₹16,490 (+11.1%)**.
36 TC fires (was 43); win rate 15/36 = 41.7% (majority of fires worse than baseline).
Top 2 outliers = +₹51,535; all other 34 fires = −₹35,045.

---

## Step 2: TC Signal Engine in Athena Production

**Why inside Athena:** VIX routing (`VIX_ARTEMIS_MAX=16`, `VIX_ATHENA_MAX=25`) means Iris and Athena are mutually exclusive. Athena must compute TC itself.

**Pattern to reuse:** `iris_production/iris.py` already fetches closed candles from the broker API at each bar boundary (`_fetch_candle` → `fetch_candles` in `iris_production/functions.py`) and updates SuperTrend incrementally. The same approach applies here.

**New file:** `athena_production/tc_signal.py`

```python
class TCSignalEngine:
    """
    Computes TRIPLE_CONFIRM in real-time from broker candle API.
    TC = ORB_75 (75 1-min bars from 09:15) + ST_FAST (5m+15m) + ST_RAPID (3m+9m)
    all aligned in the same direction.
    Fires at close of 3-min ST_RAPID flip bar when all three layers agree.
    Only fires after 10:30 (earliest possible ORB_75 completion).
    """
    def seed(self, now): ...    # fetch candle history from 09:15, compute initial state
    def update(self, now): ...  # called at each 1-min bar close; returns (ts, dir) or None
```

Reuse from `iris_production/functions.py`: `fetch_candles`, `_candles_to_df`, `seed_st`, `compute_st`.
Reuse logic from `iris_backtest/signals/triple_confirm.py` (adapted for incremental use).

The engine keeps `last_signal: tuple[datetime, str] | None` for the current day, resetting at midnight. No persistence needed — rebuilt from broker candles on restart.

---

## Step 3: Athena Main Loop Wiring

**File:** `athena_production/athena_engine.py`

At init/seed: instantiate and seed `TCSignalEngine`.

In main loop at each 1-min bar close, before `_manage_emergency_hedge`:
```python
new_tc = self.tc_engine.update(now)
if new_tc:
    self._tc_signal = new_tc   # (signal_ts, direction)
    self._slack(f'TC {new_tc[1]} @ {new_tc[0].strftime("%H:%M")}')

self._manage_emergency_hedge(spot)
self._manage_pe_chute()
```

`self._tc_signal` holds the latest TC signal for today. Cleared at trade entry and not carried across calendar days.

---

## Step 4: CE Parachute Modification

**File:** `athena_production/athena_engine.py:_manage_emergency_hedge()`

Add TC path alongside existing reactive spot path. Add `emer_via_tc` flag (new state field).

```python
tc_dir = self._tc_signal[1] if self._tc_signal else None

# Entry
tc_entry   = (tc_dir == 'bullish')
spot_entry = (spot >= self.state.ce_sell_strike + EMERGENCY_TRIGGER_OFFSET)
if force or tc_entry or spot_entry:
    # ... existing buy logic ...
    self.state.emer_via_tc = tc_entry and not spot_entry

# Exit
tc_exit   = (tc_dir == 'bearish')
spot_exit = (not self.state.emer_via_tc) and (spot <= self.state.ce_sell_strike + EMERGENCY_EXIT_OFFSET)
if force or tc_exit or spot_exit:
    # ... existing close logic ...
    self.state.emer_via_tc = False
```

Key invariant: a TC-triggered CE chute exits only on bearish TC. A spot-triggered CE chute exits only on spot reversal.

---

## Step 5: PE Parachute (New in Production)

**File:** `athena_production/athena_engine.py` — new method `_manage_pe_chute()`

```python
def _manage_pe_chute(self):
    tc_dir = self._tc_signal[1] if self._tc_signal else None

    # Entry: bearish TC only
    if not self.state.pe_chute_active and self.state.pe_chute_attempts < EMERGENCY_MAX_ATTEMPTS:
        if tc_dir == 'bearish':
            # 1. Sell PE wing (lock proceeds to running_realised_pl)
            # 2. Buy PE chute at EMERGENCY_HEDGE_DELTA (0.35)
            # 3. pe_chute_active=True, pe_wing_active=False

    # Exit: bullish TC only (false signal reversal)
    elif self.state.pe_chute_active:
        if tc_dir == 'bullish':
            # 1. Sell PE chute (lock P&L to running_realised_pl)
            # 2. Rebuy PE wing at SAFETY_WING_DELTA (0.05)
            # 3. pe_chute_active=False, pe_wing_active=True
```

At `_execute_exit()`: if `pe_chute_active` at ELM/pre-expiry, close PE chute before spread legs (no rebuy — trade is ending).

---

## Step 6: State Additions

**File:** `athena_production/state.py` — `AthenaState` dataclass

```python
emer_via_tc:        bool  = False   # True if CE chute entered via TC (not reactive spot)
pe_chute_active:    bool  = False
pe_chute_strike:    str   = ''
pe_chute_token:     str   = ''
pe_chute_symbol:    str   = ''
pe_chute_entry:     float = 0.0
pe_chute_attempts:  int   = 0
```

All fields persist in `athena_state.csv`. On restart with `pe_chute_active=True`, broker position reconciliation for the PE chute token (same pattern as `emer_active`).

---

## Step 7: Config Additions

**File:** `athena_production/configs_live.py`

```python
ENABLE_TC_SIGNAL  = True
TC_SIGNAL_CUTOFF  = time(14, 45)   # ignore TC fires after this (ELM too close)
```

No changes to existing `EMERGENCY_TRIGGER_OFFSET`, `EMERGENCY_HEDGE_DELTA`, `EMERGENCY_MAX_ATTEMPTS`. PE chute reuses them.

---

## Verification

1. **Fix backtest first** (Step 1): ✅ Complete — corrected improvement is +11.1%. Edge is marginal and outlier-driven; live implementation deferred pending further investigation.

2. **Unit test TC engine**: Run `TCSignalEngine` against historical 1-min data; compare output to `TRIPLE_CONFIRM_excursions.csv`. Signal timestamps must match exactly.

3. **Paper session**: Run for ≥ 3 TC fires. Verify:
   - TC fires at the correct timestamp (not start-of-day)
   - `emer_via_tc` correctly gates CE chute exit condition
   - PE wing closes and PE chute opens on bearish TC; reverses on bullish TC
   - State CSV persists correctly through each event
   - All events appear on Slack with timestamps

4. **Live go/no-go gate**: Only after corrected backtest numbers are reviewed and paper session confirms correct timing.

---

## Files

| File | Change |
|---|---|
| `athena_backtest/backtest_tc.py` | Fix lookahead: timestamp-keyed signal map |
| `athena_production/tc_signal.py` | New — TCSignalEngine |
| `athena_production/athena_engine.py` | TC wiring in main loop; CE chute; PE chute |
| `athena_production/state.py` | 7 new fields |
| `athena_production/configs_live.py` | 2 new constants |
