# Prometheus — MCX Crude Oil Intraday Trend-Following

Intraday trend-following strategy for MCX crude oil futures (CRUDEOILM primary, CRUDEOIL
cross-validation), built on Supertrend flip signals. Named for the fire-bringer, fitting for
a crude oil / energy strategy, per the repo's Greek-mythology naming convention. Three design
phases live here — v1 (superseded), Phase 2 (active, session-bound 2-lot scale-out), and
Phase 3 (in progress, positional 2-lot scale-out — decision pending between two calibrated
multiplier candidates, see below) — all backtest-only; production build (based on Phase 2) is
planned but not yet implemented (see [`plans/prometheus-phase2-production.md`](../plans/prometheus-phase2-production.md)).

## Data

- `data_pipeline/data/mcx/{CRUDEOILM,CRUDEOIL}/<expiry>_futures.csv` — 1-minute OHLCV,
  one file per contract, stitched across expiry rolls by `load_futures_1min()`. No
  back-adjustment needed: the strategy is pure intraday, so no position ever spans a roll —
  each day's bars belong to whichever contract was genuinely front-month that day.
- Current coverage: 2026-01-30 to 2026-09-01 16:17 IST (152 trading days, the most recent a
  partial in-progress session — refreshed 2026-09-01, confirmed against
  `load_futures_1min()`'s own front-month-resolved max timestamp before trusting it, not just
  a raw file's last row). 2026-08-28's daytime bars (09:00–15:15) were backfilled by
  `data_downloader_mcx.py` on 2026-08-29, joining seamlessly with the evening session
  `mcx_live_downloader.py` had already captured live (15:16–23:29) — verified gapless (0
  missing minutes, 0 duplicate timestamps) before rerunning.
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

**Current result** (221 trades, 2026-01-30–2026-09-01, refreshed through the latest candle):
55.2% win rate, ₹42,453 total P&L, −₹14,943 max drawdown, Calmar 2.84 (same unitless
definition as v1 — not annualized). Max drawdown is unchanged from the prior ₹44,693/219-trade
snapshot — the two new trades didn't deepen the worst episode, just diluted the total P&L and
win rate slightly.

**On a ₹1,00,000 allocated-capital basis** (the user's own sizing call, accounting for ~₹25k
margin/lot × 2 lots plus a drawdown buffer): 42.45% return on capital over the backtest's
0.586-year window, **72.51% annualized** (simple/linear annualization, appropriate since the
strategy trades a fixed 2-lot size rather than compounding with account growth — not a
compounded CAGR), max drawdown −14.94% of capital, and a properly-annualized Calmar
(annualized return % ÷ max DD%) of **4.85**. Lowest account value reached: ₹92,540
(−7.46% from start), 2026-02-17 — unchanged across every refresh so far.

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

## Phase 3 — positional 2-lot scale-out (decision pending)

Folder: `prometheus_backtest/phase3/`.

**Motivation** (user, 2026-09-01): `ST_PERIOD=10`/`ST_MULTIPLIER=3.0` was never actually
calibrated for Prometheus — `configs_p2.py`'s own docstring says "same day-1 starting values
as Iris/Prometheus v1, not yet calibrated for this design specifically", and `sweep_p2.py`
computes the Supertrend series once, before its sweep loop, so every Phase 2 calibration pass
held the entry signal itself fixed and never questioned it. Crude's cleaner trending character
(vs. Nifty/Sensex, which 10,3 actually *was* tuned for, via Iris) is a real, testable reason to
suspect a different multiplier suits it better.

**Design, deliberately decoupled in two stages:**
1. **Raw signal-quality sweep first** (`backtest_p3.py`, `sweep_p3.py`) — no SL, no profit
   target, no EOD square-off. The only exit is the opposite Supertrend flip; a position can
   hold overnight, across multiple days, even across a contract roll. 1 lot, no scale-out.
   Every trade gets a minute-by-minute log (`trade_paths_p3.py`) tracking running MAE/MFE and
   unrealised P&L — not to pick a winner by P&L alone (there's no SL/target yet to optimise
   against), but as the raw material the *next* stage calibrates against without re-running the
   backtest.
2. **Exit calibration second** (`exit_calib_p3.py`), reusing those per-trade 1-minute logs
   directly rather than reloading raw price data — same staged, one-variable-at-a-time
   methodology as `sweep_p2.py` (SL grid → target1 grid → target2 grid, each stage pinning the
   previous stage's Calmar-selected winner), but at 1-minute fill granularity (finer than Phase
   2's native 15-minute bars, since that's what the logs are) and explicitly run against
   **every** multiplier tested, not just the best-Calmar one — see the overfitting discussion
   below for why.

Positional design (no EOD square-off, no entry-time gate) carries through the exit-calibration
stage unchanged — calibrating SL/target on top of Phase 3's already-decided entry/holding
design, not reinstating Phase 2's session structure.

**Files:**
- `configs_p3.py` — signal parameters (`ST_PERIOD`, `ST_MULTIPLIER_GRID`), paths
- `backtest_p3.py` — raw signal-following state machine (trend_flip-only exit)
- `trade_paths_p3.py` — per-trade 1-minute MAE/MFE/unrealised-P&L logs (the actual calibration
  substrate for stage 2)
- `sweep_p3.py` — runs the raw backtest across the multiplier grid, saves
  `data_sweep/mult_<X.X>/{trade_summary.csv,trade_logs/}` and `data_sweep/sweep_p3_summary.csv`
- `exit_calib_p3.py` — staged SL/target1/target2 calibration against the saved logs; saves
  `data_sweep/exit_calib_p3_detail.csv` (every grid point tried) and
  `data_sweep/exit_calib_p3_winners.csv` (one row per multiplier)
- `bespoke_2lot_p3.py` — full per-trade detail (entry/exit price, reason, P&L per lot) for a
  specific *already-chosen* bespoke combo, schema-matched to `trade_summary_p2.csv` for direct
  comparison; saves `data_sweep/mult_<X.X>/bespoke_trade_summary.csv` — for manually inspecting
  individual trades, not for calibration itself

**Raw signal-quality sweep results** (`ST_PERIOD=10`, no SL/target/EOD, refreshed through the
latest candle):

| Multiplier | Trades | Win % | Total P&L | Max DD | Calmar |
|---|---|---|---|---|---|
| 2.0 | 375 | 41.6% | ₹145,600 | −₹19,740 | 7.38 |
| 2.5 | 285 | 41.8% | ₹122,670 | −₹16,600 | 7.39 |
| 3.0 | 229 | 39.3% | ₹66,950 | −₹19,680 | 3.40 |
| 3.5 | 194 | 38.1% | ₹43,650 | −₹20,190 | 2.16 |
| 4.0 | 158 | 38.6% | ₹29,550 | −₹18,850 | 1.57 |
| 4.5 | 128 | 39.8% | ₹55,010 | −₹32,500 | 1.69 |
| 5.0 | 114 | 39.5% | ₹36,090 | −₹28,910 | 1.25 |
| 5.5 | 97 | 40.2% | ₹29,540 | −₹32,340 | 0.91 |

Raw Calmar climbs steadily as the multiplier drops from 5.5 to 2.5 (0.91 → 7.39) — on its own,
that's the signature of an under-explored grid edge, not a found optimum (the lowest multiplier
tested looking best is exactly what you'd see if the real peak sits below the grid, or if the
tightest setting is just chasing noise). Extending one step further to 2.0 broke that pattern:
Calmar essentially flattened (7.38 vs 7.39) instead of continuing to climb — a reassuring
single data point, not proof, but it argues against 2.5 being purely a boundary artifact.

