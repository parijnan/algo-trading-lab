# Prometheus — MCX Crude Oil Intraday Trend-Following

Intraday trend-following strategy for MCX crude oil futures (CRUDEOILM primary, CRUDEOIL
cross-validation), built on Supertrend flip signals. Named for the fire-bringer, fitting for
a crude oil / energy strategy, per the repo's Greek-mythology naming convention. Two design
phases live here — v1 (superseded) and Phase 2 (active) — both backtest-only; production
build is planned but not yet implemented (see [`plans/prometheus-phase2-production.md`](../plans/prometheus-phase2-production.md)).

## Data

- `data_pipeline/data/mcx/{CRUDEOILM,CRUDEOIL}/<expiry>_futures.csv` — 1-minute OHLCV,
  one file per contract, stitched across expiry rolls by `load_futures_1min()`. No
  back-adjustment needed: the strategy is pure intraday, so no position ever spans a roll —
  each day's bars belong to whichever contract was genuinely front-month that day.
- Current coverage: 2026-01-30 to 2026-08-28 (151 full trading days). 2026-08-28's daytime
  bars (09:00–15:15) were backfilled by `data_downloader_mcx.py` on 2026-08-29, joining
  seamlessly with the evening session `mcx_live_downloader.py` had already captured live
  (15:16–23:29) — verified gapless (0 missing minutes, 0 duplicate timestamps) before rerunning.
- Lot sizes and tick size looked up live from `data_pipeline/data/mcx_instrument_master.csv`,
  never hardcoded: CRUDEOILM = 10 barrels/lot, CRUDEOIL = 100 barrels/lot. The instrument
  master's `tick_size=100` field is in Angel One's paise-scaled convention — actual tick is
  ₹1.00 = 1 price point, matching the whole-number prices already in the data.

## Phase 1 (v1) — dual-timeframe, hold-till-flip

Folder: `prometheus_backtest/` (root files — `configs.py`, `data_loader.py`, `backtest.py`,
`analysis.py`, `run.py`, `sweep.py`, `trade_paths.py`).

**Architecture**: deliberately mirrors `iris_production/iris.py`'s live watching→in_trade→watching
state machine rather than a vectorized precompute-then-scan approach (explicit design direction).
5-min entry-timeframe Supertrend flip, gated by 15-min regime-timeframe alignment — the same
dual-timeframe structure Iris uses live. Single position, held until an opposing flip or the
session close forces a square-off. No stop-loss or profit target in the baseline — cost-free,
signal-only, to validate the raw edge before layering in exit calibration.

**Two correctness bugs found and fixed via review before any result was trusted:**
- **Look-ahead in the 15-min regime filter**: originally checked `regime_series.index <= ts`,
  which reads a left-labelled 15-min bar as available the instant its label-timestamp is
  reached rather than waiting for it to actually close — letting entry decisions peek up to
  ~10 minutes into a still-forming regime bar. Fixed to require the regime bar's own close
  (`regime_ts + REGIME_TF_MIN`) to be ≤ the decision timestamp, mirroring `iris.py`'s real
  `_update_15m_regime` behaviour.
- **Spurious first-bar Supertrend flip**: the very first bar where Supertrend leaves its NaN
  warm-up period always reads as a flip (comparing against a preceding `NA` value) — explicitly
  zeroed out, not a genuine regime change.

**Baseline results** (current data, 2026-01-30–2026-08-27): 188 trades, 38.3% win rate,
₹11,420 total P&L, −₹16,820 max drawdown, Calmar 0.68 (unitless: total P&L ÷ |max DD|, not
annualized).

**Calibration sweep** (`sweep.py`): tested SL and profit-target in both points and % terms,
plus a flat-vs-pivot target2 control. A stop-loss meaningfully improved results — non-monotone
in absolute points (a real sweet spot near 60–75 points, an unexplained bad valley at
90–130 points), noticeably smoother and broader in % terms. This sweep is also what surfaced
the limitation that shaped Phase 2's whole redesign: CRUDEOILM's entry price ranged
₹5,617–10,683 over the backtest window (CV 16.6%), so any fixed-points threshold is roughly a
2× swing in proportional size depending on where price happens to sit. That observation, plus
an explicit user request for a scale-out design, is why development moved to Phase 2 rather
than continuing to tune v1's single-position architecture.

