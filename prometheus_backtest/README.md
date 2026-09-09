# Prometheus — MCX Crude Oil Intraday Trend-Following

Intraday trend-following strategy for MCX crude oil futures (CRUDEOILM primary, CRUDEOIL
cross-validation), built on Supertrend flip signals. Named for the fire-bringer, fitting for
a crude oil / energy strategy, per the repo's Greek-mythology naming convention. Three design
phases live here — v1 (superseded), Phase 2 (session-bound 2-lot scale-out, superseded in
production by Phase 3's mult-2.0 candidate but kept as the reference baseline), and Phase 3
(positional 2-lot scale-out, own Supertrend multiplier calibrated — **decided 2026-09-04: mult
2.0**, see below) — all backtest-only in this folder; the production build is
[`prometheus_production/`](../prometheus_production/README.md), based on Phase 3, first live
DRY_RUN-tested 2026-09-04.

## Data

- `data_pipeline/data/mcx/{CRUDEOILM,CRUDEOIL}/<expiry>_futures.csv` — 1-minute OHLCV,
  one file per contract, stitched across expiry rolls by `load_futures_1min()`. No
  back-adjustment needed: the strategy is pure intraday, so no position ever spans a roll —
  each day's bars belong to whichever contract was genuinely front-month that day.
- Current coverage: 2026-01-30 to 2026-09-03 23:29 IST (153 trading days — refreshed 2026-09-04
  via the nightly Delos MCX cron + datasync). 2026-08-28's daytime bars (09:00–15:15) were
  backfilled by `data_downloader_mcx.py` on 2026-08-29, joining seamlessly with the evening
  session `mcx_live_downloader.py` had already captured live (15:16–23:29) — verified gapless
  (0 missing minutes, 0 duplicate timestamps) before rerunning.
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

**Current result** (226 trades, 2026-01-30–2026-09-03, refreshed 2026-09-04):
55.8% win rate, ₹42,778 total P&L, −₹14,943 max drawdown, Calmar 2.86 (same unitless
definition as v1 — not annualized). Max drawdown is unchanged from the prior ₹42,453/221-trade
snapshot — the 5 new trades didn't deepen the worst episode, just added modestly to total P&L
and win rate.

**On a ₹1,00,000 allocated-capital basis** (the user's own sizing call, accounting for ~₹25k
margin/lot × 2 lots plus a drawdown buffer): 42.78% return on capital over the backtest's
0.592-year window, **72.29% annualized** (simple/linear annualization, appropriate since the
strategy trades a fixed 2-lot size rather than compounding with account growth — not a
compounded CAGR), max drawdown −14.94% of capital, and a properly-annualized Calmar
(annualized return % ÷ max DD%) of **4.84**. Lowest account value reached: ₹92,540
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

## Phase 3 — positional 2-lot scale-out (decided, live in production)

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

**Data-quality fix (2026-09-01):** `load_futures_1min` now drops Saturday/Sunday bars entirely.
Exactly one such session exists across the whole dataset — MCX's 2026-02-01 Union Budget special
session (WTI itself wasn't trading) — but left in, it fed both the raw ST_15 signal computation
and SL/target fill checks with thin, WTI-disconnected price action that a live Prometheus process
could never have reacted to anyway: the production cron is Mon-Fri only (`15 9 * * 1-5`), so it
simply isn't running on a Sunday. Confirmed via a real incident this session — a short entered
Friday 2026-01-30 got stopped out one minute into that Sunday session, a fill no live deployment
could ever have produced — this is a permanent fix to the historical data, not a one-off patch;
every number below reflects it. (The live-production analogue — a known-bad session re-entering
the *daily* ST re-seed window rather than one-time historical data — is tracked as `ST_SEED_SKIP_DATES`
in `plans/prometheus-phase3-production.md` §10.)

**Raw signal-quality sweep results** (`ST_PERIOD=10`, no SL/target/EOD, refreshed 2026-09-06
through 2026-09-04 — `sweep_p3.py` re-run in full after that session's live trading added a real
flip, per CLAUDE.md's "one variable changed" convention there's no reason a data refresh alone
should touch the grid selectively):

| Multiplier | Trades | Win % | Total P&L | Max DD | Calmar |
|---|---|---|---|---|---|
| 2.0 | 381 | 42.0% | ₹150,150 | (see sweep_p3_summary.csv) | — |
| 2.5 | 289 | 42.2% | ₹128,280 | (see sweep_p3_summary.csv) | — |
| 3.0 | 235 | 39.6% | ₹69,240 | (see sweep_p3_summary.csv) | — |
| 3.5 | 200 | 38.0% | ₹41,550 | (see sweep_p3_summary.csv) | — |
| 4.0 | 162 | 38.3% | ₹34,360 | (see sweep_p3_summary.csv) | — |
| 4.5 | 131 | 39.7% | ₹59,320 | (see sweep_p3_summary.csv) | — |
| 5.0 | 117 | 39.3% | ₹38,240 | (see sweep_p3_summary.csv) | — |
| 5.5 | 99 | 40.4% | ₹35,080 | (see sweep_p3_summary.csv) | — |

(Trade counts above are closed trades only — each multiplier also has exactly 1 trade still open
at data end, the real bullish position entered live 2026-09-04 20:15 that the earlier bearish
raw trade flipped into.) Pattern held with one more day of data: trade counts, win rates, and
total P&L all moved by small, proportionate amounts (e.g. mult 2.0: 380→381 trades,
₹148,560→₹150,150), no reversal. `sweep_p3.py`'s own summary doesn't compute Calmar for the raw
(no-SL/target) series — the qualitative finding stands regardless: raw Calmar climbed steadily
from 5.5 down to 2.5, then flattened extending one step further to 2.0 rather than continuing to
climb — the signature that argues against 2.5 being purely an under-explored grid-edge artifact.

**Exit calibration winners, all multipliers** (SL/target1/target2 grids: 1.0–3.5% / 0.5–2.0% /
1.5–6.0%, Calmar-selected at each stage, refreshed 2026-09-04):

| Multiplier | SL% | T1% | T2% | Calmar | Total P&L | Max DD |
|---|---|---|---|---|---|---|
| 2.0 | 2.2 | 2.0 | 5.0 | 10.46 | ₹169,779 | −₹16,235 |
| 2.5 | 1.0 | 1.25 | 4.0 | 11.39 | ₹120,936 | −₹10,619 |
| 3.0 | 1.0 | 0.75 | 6.0 | 7.29 | ₹82,042 | −₹11,259 |
| 3.5 | 1.8 | 2.00 | 6.0 | 4.05 | ₹75,281 | −₹18,565 |
| 4.0 | 2.6 | 1.75 | 6.0 | 3.72 | ₹70,080 | −₹18,817 |
| 4.5 | 1.8 | 1.0 | 2.5 | 6.74 | ₹54,949 | −₹8,156 |
| 5.0 | 1.0 | 1.0 | 5.0 | 11.14 | ₹82,928 | −₹7,446 |
| 5.5 | 1.0 | 1.75 | 5.0 | 13.34 | ₹83,568 | −₹6,267 |

The winning SL/T1/T2 combo for both 2.0 and 2.5 is unchanged from the 2026-09-01 run despite
the fresh data — the bespoke candidates below remain the Calmar-optimal choice at this grid
resolution, not stale picks.

(Calmar/max-DD here use the per-trade, lot1+lot2-combined equity series that `exit_calib_p3.py`
itself computes; the two candidate write-up below uses a slightly more precise per-lot-*exit*
equity series instead — see the artifact note under Supporting analysis for why the two differ
by a small amount.)

**These winners don't agree with each other, and that matters.** SL ranges 1.0–2.6%, target1
0.75–2.0%, target2 2.5–6.0% across the grid — nothing close to Phase 2's experience of one
combo (1.8/1.0/2.3) cross-validating cleanly across two instruments. A robustness check (fixed
SL/T1/T2 combos applied *unchanged* across every multiplier, rather than each getting its own
bespoke tuning) found SL 1.8/T1 1.0/T2 3.0 as the most robust single choice — min-Calmar 1.80
across the grid vs. 0.20 for a combo built around 2.5's own bespoke values — but that check
predates the 2026-09-01 data refresh and multiplier 2.0's existence (and hasn't been re-run
against the 2026-09-04 refresh either), so treat it as directional, not current.

**Two calibrated candidates — DECIDED 2026-09-04: mult 2.0** (adopted live in
`prometheus_production/` the same day, after confirming mult 3.0's live ST matched the chart
first). **Mult 2.0's `TARGET1_PCT` updated 2.0 → 2.2 on 2026-09-09** (caveat #1 below) — this is
now Prometheus's final exit configuration; table refreshed the same day
(`two_candidate_stats_p3.py`, new script, `data_sweep/two_candidate_stats.csv`), mult 2.5 column
also refreshed against current data as a same-vintage sanity check (289 vs previous 288 trades,
one more raw flip since the last refresh — figures otherwise match closely, confirming the
methodology reproduces correctly):

| Metric | Mult 2.0 (SL 2.2/T1 2.2/T2 5.0) | Mult 2.5 (SL 1.0/T1 1.25/T2 4.0) |
|---|---|---|
| Total trades | 381 | 289 |
| Win % | 44.62% | 48.79% |
| Total P&L | ₹179,726 | ₹123,010 |
| Avg win / avg loss | ₹3,248 / −₹1,765 | ₹2,340 / −₹1,398 |
| Max win / max loss | ₹9,376 / −₹6,320 | ₹7,835 / −₹2,160 |
| Max drawdown | −₹15,267 | −₹11,219 |
| Calmar | 11.77 | 10.96 |

**CRUDEOIL cross-validation — done 2026-09-07, re-run 2026-09-09 at T1=2.2%** (`prometheus_backtest/phase3_crudeoil/`, identical pipeline to the CRUDEOILM run above, `SYMBOL` the only change, stored in a separate sibling folder rather than overwriting this one; new `two_candidate_stats_p3.py` script mirrored into this folder to compute the table below). Same per-lot-exit-event Calmar/drawdown methodology as the table above, computed directly from the refreshed `bespoke_trade_summary.csv` files (matching how the CRUDEOILM figures above were produced, not the coarser per-trade `exit_calib_p3_winners.csv` method). Mult 2.5's column reproduces the 2026-09-07 figures exactly (unchanged inputs), confirming the methodology is applied consistently:

| Metric | Mult 2.0, CRUDEOIL | Mult 2.5, CRUDEOIL |
|---|---|---|
| Total trades | 398 | 288 |
| Win % | 41.96% | 48.61% |
| Total P&L | ₹1,573,300 | ₹1,161,011 |
| Avg win / avg loss | ₹33,130 / −₹17,140 | ₹23,256 / −₹14,154 |
| Max win / max loss | ₹102,350 / −₹68,800 | ₹88,600 / −₹56,000 |
| Max drawdown | −₹219,408 | −₹152,737 |
| Calmar | **7.17** (vs. CRUDEOILM's 11.77) | **7.60** (vs. CRUDEOILM's 10.96) |

**The edge holds directionally on the full-size contract but not at matching risk-adjusted quality.** Both candidates stay clearly profitable — win rates land within ~2.7 points of the mini-contract figures, and mult 2.5 remains the higher-Calmar choice on CRUDEOIL too (consistent ranking). But P&L scales up only ~8.8x (mult 2.0) / ~9.4x (mult 2.5) while max drawdown scales up ~14.4x (mult 2.0) / ~13.6x (mult 2.5) relative to CRUDEOILM — noticeably more than the 10x lot-size ratio alone would predict — so CRUDEOIL's drawdowns run proportionally deeper against its own return than CRUDEOILM's do. This is not a like-for-like guarantee that the live strategy (calibrated and risk-managed specifically against CRUDEOILM's own tighter profile) would perform equivalently if traded on the full-size contract instead — it's the reason Prometheus trades CRUDEOILM, not a reason to doubt the calibration.

*(Historical, superseded by the 2026-09-09 refresh in the table above — kept for the refresh
trail.)* Mult 2.0 refreshed 2026-09-06 (through 2026-09-04) — `bespoke_2lot_p3.py` re-run against
the freshly-synced data covering Friday's live session, which included one more raw flip
(bearish trade 381 closing into the real bullish position entered 20:15). Mult 2.5's column was
still the 2026-09-04-vintage figure (through 2026-09-03) at that point, not re-run that pass.
Calmar moved 10.21→10.41 for mult 2.0 (at the then-current T1=2.0%); drawdown reproduced the
original figure exactly (−16,625 unchanged), confirming the per-lot-exit-event methodology was
applied consistently. Both columns are now on the same current vintage (see table above) and
mult 2.0 runs T1=2.2%, not 2.0% — this paragraph's own numbers no longer apply.

**Open caveats on both candidates, not yet resolved:**
1. **Mult 2.0's `TARGET1_PCT` grid-edge caveat — RESOLVED 2026-09-09**, see
   `exit_calib_p3_t1_widen.py` (new script, `data_sweep/exit_calib_p3_t1_widen_mult20.csv`).
   Widened the T1 grid from the original 0.5%–2.0% out to 4.0% (0.25% spacing throughout),
   381-trade current vintage. One methodology change was forced: the original grid pinned
   `target2` at `T2_STARTING_DEFAULT=2.3%` while searching T1, but `_simulate_trade`'s own
   `target2_dist > target1_dist` assertion makes 2.3% infeasible once T1 is tested past 2.3% —
   so the widened sweep pins `target2` at the actual production value (5.0%) instead, across the
   *full* 0.5%–4.0% range (not spliced at 2.0%), answering the more relevant question directly:
   given the real decided SL/T2, is T1=2.0% still the best T1? Under this pin, Calmar at T1=2.0%
   reads 10.66 (not the original grid's 8.81 — different T2 pin, not a contradiction).

   Result: Calmar climbs 0.5%→2.25% (7.03 → 6.93 → 7.65 → 9.03 → 8.78 → 9.36 → 10.66 → **12.31**),
   then drops sharply at 2.50% (9.62) and stays in the 7–9 range out to 4.0% (max 190,642 total
   P&L at 4.0%, but Calmar never returns above ~9.6). **This is not a cut-off — the curve
   genuinely peaks and reverses just past the old edge, confirming the edge-of-grid concern was
   legitimate but resolvable.** Re-running Stage 3 (T2 grid) at the new T1=2.25% winner confirms
   T2=5.0% is still the joint optimum (unchanged). Full joint-optimum comparison, same 381-trade
   set, same methodology:

   | Combo | Total P&L | Max DD | Calmar |
   |---|---|---|---|
   | Current production: SL 2.2 / T1 2.0 / T2 5.0 | ₹173,102 | −₹16,235 | 10.66 |
   | New candidate: SL 2.2 / **T1 2.25** / T2 5.0 | ₹176,884 | **−₹14,364** | **12.31** |

   T1=2.25% beats production on every metric — higher P&L, shallower drawdown, +15.5% Calmar.

   **Fine-grid follow-up, same day** (`exit_calib_p3_t1_fine.py`,
   `data_sweep/exit_calib_p3_t1_fine_mult20.csv`) — 0.05% steps from 2.00% to 2.50%, same
   SL=2.2%/T2=5.0% pins, to check whether the 12.31 peak was a single-grid-point noise artifact.
   **It is not.** Calmar sits in an 8-point *plateau* from 2.05% to 2.45% (11.80–12.77, one mild
   dip at 2.30%), and — tellingly — max drawdown is pinned at *exactly* −₹14,364 for 8 of those
   11 points (2.05, 2.10, 2.15, 2.20, 2.25, 2.35, 2.40, 2.45), meaning the same worst losing
   sequence governs the whole plateau and only total P&L wobbles mildly (₹173k–₹183k) within it
   — the opposite of what single-point grid noise would look like. Then a genuine **structural
   cliff** at 2.50% exactly: drawdown jumps to −₹18,587 and Calmar falls to 9.62, a discrete
   break, not a gradual taper — some specific trade's outcome flips right at that threshold.

   | T1 | Total P&L | Max DD | Calmar |
   |---|---|---|---|
   | 2.00% (production) | ₹173,102 | −₹16,235 | 10.66 |
   | 2.05%–2.45% (plateau, 8/11 pts share max DD) | ₹175,928–₹183,402 | −₹14,364 (mostly) | 11.80–12.77 |
   | 2.50% (cliff) | ₹178,854 | −₹18,587 | 9.62 |

   **Conclusion: the T1=2.0%→2.25%+ edge is real, not noise** — any value roughly 2.05%–2.45%
   outperformed the prior production value (2.0%) by a similar margin, which is much stronger
   evidence than one isolated best point would have been. The single best fine-grid point
   (T1=2.45%, Calmar 12.77) sits right next to the 2.50% cliff, so picking the exact grid maximum
   would mean choosing the riskiest point in the plateau if live conditions shift even slightly —
   **T1=2.2% was adopted instead** (2026-09-09, central-plateau pick, `prometheus_production/prometheus_configs.py`), trading a little backtested Calmar for distance from the cliff. This is
   now Prometheus's final exit configuration. CRUDEOIL cross-validation of T1=2.2% — done
   2026-09-09, see the CRUDEOIL table above and caveat #3; the edge held. Still open: the
   in-sample-everywhere limitation (caveat #5) — this whole exercise, cliff included, was fit and
   evaluated on the same 381-trade window it's judged against.
2. **The two candidates are structurally different strategies, not the same mechanism at
   different scale.** Exit-reason mix (lot1 / lot2, of trades reaching each outcome, refreshed
   2026-09-09 for mult 2.0's new T1=2.2%; mult 2.5 refreshed to the same 289-trade vintage):

   | | Mult 2.0 lot 1 | Mult 2.0 lot 2 | Mult 2.5 lot 1 | Mult 2.5 lot 2 |
   |---|---|---|---|---|
   | trend_flip | 217 (57.0%) | 300 (78.7%) | 42 (14.5%) | 110 (38.1%) |
   | target | 134 (35.2%) | 47 (12.3%) | 139 (48.1%) | 51 (17.6%) |
   | stop_loss | 30 (7.9%) | 34 (8.9%) | 108 (37.4%) | 128 (44.3%) |

   At 2.5, the tight 1.0% SL does most of the work (largest single exit-reason bucket for both
   lots). At 2.0, the wide 2.2% SL barely intervenes — most trades just ride to the raw
   trend_flip exit. **That trend_flip bucket is not benign for mult 2.0's lot 1**: 217 trades,
   only 18.4% win rate, −₹116,900 in aggregate — the single biggest loss center in the whole
   2.0 system, bigger than the SL bucket itself (−₹58,402). The SL is correctly sized to catch
   *extreme* individual losers (mean −₹1,947/trade vs. trend_flip's −₹539), but the real drag on
   2.0's lot 1 is a large population of trades that never reach either target and bleed out
   slowly — a signal-quality issue, not something a different SL or T1 fixes (T1=2.2% moved a
   handful of borderline trades from lot1-target into lot1-trend_flip relative to the T1=2.0%
   figures above — 209→217 — but the qualitative picture is unchanged). Lot 2's trend_flip, by
   contrast, is genuinely closer to breakeven (−₹119 avg, 37.0% win rate) — the "let it play
   out" framing holds there, just not for lot 1.
3. **CRUDEOIL cross-validation — done 2026-09-07, re-run 2026-09-09 at T1=2.2%** (see the table
   above): the edge replicates directionally on the full-size contract, but Calmar drops
   noticeably for both candidates (11.77→7.17 for mult 2.0, 10.96→7.60 for mult 2.5) — CRUDEOIL's
   drawdowns run proportionally deeper than CRUDEOILM's, not just larger by the 10x lot-size
   ratio. Doesn't change the mult-2.0 production decision (CRUDEOILM is the live-traded
   instrument), but means the live strategy's risk profile shouldn't be assumed to carry over
   unchanged if ever run on CRUDEOIL instead.
4. **No transaction costs modeled** (same convention as v1/Phase 2) — mult 2.0 has the highest
   trade count of any candidate (381 vs. 2.5's 289), making it the most cost-exposed once
   slippage/brokerage are added.
5. **In-sample selection throughout** — both the multiplier grid and every exit-parameter grid
   were selected on the same window they're evaluated against; no train/test split or
   walk-forward check has been run.

### Supporting analysis

- **Multiplier sensitivity (MAE/MFE/P&L distributions, equity curve, drawdown)** — published
  artifact (private): `https://claude.ai/code/artifact/1ce085fa-bb85-4b92-b777-81cdde674268`.
- **Scale-out vs. raw, Phase 2 vs. Phase 3, and mult 2.0 vs. 2.5** (equity curves, drawdown
  curves, full per-trade comparison tables, all three as separate sections on one page) —
  published artifact (private, **2026-09-01 data, not refreshed**):
  `https://claude.ai/code/artifact/624f0f27-8c12-4d5a-9e3a-9f050b34e087`. Originated the
  per-lot-exit-event equity/Calmar methodology — a finer-grained cash-flow series than
  `exit_calib_p3.py`'s own per-trade summary, treating each lot's own exit as its own
  chronological cash-flow event rather than bundling both lots' P&L at the trade's completion,
  so its max-DD figures read a little deeper (e.g. mult 2.5: −₹11,219 vs. −₹10,619 in the
  refreshed `exit_calib_p3_winners.csv`) because it can see a dip that opens and closes entirely
  between one trade's lot 1 exit and its lot 2 exit. Not a contradiction, just more precision.
  The 2026-09-04-refreshed per-lot-exit-event numbers in the two-candidate table above reproduce
  this artifact's methodology (verified: drawdown figures match exactly) but were computed
  directly from the refreshed `bespoke_trade_summary.csv` files, not from a re-published
  artifact.
- **Early MFE as a predictor of trade outcome (2026-09-08)** — prompted by watching a live trade
  stall at only ~2 points of MFE. Ad-hoc analysis (not a committed script) against all 381 mult-2.0
  bespoke trades: for each, measured running MFE at fixed early checkpoints (15/30/60/120/240 min
  since entry) using only trades still genuinely open at that checkpoint — no lookahead, a trade
  already closed before a given checkpoint is excluded from that checkpoint's cohort — then
  correlated against the trade's eventual `total_pnl_rs`.

  | Checkpoint | corr(MFE, P&L) | Low-MFE tercile win rate | High-MFE tercile win rate |
  |---|---:|---:|---:|
  | 15 min | +0.25 | 34% | 54% |
  | 30 min | +0.35 | 31% | 60% |
  | 60 min | +0.38 | 27% | 68% |
  | 120 min | +0.39 | 28% | 74% |
  | 240 min | +0.40 | 37% | 92% |

  Correlation strengthens the longer the trade survives; the bottom MFE tercile is net-negative in
  mean P&L at every checkpoint. The user's own trigger case checks out: trades with ≤2 points of
  MFE within the first 60 minutes (n=21) went on to a 33.3% win rate and −₹652 mean P&L, vs.
  46.1%/+₹576 for the rest. Stop-loss trades also have much lower final MFE (median 42 pts) than
  non-stop trades (median 112 pts). Moderate correlation, not a hard rule — even the worst bucket
  still has a quarter-to-a-third of eventual winners, so this isn't grounds to override SL/exit
  logic on its own. Purely descriptive/correlational so far — see the open-threads entry below for
  where this could go next.

### Not yet done / open threads

- **Early-MFE signal (above): worth watching, not yet turned into a rule.** Open questions before
  this becomes anything actionable: (1) does the same early-MFE/outcome relationship hold on
  CRUDEOIL's own 398 trades, or is it a CRUDEOILM-specific artifact of this particular price
  history? (2) if a live rule were built on it (e.g. an early tightened stop, or an alert rather
  than an auto-action, when MFE stays below some threshold past a fixed time), what's the
  false-positive cost — the ~30% of low-MFE trades that still win would be the ones a premature
  exit gives up? (3) is there a cleaner single early-checkpoint to standardize on (60min looks like
  a reasonable point where the signal is already fairly strong without waiting too long) rather
  than reporting all five? None of this has been tested as an actual rule change yet — currently
  just a live-monitoring signal to watch, per the user's request, not a backtested optimization.

- ~~**CRUDEOIL cross-validation** for mult 2.0 (the decided candidate)~~ — done 2026-09-07, see
  open caveat #3 above and the two-candidate table. Edge replicates, Calmar is meaningfully lower
  on the full-size contract.
- ~~**Mult 2.0's `TARGET1_PCT` grid edge** (open caveat #1)~~ — done 2026-09-09, see caveat #1
  above, including same-day fine-grid follow-up and CRUDEOIL re-validation. Confirmed a genuine
  ~2.05-2.45% plateau (not a single noisy point, not a cut-off), beating the prior T1=2.0% on
  P&L, drawdown, and Calmar across the whole plateau, bounded by a real structural cliff exactly
  at 2.50%. **T1=2.2% adopted as Prometheus's final exit configuration** (central-plateau pick,
  not the exact grid maximum) and cross-validated on CRUDEOIL the same day — edge held (Calmar
  7.17 vs. mult 2.5's 7.60, consistent ranking with CRUDEOILM). Still open: a train/test split
  given caveat #5's in-sample-everywhere limitation still applies here too — this whole exercise,
  cliff included, was fit and evaluated on the same 381-trade window it's judged against.
- Re-run the robustness check (fixed combo across the whole multiplier grid) against the
  2026-09-04 data — the version quoted above predates the 2026-09-01 refresh and multiplier
  2.0's existence, and hasn't been re-run since.
- Transaction-cost modeling, given how trade-count-sensitive the candidates are to each other.
- Once a candidate is chosen: fold it into `configs_p3.py` as the default, and decide whether
  Phase 3 supersedes Phase 2 as the production target or runs alongside it.

## Phase 4 — 1h/15m ST alignment entry filter (tested, SHELVED 2026-09-04)

Folder: `prometheus_backtest/phase4/`.

**Motivation.** `prometheus_production/` already has an entry-time regime-confirmation gate built
in (`_check_1h_alignment`, plan §17 preview) — a 1-hour-timeframe Supertrend that must agree with
the 15m entry signal's own direction before a fresh entry, a Rule 7 re-entry, an evening
rollover reopen, or a missed-rollover recovery reopen is allowed to proceed. It ships gated off
(`ENTRY_FILTER_1H_ALIGN_ENABLED=False`) pending this backtest. ST_15 held fixed at Phase 3's
decided mult-2.0 candidate (period 10, mult 2.0) throughout — only the 1h filter's own
`ST_1H_PERIOD`/`ST_1H_MULTIPLIER` varies, one variable changed per experiment.

**Method.** The filter can only *remove* an entry a 15m flip would otherwise take, never add
one — ST_15 flips are price-only and position-independent, so blocking one entry never moves any
other trade's own `signal_ts`/`exit_ts`. This makes it valid to evaluate by filtering Phase 3's
already-generated raw mult-2.0 trade list (`phase3/data_sweep/mult_2.0/`) rather than running a
second parallel backtest engine: build the 1h ST series (`filter_1h_p4.build_1h_st_series`,
reusing `data_loader.resample_ohlcv` unchanged — its per-day `origin=day.index[0]` anchor already
constructs genuine trailing partial 1h buckets, including on the 7/153 evening-only special
sessions that start at 17:00 rather than 09:00, confirmed against the user's own chart), check
each trade's alignment at its own decision time (`filter_1h_p4.check_alignment` — decision_ts =
signal_ts + 15min, the moment the 15m bar's window actually closes and the flip becomes known;
only the LAST FULLY-CLOSED 1h bar as of that instant may inform the gate, via
`pd.merge_asof(..., direction='backward')` — same no-lookahead rule as `_check_1h_alignment` in
production, and the same bug class as Phase 1's previously-fixed 15-min regime-filter lookahead,
just a 6x wider window), then re-apply the mult-2.0 bespoke exits (SL 2.2%/T1 2.0%/T2 5.0%) to
both the kept and blocked subsets via `bespoke_2lot_p3._simulate_trade_detailed`.

**Files:**
- `configs_p4.py` — ST_15 and exits fixed at Phase 3's decided values; `ST_1H_PERIOD_GRID`/
  `ST_1H_MULTIPLIER_GRID` for the 1h filter under test; `DECISION_OFFSET_MIN=15`
- `filter_1h_p4.py` — `build_1h_st_series`, `check_alignment` (no-lookahead alignment check)
- `run_p4.py` — loads Phase 3's raw trades/paths once, loops the full grid, applies bespoke
  exits to kept/blocked subsets, computes per-lot-exit-event Calmar (same methodology as Phase
  3's two-candidate table, for direct comparability) for both, and reports `n_1h_flips` per cell
  (a degenerate-overfitting tripwire — as the 1h filter's own period/multiplier shrink toward
  ST_15-like responsiveness, it stops being a regime filter and becomes a near-duplicate of the
  entry signal itself). Doubles as the sweep entry point (loops `configs_p4.py`'s grid); no
  separate `sweep_p4.py` was needed.

**Result: every cell underperforms the unfiltered baseline, and the failure mode is structural.**

Grid: period ∈ {3, 4, 5, 7, 10} × multiplier ∈ {1.0, 1.5, 2.0, 2.5, 3.0} (25 cells), against the
mult-2.0 baseline (380 trades, ₹169,779, Calmar 10.21 — see Phase 3's two-candidate table).

| | Best cell (period 3, mult 2.5) | Worst cell (period 10, mult 2.0) |
|---|---|---|
| Kept trades | 153 | 135 |
| Kept total P&L | ₹105,401 | ₹72,765 |
| Kept Calmar | 9.67 | 2.05 |
| Blocked Calmar | 2.64 | 5.15 |
| Δ vs. baseline Calmar | −0.54 | −8.16 |

No cell in the grid reached baseline's Calmar of 10.21. Two distinct failure modes, split cleanly
by multiplier:

1. **Below mult 2.5 (15 of 25 cells): the filter anti-selects.** Blocked-set Calmar exceeds
   kept-set Calmar at every one of these cells (e.g. the original preview setting, period 10/mult
   2.0: kept Calmar 2.05 vs. blocked Calmar 5.15) — the filter systematically keeps the *worse*
   trades and blocks the *better* ones. Confirmed not a directional bug (bullish/bearish pass
   rates are balanced, 38.1%/33.0% at the preview setting) and not concentration of the known-bad
   `trend_flip` population in one bucket (that population is ~55% of both kept and blocked, but
   the kept share of it has a *worse* win rate — 12.0% vs. 17.2% blocked). At this multiplier
   range the 1h ST flips 110–245 times over the window — too often to function as a slow regime
   confirmation, not often enough to track ST_15 usefully either.
2. **At mult ≥ 2.5 (10 of 25 cells): the sign corrects, but there's still no edge.** Fewer,
   more decisive 1h flips (62–76 over the window) do correctly separate better trades from
   worse (kept Calmar > blocked Calmar in all 10 cells) — but the best of these (9.67) still
   trails the unfiltered baseline (10.21) on both Calmar and raw P&L, while discarding ~60% of
   trades to get there. There's no compensating edge once the sign is right, only a smaller
   sample of a strategy that was already fine.

**Conclusion: shelved, not re-tuned.** This isn't a calibration gap — the full grid was already
run. The 1h/15m alignment premise doesn't survive contact with the data: it either damages trade
selection outright (mult < 2.5) or simply subtracts a large fraction of otherwise-good trades for
no benefit (mult ≥ 2.5). `ENTRY_FILTER_1H_ALIGN_ENABLED` stays `False` in
`prometheus_production/`; no live re-test is warranted on this evidence. The mechanism (and its
production call sites — fresh entry, Rule 7 re-entry, rollover reopen, missed-rollover recovery)
remains in the codebase, gated off, in case a future signal redesign wants a differently-shaped
regime filter — but the specific "1h Supertrend agreement" mechanism itself is the finding here,
not just this particular grid.

### Not yet done / open threads

- No CRUDEOIL cross-validation — moot while the mechanism is shelved on CRUDEOILM.
- A wider grid (finer multiplier steps between 2.0 and 3.0, where the sign flips) was not run —
  not pursued, since even the sign-correct region never beat baseline; diminishing-returns
  territory rather than a promising lead.

## Position sizing — volume/participation analysis for scaling on CRUDEOILM (2026-09-07)

**Context.** User's plan is to trade CRUDEOILM as the primary instrument (not CRUDEOIL — see the
cross-validation finding above; the mini's lower per-lot capital requirement and MCX's 1000-lot
freeze limit both leave plenty of headroom to scale). Before scaling `STATIC_UNITS` up, the
question was how much real market depth backs each additional lot, since Prometheus's backtest
P&L (like every phase above) is deliberately cost-free (`SLIPPAGE_ENABLED = False` throughout) and
says nothing about fill quality at size.

**Units check, done first.** `getCandleData`'s `volume` field is number of **contracts (lots)**
traded in the bar, not underlying barrels — confirmed by comparing CRUDEOILM (10 bbl/lot) and
CRUDEOIL (100 bbl/lot) at identical timestamps: both report comparable per-minute volume
magnitudes (e.g. 2026-09-04 23:2x: CRUDEOILM 25–566, CRUDEOIL 26–154). If `volume` were in
barrels, CRUDEOIL's 10x-larger lot would inflate its barrel-count roughly 10x for comparable
participation; instead the two contracts print the same order of magnitude, consistent with each
being its own independently-traded pool of lots. Every figure below is in lots, directly
comparable to an order size in lots. Tick size is 1.0 (all OHLC prints are whole numbers) →
1 tick = ₹10/lot on CRUDEOILM.

**Method.** Loaded the full CRUDEOILM 1-min series via `data_loader.load_futures_1min` (same
front-month de-duplication as every backtest phase). Whole-day volume averages flatter the
picture, so participation was measured on the 1-min bar starting at each 15-min mark (:00/:15/
:30/:45, from 09:15 onward matching `MIN_ENTRY_TIME`) — 8,594 such bars across 6.5 months
(2026-01-30 to 2026-09-04) — since that's the bar an ST_15-triggered order actually needs to fill
against. 8 rows (of 130,366) carry a negative `volume` value, a known data-pipeline artifact; none
fall on a 15-min boundary, so they don't affect this analysis and weren't otherwise investigated.

**Participation by order size** (share of that boundary-minute's own volume; percentile columns
read as "in the worst N% of boundary-minutes, participation is at least this"):

| Order size (lots) | Median | p25 | p10 | p5 | Boundary-minutes ≥20% participation |
|---:|---:|---:|---:|---:|---:|
| 1 | 0.65% | 0.32% | 0.17% | 0.12% | 133 / 8,594 |
| 10 | 6.54% | 3.16% | 1.68% | 1.18% | 1,612 |
| 20 | 13.07% | 6.33% | 3.36% | 2.37% | 3,077 |
| 50 | 32.68% | 15.82% | 8.40% | 5.92% | 5,770 |
| 100 | 65.36% | 31.65% | 16.81% | 11.83% | 7,443 |
| 200 | 130.72% | 63.29% | 33.61% | 23.67% | 8,294 |

Median boundary-minute volume is 153 lots; the 10th-percentile (a genuinely thin minute) is 29
lots. Liquidity is meaningfully time-of-day-dependent: 15:00–close boundary minutes run ~2x
09:15–15:00 ones (median 204 vs. 96 lots) — thin-liquidity risk concentrates in the morning
session.

**Slippage: framed in ticks, not a modeled ₹ figure.** No square-root-impact coefficient is
applied — it would need calibration this dataset can't provide, and multiplying real volume data
by an uncalibrated constant produces a number that looks derived without being one. The backtest's
existing fill convention (`_target_fill_price`/`_stop_fill_price`) already prices in adverse
gap-through at the bar open, so this is specifically about *additional* size-driven impact on top
of that. Grounded read: at single-digit-to-teens lots, comfortably inside the spread most of the
time. Past ~20-30% participation (roughly 30-50 lots per the table above), expect to reliably
cross the spread and likely walk 1-2 ticks beyond (₹10-20/lot) on the worse-liquidity minutes;
past ~100 lots, plan for multi-tick slippage and likely order-splitting well before MCX's 1,000-
lot freeze limit (`freeze_qty=10000` underlying units / `LOT_SIZE=10` on CRUDEOILM).

**Decision (2026-09-07):** user's current capital supports scaling to 50 lots; plan is to scale up
gradually, not in one step. 1-2 ticks of slippage on worst-liquidity minutes is acceptable at that
size. 50 lots sits at the boundary where participation regularly exceeds 20-30% on a meaningful
minority of boundary-minutes (5,770 / 8,594, i.e. ~67%, at ≥20%) — consistent with "acceptable,
not free" rather than "negligible," matching the decision.

Not itself a slippage model (no fill data exists yet to calibrate one against) — a starting point
for judging how much headroom exists before participation, and therefore expected slippage,
becomes uncomfortable. Worth re-cutting against real fill data once trading at meaningful size.

## Position sizing — CRUDEOIL (main contract) liquidity comparison (2026-09-08)

**Context.** The dynamic-sizing simulation above (both the no-slippage and slippage-adjusted runs)
sizes purely in CRUDEOILM lots and reaches units the 2026-09-07 analysis already flagged as past
comfortable participation. Question: would switching to CRUDEOIL (the main/full-size contract, 100
bbl/lot vs. CRUDEOILM's 10) buy meaningfully more headroom to scale, at least beyond some exposure
level? Same method as the 2026-09-07 analysis, extended to CRUDEOIL and cross-compared on a
barrel-equivalent basis — a raw lot count means 10x different things on the two contracts, so lots
alone aren't a fair comparison.

**Method.** Identical to the CRUDEOILM analysis: participation measured on the 1-min bar at each
15-min mark (:00/:15/:30/:45, 09:15 onward) across the full available history (2026-01-30 to
2026-09-07 — three days further than the original cut). CRUDEOILM: 8,651 boundary-minutes;
CRUDEOIL: 8,570 (a handful fewer — gaps in the raw feed, not investigated, immaterial at this
sample size). Zero negative-volume rows land on a boundary minute on either contract. Units
re-confirmed with a fresh timestamp sample (both print comparable per-minute *lot* counts at
matching moments, e.g. 2026-01-30 23:2x: CRUDEOILM 27–92, CRUDEOIL 11–63) — volume is lots on both,
consistent with the original check.

**CRUDEOIL's own participation table** (lots, same shape as the CRUDEOILM table above):

| Order size (lots) | Median | p25 | p10 | p5 | Boundary-minutes ≥20% participation |
|---:|---:|---:|---:|---:|---:|
| 1 | 2.08% | 0.93% | 0.48% | 0.33% | 487 / 8,570 |
| 10 | 20.83% | 9.35% | 4.78% | 3.26% | 4,412 |
| 20 | 41.67% | 18.69% | 9.57% | 6.52% | 6,265 |
| 50 | 104.17% | 46.73% | 23.92% | 16.31% | 7,934 |
| 100 | 208.33% | 93.46% | 47.85% | 32.62% | 8,401 |
| 200 | 416.67% | 186.92% | 95.69% | 65.24% | 8,528 |

Median boundary-minute volume is 48 lots (vs. CRUDEOILM's 153) — CRUDEOIL trades roughly a third as
many *lots*, unsurprising for a contract with 10x the lot size and (presumably) a smaller retail
base. Read alone, this table looks worse than CRUDEOILM's — it isn't, once lots are converted to
the exposure they actually represent.

**Barrels, not lots, are the fair comparison unit.** Re-expressed at matched underlying exposure
(a CRUDEOILM lot is 10 bbl, a CRUDEOIL lot is 100 bbl):

| Barrels | CRUDEOILM lots | CRUDEOILM median part. | CRUDEOIL lots | CRUDEOIL median part. |
|---:|---:|---:|---:|---:|
| 10 | 1 | 0.65% | 0.10 | 0.21% |
| 100 | 10 | 6.54% | 1.00 | 2.08% |
| 200 | 20 | 13.07% | 2.00 | 4.17% |
| 500 | 50 | 32.68% | 5.00 | 10.42% |
| 1,000 | 100 | 65.36% | 10.00 | 20.83% |
| 2,000 | 200 | 130.72% | 20.00 | 41.67% |

CRUDEOIL's participation runs at roughly **a third** of CRUDEOILM's for the same barrel exposure,
consistently — checked at the tails too, not just the median: at 1,000 bbl, CRUDEOILM's p10/p5 are
16.81%/11.90% against CRUDEOIL's 4.78%/3.26%, the same ~3x gap. This isn't lots being re-sliced —
CRUDEOIL's real underlying pool is deeper: median boundary-minute volume in barrel terms is 4,800
bbl (CRUDEOIL) vs. 1,530 bbl (CRUDEOILM), a 3.14x ratio, and that ratio is what drives every
barrel-equivalent comparison above.

**Where the "1-2 tick" crossover sits, in barrels.** The 2026-09-07 analysis put CRUDEOILM's
crossover into the "expect to reliably cross the spread, walk 1-2 ticks" 20-30%-participation band
at roughly 30-50 lots — 300-500 bbl. CRUDEOIL's median participation crosses that same band around
10 lots — 1,000 bbl — call it 2-3x the barrel exposure before hitting the same qualitative
slippage zone, consistent with the 3.14x pool-size ratio. Per-barrel, a tick of slippage costs the
same on either contract (both quote the same underlying commodity price, tick size 1.0 on both) —
what changes is how much barrel exposure a given participation band tolerates, not the cost of a
tick itself. No calibrated ₹ coefficient here either, for the same reason the 2026-09-07 analysis
declined one — this is a real-volume-grounded qualitative read, not a fitted model.

**Checked against where the simulation already is.** *(Peak-units figures refreshed 2026-09-09
after `TARGET1_PCT` changed 2.0→2.2 — participation percentages rescaled linearly from the
2026-09-08 base run rather than re-pulled from raw volume data, which is exact for this purpose:
participation_pct = order_size / that_minute's_volume × 100 is linear in order_size for any fixed
minute, so every quantile of the distribution — median, p10, p5 — scales by the same ratio as the
lot count itself. No approximation, just algebra.)* The dynamic-sizing runs above now reach peak
units of 260 (no-slippage) and 181 (slippage-adjusted, anchor coefficient) — 520 and 362 CRUDEOILM
lots respectively (2 lots/unit), i.e. 5,200 bbl and 3,620 bbl of exposure:

| Simulation | CRUDEOILM lots | CRUDEOILM median part. | CRUDEOIL-equivalent lots | CRUDEOIL median part. |
|---|---:|---:|---:|---:|
| No-slippage peak (260 units) | 520 | 340.0% | 52.0 | 108.3% |
| Slippage-adjusted peak (181 units) | 362 | 236.5% | 36.2 | 75.4% |

CRUDEOIL is a consistent ~3.1x better at both points — but neither point is actually *comfortable*
on either contract at this exposure; CRUDEOIL just pushes the same problem out roughly 3x in
barrel terms, it doesn't remove it. The genuinely comfortable (<20-30%) CRUDEOIL zone tops out
around 1,000 bbl (≈10 CRUDEOIL lots ≈ 100 CRUDEOILM-lot-equivalent) — both simulations' sizing has
already run well past that by the time units reach the 150-250 range.

**Freeze-limit parity.** MCX's `freeze_qty` is 10,000 underlying units for both contracts —
1,000-lot ceiling on CRUDEOILM, 100-lot ceiling on CRUDEOIL, identical in barrel terms (10,000 bbl
either way). Order-splitting risk kicks in at the same total exposure regardless of which contract
carries it.

**Reading this.** CRUDEOIL offers meaningfully more room to scale the *same* capital-equivalent
exposure than CRUDEOILM does — roughly 3x, consistently, across the whole size range and both
tails checked — but "more room" isn't "unlimited room": past ~1,000 bbl (~10 CRUDEOIL lots), the
same qualitative slippage concerns reappear, just later. Whether switching (or splitting exposure
across both contracts) is actually worth it also depends on the strategy's own edge holding up
equally well on CRUDEOIL — already cross-validated 2026-09-07, re-validated 2026-09-09 at T1=2.2%
above, with Calmar running lower there (7.17-7.60 vs. CRUDEOILM's 10.96-11.77) — and on
CRUDEOIL's own margin-per-lot, which hasn't been pulled from a live source here and isn't
assumed to scale linearly with lot size.

**Not done here, by design** (this pass was scoped to liquidity/slippage only): no equity curve,
drawdown, or trade-performance re-simulation using CRUDEOIL-based sizing — queued as the next step.

**Follow-up, done 2026-09-08**: the equity curve / drawdown / trade-performance re-simulation
queued above is now done — see the CRUDEOIL dynamic-sizing simulation below, in its own artifact.

## Dynamic-sizing equity simulation — CRUDEOIL (main contract) (2026-09-08)

Same question as the CRUDEOILM dynamic-sizing simulation earlier in this README, asked of the main
contract: what if Prometheus had gone live on 2026-01-30 with `DYNAMIC_SIZING=True` on CRUDEOIL
instead, using CRUDEOIL's own checked margin requirement — `MARGIN_PER_UNIT` = Rs 10,00,000 (10x
CRUDEOILM's, tracking the contract's 10x lot size) and starting capital Rs 55,00,000, both
user-supplied. Same live production combo (mult 2.0, SL 2.2%/T1 2.2%/T2 5.0% — T1 updated 2026-09-09, see Phase
3 caveat #1), run against CRUDEOIL's own 398-trade backtest
(`phase3_crudeoil/data_sweep/mult_2.0/bespoke_trade_summary.csv` — 17 more trades than
CRUDEOILM's 381, same signal/exit logic against a different price series). Same per-lot-exit-event
equity/drawdown methodology, and — from the start this time, not as a follow-up — both a
no-slippage run and a slippage-adjusted run using the identical participation model from the
CRUDEOIL liquidity comparison above (same A=0.3 anchor, carried over rather than re-fit, since a
tick costs the same Rs/barrel on either contract).

**No-slippage result, refreshed 2026-09-09 at T1=2.2%** (was Rs 55L → Rs 1.94Cr, Calmar 12.77,
peak 20 units at T1=2.0%): 398 trades, Rs 55L → Rs 2.08Cr (+278.3%), max drawdown −20.6%, Calmar
13.52. Units grow from 5 to a peak of 21 — a far more modest range than CRUDEOILM's 50→260,
because CRUDEOIL's 10x-larger per-unit margin means the same rupee P&L moves units far less. Even
so, an entry at peak size (21 units = 42 lots) runs ~87% median participation (linearly rescaled
from the liquidity comparison's 83%-at-40-lots figure above) — past the "1-2 tick" comfort zone,
though nowhere near CRUDEOILM's equivalent-scaling extreme.

**Slippage-adjusted result, refreshed 2026-09-09** (same feedback-loop mechanics as the CRUDEOILM
slippage run — units resized from post-slippage capital every trade; was Rs 1.59Cr/Calmar 8.97/peak
17 units at T1=2.0%): final capital Rs 1.64Cr (+198.9%), max drawdown −23.8%, Calmar 8.35, peak
units damped from 21 to 17. Coefficient sensitivity (0.5x/1x/2x anchor) holds the same ranking:
Rs 1.87Cr → Rs 1.64Cr → Rs 1.33Cr final capital.

[Chart + table (both runs, comparison charts, sensitivity table)](https://claude.ai/code/artifact/704b21e1-1343-489b-8793-7d19240279ef)
— structured identically to the CRUDEOILM artifact. **Note: this published artifact is from the
2026-09-08 T1=2.0% run and has not been rebuilt against the 2026-09-09 T1=2.2% figures above** —
the numbers in this section are current, the linked chart/table page is not. Scripts:
`phase3_crudeoil/dynamic_sizing_sim.py` and `phase3_crudeoil/dynamic_sizing_sim_slippage.py`, both
committed. Detailed CSVs (`dynamic_sizing_trades.csv`, `dynamic_sizing_equity_curve.csv`, and their
`_slippage` counterparts) in `phase3_crudeoil/data_sweep/mult_2.0/` (gitignored, run the scripts to
regenerate).

## Risk of Ruin at 50-unit sizing (2026-09-08)

**Why 40% drawdown is the ruin threshold, not an arbitrary number.** MCX's actual required margin
for CRUDEOILM is ₹50,000/unit. `MARGIN_PER_UNIT` in `prometheus_configs.py` is set to ₹1,00,000 —
double the raw requirement — by design: the user allocates capital per unit so that a 40% drawdown
plus a further 10% negative MTM swing (50 percentage points of adverse capital use, together) can
be absorbed without ever touching the raw margin itself (`raw_margin / (1 − 0.40 − 0.10) =
50,000 / 0.50 = 1,00,000`). So "ruin" at 40% drawdown isn't a round-number risk tolerance pulled
from convention — it's the exact point at which the strategy starts eating into the 10%-MTM-swing
reserve that sits between the drawdown allowance and an actual margin call. A drawdown beyond 40%
that doesn't recover quickly is the scenario the sizing was explicitly built to avoid.

**Method.** Monte Carlo bootstrap over the 381 real backtested trades from Phase 3's live
production combo (mult 2.0, `phase3/data_sweep/mult_2.0/bespoke_trade_summary.csv`), resampled
with replacement (each trade's `total_pnl_rs` treated as one atomic outcome — lot1+lot2 combined,
appropriate for synthesizing new orderings rather than reconstructing the original timeline, where
the finer per-lot-exit-event method matters instead). Scaled to 50-unit sizing (linear ×50 on
each trade's 1-unit P&L). Capital base: ₹50,00,000 (50 units × `MARGIN_PER_UNIT`'s ₹1,00,000 —
the fully-buffered allocation per unit, not the raw ₹50,000 margin). 20,000 simulated paths, each
~1,289 trades (2 years, at the backtest's own observed pace of ~644 trades/year over its 216-day
span). Ruin defined as: max drawdown > 40% at any point, **and** equity has not recovered back to
its pre-drawdown peak by the end of the 2-year horizon.

**Refreshed 2026-09-09** against the new T1=2.2% `bespoke_trade_summary.csv` (original script was
ad-hoc/ephemeral, not checked in — reconstructed faithfully from this section's own method
description above, fixed seed for reproducibility). Directionally unchanged, modestly better:

**Result: P(ruin) = 0.00%** — 0 of 20,000 simulated paths met the full definition.
- P(max drawdown > 40% at any point): 2.43% (486/20,000 paths).
- Of those 486, every single one recovered within the 2-year horizon — 29.4% within 1 month, 91.2%
  within 3 months, 99.4% within 6 months, 100% within a year. None qualified as a "long recovery."
- P(equity ever negative — literal wipeout): 0.00%.
- Max drawdown distribution: p50 17.2%, p90 29.2%, p95 34.2%, p99 46.6% — essentially unchanged
  from the T1=2.0% run; even the 99th-percentile bad-luck path is only borderline past the 40%
  mark, not blown through it.
- Median terminal equity after 2 years: ₹3.54 crore (up from ₹3.43 crore at T1=2.0%), from a
  ₹50,00,000 base — reflects the combo's improved historical edge (win rate 44.6% unchanged, avg
  win ₹3,248 vs. avg loss ₹1,765 per unit at 1-unit sizing, both up slightly from T1=2.0%'s
  ₹3,174/₹1,737), which is exactly why ruin is this rare in the simulation.

**Why this "0%" shouldn't be read as a guarantee.**
- Bootstrap resampling treats the 381-trade sample as a fixed, stationary distribution — it cannot
  model the edge decaying or the strategy meeting a genuinely different regime than the last 6.5
  months produced.
- No slippage or execution cost is modeled here, consistent with every backtest phase
  (`SLIPPAGE_ENABLED=False` throughout) — but the volume/participation analysis above already
  found 50 lots sees ~33% median participation at the moments Prometheus fills, so real P&L at
  this size runs somewhat worse than a clean ×50 linear scale-up assumes.
- Resampling destroys whatever serial correlation the real historical sequence has (trending
  periods clustering, say) — trades are treated as independent draws, which is unlikely to be
  exactly true of the real market.
- The capital-base assumption (bare `MARGIN_PER_UNIT` × units, no further buffer beyond what's
  already built into that constant) is the single biggest lever on this number.

Not committed to any script in the repo (ad-hoc analysis, run once per this session's convention)
— re-run if the underlying trade sample changes materially (a re-calibration, a longer backtest
window, or once real fill/slippage data exists to replace the linear-scaling assumption).

## Side project: WTI 5-minute approximation (2026-09-08)

`prometheus_backtest/side_wti_5m/` — tests the already-decided Phase 3 combo(s) unchanged against
14.6 years of WTI crude 5-min data (Kaggle), as a loose cross-market sanity check. **Not a
validation like `phase3_crudeoil/`** — different exchange, different data source/quality, kept
deliberately out of the numbered phases and out of the production go-live decision chain.
**Result: the edge does not clearly transfer** (Calmar 0.05 / −0.29 vs. CRUDEOILM's own 10.21/10.78
at the time this side project ran — mult 2.0's T1 has since changed to 2.2%, not re-tested here)
— full methodology, the real data-quality finding behind it (weekend synthetic-fill
contamination), and caveats in `side_wti_5m/README.md`.

## Running

```bash
python prometheus_backtest/run.py                    # v1 baseline
python prometheus_backtest/sweep.py                  # v1 calibration grids
python prometheus_backtest/phase2/run_p2.py          # Phase 2, current recommended config
python prometheus_backtest/phase2/sweep_p2.py        # Phase 2 calibration grids
python prometheus_backtest/phase3/sweep_p3.py        # Phase 3, raw signal-quality sweep (all multipliers)
python prometheus_backtest/phase3/exit_calib_p3.py   # Phase 3, exit calibration (all multipliers; reuses sweep_p3.py's logs)
python prometheus_backtest/phase3/bespoke_2lot_p3.py # Phase 3, full per-trade detail for the two candidate combos
python prometheus_backtest/phase3_crudeoil/sweep_p3.py        # Phase 3 CRUDEOIL cross-validation, raw signal sweep
python prometheus_backtest/phase3_crudeoil/bespoke_2lot_p3.py # Phase 3 CRUDEOIL cross-validation, the two candidate combos
python prometheus_backtest/phase4/run_p4.py          # Phase 4, 1h alignment filter grid (shelved finding — SL/exits still fixed at mult-2.0's combo)
```

Symbol switch: `SYMBOL` in `configs.py` / `configs_p2.py` / `configs_p3.py` — `'CRUDEOILM'`
(default, primary calibration target) or `'CRUDEOIL'` (cross-validation, full-size contract).
Phase 3's CRUDEOIL cross-validation (2026-09-07) lives in its own sibling folder,
`prometheus_backtest/phase3_crudeoil/` — an exact copy of `phase3/`'s pipeline with only
`configs_p3.py`'s `SYMBOL` changed, rather than overwriting `phase3/`'s own CRUDEOILM output by
flipping the constant in place. See Phase 3's own section above for results.

All generated output (`data/`, `data_sweep/`, per-trade logs) is gitignored — every number in
this README was verified against a fresh run of the current code, not carried over from
memory of an earlier session.