**Exit calibration winners, all multipliers** (SL/target1/target2 grids: 1.0–3.5% / 0.5–2.0% /
1.5–6.0%, Calmar-selected at each stage):

| Multiplier | SL% | T1% | T2% | Calmar | Total P&L | Max DD |
|---|---|---|---|---|---|---|
| 2.0 | 2.2 | 2.0 | 5.0 | 10.07 | ₹163,451 | −₹16,235 |
| 2.5 | 1.0 | 1.25 | 4.0 | 9.50 | ₹103,420 | −₹10,886 |
| 3.0 | 1.8 | 0.75 | 6.0 | 5.19 | ₹82,700 | −₹15,937 |
| 3.5 | 1.8 | 1.75 | 6.0 | 3.04 | ₹59,106 | −₹19,427 |
| 4.0 | 2.6 | 1.75 | 3.0 | 3.21 | ₹52,247 | −₹16,272 |
| 4.5 | 1.8 | 1.0 | 2.5 | 5.66 | ₹46,137 | −₹8,156 |
| 5.0 | 1.0 | 1.0 | 5.0 | 8.82 | ₹65,655 | −₹7,446 |
| 5.5 | 1.0 | 1.0 | 5.0 | 10.50 | ₹67,162 | −₹6,394 |

(Calmar/max-DD here use the per-trade, lot1+lot2-combined equity series that `exit_calib_p3.py`
itself computes; the two candidate write-ups below use a slightly more precise per-lot-*exit*
equity series instead — see the artifact note under Supporting analysis for why the two differ
by a small amount.)