v1 is retained as-is for reference and comparison — **not actively developed further**.
Phase 2 is the live calibration target.

## Phase 2 — two-lot scale-out (active)

Folder: `prometheus_backtest/phase2/`.

**Design** (user-specified 2026-08-27): single-timeframe ST_15 signal (no regime gate — this
is a genuine architectural departure from v1, not just a different outcome of the same
structure). Entry with 2 independently-managed 1-lot legs. Lot 1 books at a fixed distance
(`target1`); lot 2 books at a second, farther target — originally the nearest daily
pivot/resistance/support level beyond `target1`, later found to be beaten by a flat %
distance (see calibration journey below). SL is a single shared stop protecting whichever
lot(s) remain open, checked ahead of trend-flip on any same-bar tie. Trend-flip remains the
fallback SL and doubles as the next entry signal in the opposite direction — same flip, same
bar, resolved together at the next bar's open. Entries are gated by `MIN_ENTRY_TIME` (checked
at the **fill** bar, not the signal bar — see the bug note below) and
`MAX_ENTRY_BEFORE_CLOSE_MIN` (measured relative to each trading day's own actual last bar, not
a fixed clock time — MCX's nominal 23:30 close actually lands on 23:15/23:30/23:45 depending
on the day, confirmed from the data). EOD square-off uses the same relative-to-actual-close
design, `EOD_SQUAREOFF_BEFORE_CLOSE_MIN` minutes before whatever that day's last bar turns out
to be.