**These winners don't agree with each other, and that matters.** SL ranges 1.0–2.6%, target1
0.75–2.0%, target2 2.5–6.0% across the grid — nothing close to Phase 2's experience of one
combo (1.8/1.0/2.3) cross-validating cleanly across two instruments. A robustness check (fixed
SL/T1/T2 combos applied *unchanged* across every multiplier, rather than each getting its own
bespoke tuning) found SL 1.8/T1 1.0/T2 3.0 as the most robust single choice — min-Calmar 1.80
across the grid vs. 0.20 for a combo built around 2.5's own bespoke values — but that check
predates both the 2026-09-01 data refresh and multiplier 2.0's existence, so treat it as
directional, not current; it hasn't been re-run since.

**Two calibrated candidates under active consideration** (2026-09-01, decision pending):

| Metric | Mult 2.0 (SL 2.2/T1 2.0/T2 5.0) | Mult 2.5 (SL 1.0/T1 1.25/T2 4.0) |
|---|---|---|
| Total trades | 375 | 285 |
| Win % | 44.27% | 48.07% |
| Total P&L | ₹163,451 | ₹103,420 |
| Avg win / avg loss | ₹3,173 / −₹1,738 | ₹2,268 / −₹1,401 |
| Max win / max loss | ₹9,376 / −₹6,320 | ₹7,835 / −₹2,160 |
| Max drawdown | −₹16,624 | −₹11,219 |
| Calmar | 9.83 | 9.22 |

Both re-run against the 2026-09-01 refreshed data. Both shown at 1 unit (2 lots) as traded —
no capital normalisation.

**Open caveats on both candidates, not yet resolved:**
1. **Mult 2.0's `TARGET1_PCT` landed on the edge of its own grid** (0.5%–2.0% tested), with
   Calmar still climbing at the top of that range (5.62 → 6.21 → 7.40 → 7.52 → **8.48** at
   0.5%/1.0%/1.25%/1.75%/2.0%) — the same edge-of-grid problem flagged for multiplier
   selection itself, one level down. The grid needs widening past 2.0% before 2.0's combo can
   be trusted as a genuine optimum rather than a cut-off.
2. **The two candidates are structurally different strategies, not the same mechanism at
   different scale.** Exit-reason mix (lot1 / lot2, of trades reaching each outcome):

   | | Mult 2.0 lot 1 | Mult 2.0 lot 2 | Mult 2.5 lot 1 | Mult 2.5 lot 2 |
   |---|---|---|---|---|
   | trend_flip | 205 (54.7%) | 293 (78.1%) | 40 (14.0%) | 106 (37.2%) |
   | target | 142 (37.9%) | 47 (12.5%) | 135 (47.4%) | 48 (16.8%) |
   | stop_loss | 28 (7.5%) | 35 (9.3%) | 110 (38.6%) | 131 (46.0%) |

   At 2.5, the tight 1.0% SL does most of the work (largest single exit-reason bucket for both
   lots). At 2.0, the wide 2.2% SL barely intervenes — most trades just ride to the raw
   trend_flip exit. **That trend_flip bucket is not benign for mult 2.0's lot 1**: 205 trades,
   only 15.1% win rate, −₹120,600 in aggregate — the single biggest loss center in the whole
   2.0 system, bigger than the SL bucket itself (−₹53,812). The SL is correctly sized to catch
   *extreme* individual losers (mean −₹1,922/trade vs. trend_flip's −₹588), but the real drag on
   2.0's lot 1 is a large population of trades that never reach either target and bleed out
   slowly — a signal-quality issue, not something a different SL fixes. Lot 2's trend_flip, by
   contrast, is genuinely closer to breakeven (−₹133 avg, 36.5% win rate) — the "let it play
   out" framing holds there, just not for lot 1.
3. **No CRUDEOIL cross-validation yet.** Phase 2 wasn't trusted until every major finding
   replicated on the full-size contract; Phase 3's multiplier and exit choices are CRUDEOILM-only
   so far.
4. **No transaction costs modeled** (same convention as v1/Phase 2) — mult 2.0 has the highest
   trade count of any candidate (375 vs. 2.5's 285), making it the most cost-exposed once
   slippage/brokerage are added.
5. **In-sample selection throughout** — both the multiplier grid and every exit-parameter grid
   were selected on the same window they're evaluated against; no train/test split or
   walk-forward check has been run.

### Supporting analysis

- **Multiplier sensitivity (MAE/MFE/P&L distributions, equity curve, drawdown)** — published
  artifact (private): `https://claude.ai/code/artifact/1ce085fa-bb85-4b92-b777-81cdde674268`.
- **Scale-out vs. raw, Phase 2 vs. Phase 3, and mult 2.0 vs. 2.5** (equity curves, drawdown
  curves, full per-trade comparison tables, all three as separate sections on one page) —
  published artifact (private): `https://claude.ai/code/artifact/624f0f27-8c12-4d5a-9e3a-9f050b34e087`.
  This is where the per-lot-exit-event equity/Calmar numbers quoted in the two-candidate table
  above come from — a finer-grained cash-flow series than `exit_calib_p3.py`'s own per-trade
  summary, so its max-DD figures read a little deeper (e.g. mult 2.5: −₹11,219 here vs. −₹10,886
  in `exit_calib_p3_winners.csv`) because it can see a dip that opens and closes entirely
  between one trade's lot 1 exit and its lot 2 exit. Not a contradiction, just more precision.

### Not yet done / open threads

- **The 2.0-vs-2.5 decision itself** — pending, blocked mainly on open caveat #1 above (2.0's
  T1 grid needs widening) and #3 (no CRUDEOIL cross-validation for either).
- Re-run the robustness check (fixed combo across the whole multiplier grid) against the
  2026-09-01 data and the now-8-point grid (2.0–5.5) — the version quoted above predates both.
- CRUDEOIL cross-validation, for whichever candidate is chosen.
- Transaction-cost modeling, given how trade-count-sensitive the candidates are to each other.
- Once a candidate is chosen: fold it into `configs_p3.py` as the default, and decide whether
  Phase 3 supersedes Phase 2 as the production target or runs alongside it.

## Running

```bash
python prometheus_backtest/run.py                    # v1 baseline
python prometheus_backtest/sweep.py                  # v1 calibration grids
python prometheus_backtest/phase2/run_p2.py          # Phase 2, current recommended config
python prometheus_backtest/phase2/sweep_p2.py        # Phase 2 calibration grids
python prometheus_backtest/phase3/sweep_p3.py        # Phase 3, raw signal-quality sweep (all multipliers)
python prometheus_backtest/phase3/exit_calib_p3.py   # Phase 3, exit calibration (all multipliers; reuses sweep_p3.py's logs)
python prometheus_backtest/phase3/bespoke_2lot_p3.py # Phase 3, full per-trade detail for the two candidate combos
```

Symbol switch: `SYMBOL` in `configs.py` / `configs_p2.py` / `configs_p3.py` — `'CRUDEOILM'`
(default, primary calibration target) or `'CRUDEOIL'` (cross-validation, full-size contract).
Phase 3 has not yet been run against CRUDEOIL (see Phase 3's open threads above).

All generated output (`data/`, `data_sweep/`, per-trade logs) is gitignored — every number in
this README was verified against a fresh run of the current code, not carried over from
memory of an earlier session.