**Files:**
- `configs_p2.py` — sole parameter source
- `data_loader_p2.py` — reuses v1's `load_futures_1min`/`resample_ohlcv`/`compute_st`, adds `compute_daily_pivots`
- `backtest_p2.py` — the two-lot state machine
- `trade_paths_p2.py` — per-trade 1-min logs with lot1/lot2/total running P&L (mark-to-market
  until each lot's own exit, frozen at the realized value from that point on) plus running MAE/MFE
- `analysis_p2.py` — consolidated stats, lot1/lot2 hit-rate reporting
- `run_p2.py` — entry point
- `sweep_p2.py` — calibration grids (points and %; each grid is explicitly pinned to its
  intended conditions so it can't silently drift if `configs_p2.py`'s defaults change later)

**Calibrated/recommended config** (current defaults in `configs_p2.py`, cross-validated on
both CRUDEOILM and CRUDEOIL): `THRESHOLD_MODE='pct'`, `SL_PCT=1.8`, `TARGET1_PCT=1.0`,
`TARGET2_MODE='flat_pct'`, `TARGET2_FLAT_PCT=2.3`.

**Current result** (219 trades, 2026-01-30–2026-08-28): 55.7% win rate, ₹44,693 total P&L,
−₹14,943 max drawdown, Calmar 2.99 (same unitless definition as v1 — not annualized). The two
newest trades (2026-08-28, both MCX-evening-only entries at 18:30 and 22:15) were both losers,
net −₹2,360 — pulled the total down from the prior ₹47,053 snapshot.

**On a ₹1,00,000 allocated-capital basis** (the user's own sizing call, accounting for ~₹25k
margin/lot × 2 lots plus a drawdown buffer): 44.69% return on capital over the backtest's
0.575-year window, **77.73% annualized** (simple/linear annualization, appropriate since the
strategy trades a fixed 2-lot size rather than compounding with account growth — not a
compounded CAGR), max drawdown −14.94% of capital, and a properly-annualized Calmar
(annualized return % ÷ max DD%) of **5.20**. Lowest account value reached: ₹92,540
(−7.46% from start), 2026-02-17 — unchanged, the Aug-28 losses didn't deepen the worst drawdown.

### Calibration journey — why the config landed where it did

1. **Two correctness bugs fixed before trusting any calibration.** `MIN_ENTRY_TIME` was
   originally checked against the *signal* bar's own timestamp, rejecting a flip on a day's
   first 09:00 bar even though its fill at 09:15 is legitimately at/after 09:15 — fixed to
   check the fill bar instead (191 → 217 trades, ₹40,639 → ₹45,961 on the recommended
   config). The profit-target fill-price helper (`_target_fill_price`) is correctly
   *asymmetric* from the stop-loss fill-price helper (`_stop_fill_price`) — a stop's
   gap-through is worse for the trade, a target's is better — caught in review before the
   stop-loss sweep ran, not discovered afterward from bad numbers.
2. **SL/target1/target2 swept in both points and %.** Points-based SL showed a real but
   jagged, non-monotone response. Switching to %-based thresholds produced a materially
   smoother, broader-plateau response — evidence the % framing fits the data better, not just
   a relabeling of the same optimum found in points.
3. **Target2 mechanism reconsidered.** A flat %-distance target2 (no pivot lookup at all)
   beat the original pivot-based target2 once thresholds were expressed proportionally —
   cross-validated on both CRUDEOILM and CRUDEOIL, a genuine reversal from points-mode testing
   (where pivots had beaten a flat-points control). `TARGET2_MODE='flat_pct'` is the current
   default; `'pivot'` remains fully implemented and selectable if this is worth revisiting later.
4. **`TARGET1_PCT=1.75%` showed the single best backtested P&L but was rejected as
   unreliable.** Checked its improvement over 1.0% by price tercile and found ~70–75% of the
   gain concentrated in the series' highest-price third, replicated independently on both
   CRUDEOILM and CRUDEOIL — a real regime-dependency, not noise. `TARGET1_PCT=1.0` was kept as
   the steadier default.
5. **Every major finding cross-validated on CRUDEOIL** (the full-size contract, lot size 100
   vs. CRUDEOILM's 10) before being trusted — matches the original design requirement that a
   successful strategy must translate from the mini to the full-size contract, not just work
   on the instrument it happened to be tuned on.

### Supporting analysis

- **MAE/MFE distribution** (points and % of entry price) — published artifact (private,
  requires the account owner's access): `https://claude.ai/code/artifact/a91bd410-81e9-4590-9445-6853448c55f0`.
  MAE separates winners from losers more cleanly than MFE does (corr −0.746/−0.780 vs.
  +0.618/+0.634 in points/%), and a fixed 100-point target would have been 0.94%–1.78% of
  entry depending on price level — the concrete evidence behind the pct-mode redesign.
- **Drawdown analysis** — published artifact (private):
  `https://claude.ai/code/artifact/ed3ff713-f354-410c-be19-af817410983e`. 15 distinct drawdown
  episodes; two of them (April, 33 days; June–July, 56 days) account for most of the
  drawdown-days and align with the two weak months in the month-by-month P&L breakdown.
  Sitting at a new equity peak with no open drawdown as of the last trade in the series.
- **Single-lot side-project comparison** (`research/prometheus_p2_single_lot/`): trading only
  lot 1's mechanics (1% target, 1.8% SL, identical entry/EOD/trend-flip rules) — 217 trades,
  62.2% win rate, ₹18,664 total P&L, −₹7,657 max drawdown, Calmar 2.44. Cross-validated
  byte-for-byte against the two-lot backtest's own lot1 column before being trusted. Confirms
  lot 2 isn't just doubling size — it specifically captures bigger trending moves a single 1%
  exit structurally can't reach (two-lot total P&L is ~2.46× single-lot's, not ~2×). Decision:
  keep 2 lots.

### Not yet done / open threads

- CRUDEOIL production deployment — backtest cross-validation only so far; CRUDEOILM is the
  day-1 production target.
- Production build itself — see [`plans/prometheus-phase2-production.md`](../plans/prometheus-phase2-production.md)
  for the full architecture (Supertrend seeding, order execution and fill tracking, resilient
  candle polling, state file / crash recovery, Slack reporting). Not yet implemented.
- `TARGET1_PCT` joint-combined tests beyond the 1.0%/1.75% grid already run.

## Running

```bash
python prometheus_backtest/run.py              # v1 baseline
python prometheus_backtest/sweep.py             # v1 calibration grids
python prometheus_backtest/phase2/run_p2.py     # Phase 2, current recommended config
python prometheus_backtest/phase2/sweep_p2.py   # Phase 2 calibration grids
```

Symbol switch: `SYMBOL` in `configs.py` / `configs_p2.py` — `'CRUDEOILM'` (default, primary
calibration target) or `'CRUDEOIL'` (cross-validation, full-size contract).

All generated output (`data/`, `data_sweep/`, per-trade logs) is gitignored — every number in
this README was verified against a fresh run of the current code, not carried over from
memory of an earlier session.
