# Prometheus — MCX Crude Oil Intraday Trend-Following

Intraday trend-following strategy for MCX crude oil futures (CRUDEOILM primary, CRUDEOIL cross-validation), built on Supertrend flip signals. Named for the fire-bringer, fitting for a crude oil / energy strategy, per the repo's Greek-mythology naming convention. Three design phases live here — v1 (superseded), Phase 2 (session-bound 2-lot scale-out, superseded in production by Phase 3's mult-2.0 candidate but kept as the reference baseline), and Phase 3 (positional 2-lot scale-out, own Supertrend multiplier calibrated — **decided 2026-09-04: mult 2.0**, see below) — all backtest-only in this folder; the production build is [`prometheus_production/`](../prometheus_production/README.md), based on Phase 3, first live DRY_RUN-tested 2026-09-04.

## Data

- `data_pipeline/data/mcx/{CRUDEOILM,CRUDEOIL}/<expiry>_futures.csv` — 1-minute OHLCV, one file per contract, stitched across expiry rolls by `load_futures_1min()`. No back-adjustment needed: the strategy is pure intraday, so no position ever spans a roll — each day's bars belong to whichever contract was genuinely front-month that day.
- Current coverage: 2026-01-30 to 2026-09-11 23:29 IST (via the nightly Delos MCX cron + datasync — data_pipeline itself updates continuously; it's the backtest results below that need an explicit refresh to catch up, see "Routine backtest refresh" immediately below). 2026-08-28's daytime bars (09:00–15:15) were backfilled by `data_downloader_mcx.py` on 2026-08-29, joining seamlessly with the evening session `mcx_live_downloader.py` had already captured live (15:16–23:29) — verified gapless (0 missing minutes, 0 duplicate timestamps) before rerunning.
- Lot sizes and tick size looked up live from `data_pipeline/data/mcx_instrument_master.csv`, never hardcoded: CRUDEOILM = 10 barrels/lot, CRUDEOIL = 100 barrels/lot. The instrument master's `tick_size=100` field is in Angel One's paise-scaled convention — actual tick is ₹1.00 = 1 price point, matching the whole-number prices already in the data.

### Routine backtest refresh

`data_pipeline`'s own data updates continuously (nightly cron), but the backtest results below — `trade_summary.csv`, `trade_logs/`, `bespoke_trade_summary.csv`, the dynamic-sizing sims, risk of ruin, and every doc/artifact that cites their numbers — don't update themselves; each of those is a separate script that has to be re-run. This gap went unnoticed once already (2026-09-09: the backtest results only reached 2026-09-03 while `data_pipeline` had already reached 2026-09-08, because `sweep_p3.py` simply hadn't been re-run since 2026-09-06).

To refresh everything for both CRUDEOILM and CRUDEOIL in one pass:

```bash
python prometheus_backtest/refresh_pipeline.py
```

This re-runs the full deterministic chain (raw signal sweep → bespoke exit overlay → per-trade stats → dynamic sizing ×2 → risk of ruin) against whatever data currently exists. It's a full recompute, not a literal incremental append — but because Supertrend is a purely causal, trailing indicator, extending the tail of the price history never changes a decision made earlier in it, so every previously-computed trade reproduces identically (same `trade_id`, same entry/exit) and new trades simply appear at the end. The effect is append-like even though the mechanism recomputes everything; see the script's own docstring for the full reasoning. Updating the docs and publishing artifacts afterward still needs human/Claude judgment (numbers have to land in the right prose context) — the `/prometheus-refresh` skill (`.claude/skills/prometheus-refresh/`) walks through that whole remaining checklist, including the specific spots that have been missed before (this file's own several separate copies of the same headline numbers, and the root `README.md`'s three independent copies of the CRUDEOILM/CRUDEOIL dynamic-sizing writeup).

**A real trailing-trade-drop bug found and fixed, 2026-09-11.** `exit_calib_p3.py`'s `_load_multiplier_data` (reused directly by `bespoke_2lot_p3.py`) dropped any trade whose *raw signal* hadn't yet reversed by data end — correct for `trade_paths_p3.py`'s own MFE/MAE purpose (nothing to walk to for an undefined exit), wrong here: the bespoke 2-lot simulator can close a trade via SL/target independent of any trend-flip ever firing. This silently dropped whichever trade was "last in the dataset" from every bespoke/calibration output, every refresh — even when its SL/target outcome was already fully determinable from available price data. Caught when a direct check of the raw 1-min data showed CRUDEOILM's then-trailing trade had already hit both target1 and target2 hours before this refresh's own data cutoff, yet was absent from `bespoke_trade_summary.csv`. Fixed at the source: `trade_paths_p3.py` now walks a still-open trade to the end of available data instead of skipping it; `exit_calib_p3.py`/`bespoke_2lot_p3.py` no longer fabricate a `trend_flip` exit when neither SL/target nor a real flip has resolved a lot yet — that trade is correctly excluded until a future refresh resolves it (see CRUDEOIL's own mult 2.0 column in the two-candidates table below, still genuinely unresolved after the fix). Every table in this file that cites CRUDEOILM mult 2.0/2.5 or CRUDEOIL mult 2.5 figures was recomputed against the corrected pipeline for this same 2026-09-11 refresh — CRUDEOIL mult 2.0 was unaffected (its own trailing trade stays genuinely open either way).

## Phase 1 (v1) — dual-timeframe, hold-till-flip

Folder: `prometheus_backtest/` (root files — `configs.py`, `data_loader.py`, `backtest.py`, `analysis.py`, `run.py`, `sweep.py`, `trade_paths.py`).

**Architecture**: deliberately mirrors `iris_production/iris.py`'s live watching→in_trade→watching state machine rather than a vectorized precompute-then-scan approach (explicit design direction). 5-min entry-timeframe Supertrend flip, gated by 15-min regime-timeframe alignment — the same dual-timeframe structure Iris uses live. Single position, held until an opposing flip or the session close forces a square-off. No stop-loss or profit target in the baseline — cost-free, signal-only, to validate the raw edge before layering in exit calibration.

**Two correctness bugs found and fixed via review before any result was trusted:**
- **Look-ahead in the 15-min regime filter**: originally checked `regime_series.index <= ts`, which reads a left-labelled 15-min bar as available the instant its label-timestamp is reached rather than waiting for it to actually close — letting entry decisions peek up to ~10 minutes into a still-forming regime bar. Fixed to require the regime bar's own close (`regime_ts + REGIME_TF_MIN`) to be ≤ the decision timestamp, mirroring `iris.py`'s real `_update_15m_regime` behaviour.
- **Spurious first-bar Supertrend flip**: the very first bar where Supertrend leaves its NaN warm-up period always reads as a flip (comparing against a preceding `NA` value) — explicitly zeroed out, not a genuine regime change.

**Baseline results** (current data, 2026-01-30–2026-08-27): 188 trades, 38.3% win rate, ₹11,420 total P&L, −₹16,820 max drawdown, Calmar 0.68 (unitless: total P&L ÷ |max DD|, not annualized).

**Calibration sweep** (`sweep.py`): tested SL and profit-target in both points and % terms, plus a flat-vs-pivot target2 control. A stop-loss meaningfully improved results — non-monotone in absolute points (a real sweet spot near 60–75 points, an unexplained bad valley at 90–130 points), noticeably smoother and broader in % terms. This sweep is also what surfaced the limitation that shaped Phase 2's whole redesign: CRUDEOILM's entry price ranged ₹5,617–10,683 over the backtest window (CV 16.6%), so any fixed-points threshold is roughly a 2× swing in proportional size depending on where price happens to sit. That observation, plus an explicit user request for a scale-out design, is why development moved to Phase 2 rather than continuing to tune v1's single-position architecture.

v1 is retained as-is for reference and comparison — **not actively developed further**. Phase 2 is the live calibration target.

## Phase 2 — two-lot scale-out (active)

Folder: `prometheus_backtest/phase2/`.

**Design** (user-specified 2026-08-27): single-timeframe ST_15 signal (no regime gate — this is a genuine architectural departure from v1, not just a different outcome of the same structure). Entry with 2 independently-managed 1-lot legs. Lot 1 books at a fixed distance (`target1`); lot 2 books at a second, farther target — originally the nearest daily pivot/resistance/support level beyond `target1`, later found to be beaten by a flat % distance (see calibration journey below). SL is a single shared stop protecting whichever lot(s) remain open, checked ahead of trend-flip on any same-bar tie. Trend-flip remains the fallback SL and doubles as the next entry signal in the opposite direction — same flip, same bar, resolved together at the next bar's open. Entries are gated by `MIN_ENTRY_TIME` (checked at the **fill** bar, not the signal bar — see the bug note below) and `MAX_ENTRY_BEFORE_CLOSE_MIN` (measured relative to each trading day's own actual last bar, not a fixed clock time — MCX's nominal 23:30 close actually lands on 23:15/23:30/23:45 depending on the day, confirmed from the data). EOD square-off uses the same relative-to-actual-close design, `EOD_SQUAREOFF_BEFORE_CLOSE_MIN` minutes before whatever that day's last bar turns out to be.

**Files:**
- `configs_p2.py` — sole parameter source
- `data_loader_p2.py` — reuses v1's `load_futures_1min`/`resample_ohlcv`/`compute_st`, adds `compute_daily_pivots`
- `backtest_p2.py` — the two-lot state machine
- `trade_paths_p2.py` — per-trade 1-min logs with lot1/lot2/total running P&L (mark-to-market until each lot's own exit, frozen at the realized value from that point on) plus running MAE/MFE
- `analysis_p2.py` — consolidated stats, lot1/lot2 hit-rate reporting
- `run_p2.py` — entry point
- `sweep_p2.py` — calibration grids (points and %; each grid is explicitly pinned to its intended conditions so it can't silently drift if `configs_p2.py`'s defaults change later)

**Calibrated/recommended config** (current defaults in `configs_p2.py`, cross-validated on both CRUDEOILM and CRUDEOIL): `THRESHOLD_MODE='pct'`, `SL_PCT=1.8`, `TARGET1_PCT=1.0`, `TARGET2_MODE='flat_pct'`, `TARGET2_FLAT_PCT=2.3`.

**Current result** (226 trades, 2026-01-30–2026-09-03, refreshed 2026-09-04): 55.8% win rate, ₹42,778 total P&L, −₹14,943 max drawdown, Calmar 2.86 (same unitless definition as v1 — not annualized). Max drawdown is unchanged from the prior ₹42,453/221-trade snapshot — the 5 new trades didn't deepen the worst episode, just added modestly to total P&L and win rate.

**On a ₹1,00,000 allocated-capital basis** (the user's own sizing call, accounting for ~₹25k margin/lot × 2 lots plus a drawdown buffer): 42.78% return on capital over the backtest's 0.592-year window, **72.29% annualized** (simple/linear annualization, appropriate since the strategy trades a fixed 2-lot size rather than compounding with account growth — not a compounded CAGR), max drawdown −14.94% of capital, and a properly-annualized Calmar (annualized return % ÷ max DD%) of **4.84**. Lowest account value reached: ₹92,540 (−7.46% from start), 2026-02-17 — unchanged across every refresh so far.

### Calibration journey — why the config landed where it did

1. **Two correctness bugs fixed before trusting any calibration.** `MIN_ENTRY_TIME` was originally checked against the *signal* bar's own timestamp, rejecting a flip on a day's first 09:00 bar even though its fill at 09:15 is legitimately at/after 09:15 — fixed to check the fill bar instead (191 → 217 trades, ₹40,639 → ₹45,961 on the recommended config). The profit-target fill-price helper (`_target_fill_price`) is correctly *asymmetric* from the stop-loss fill-price helper (`_stop_fill_price`) — a stop's gap-through is worse for the trade, a target's is better — caught in review before the stop-loss sweep ran, not discovered afterward from bad numbers.
2. **SL/target1/target2 swept in both points and %.** Points-based SL showed a real but jagged, non-monotone response. Switching to %-based thresholds produced a materially smoother, broader-plateau response — evidence the % framing fits the data better, not just a relabeling of the same optimum found in points.
3. **Target2 mechanism reconsidered.** A flat %-distance target2 (no pivot lookup at all) beat the original pivot-based target2 once thresholds were expressed proportionally — cross-validated on both CRUDEOILM and CRUDEOIL, a genuine reversal from points-mode testing (where pivots had beaten a flat-points control). `TARGET2_MODE='flat_pct'` is the current default; `'pivot'` remains fully implemented and selectable if this is worth revisiting later.
4. **`TARGET1_PCT=1.75%` showed the single best backtested P&L but was rejected as unreliable.** Checked its improvement over 1.0% by price tercile and found ~70–75% of the gain concentrated in the series' highest-price third, replicated independently on both CRUDEOILM and CRUDEOIL — a real regime-dependency, not noise. `TARGET1_PCT=1.0` was kept as the steadier default.
5. **Every major finding cross-validated on CRUDEOIL** (the full-size contract, lot size 100 vs. CRUDEOILM's 10) before being trusted — matches the original design requirement that a successful strategy must translate from the mini to the full-size contract, not just work on the instrument it happened to be tuned on.

### Supporting analysis

- **MAE/MFE distribution** (points and % of entry price) — published artifact (private, requires the account owner's access): `https://claude.ai/code/artifact/a91bd410-81e9-4590-9445-6853448c55f0`. MAE separates winners from losers more cleanly than MFE does (corr −0.746/−0.780 vs. +0.618/+0.634 in points/%), and a fixed 100-point target would have been 0.94%–1.78% of entry depending on price level — the concrete evidence behind the pct-mode redesign.
- **Drawdown analysis** — published artifact (private): `https://claude.ai/code/artifact/ed3ff713-f354-410c-be19-af817410983e`. 15 distinct drawdown episodes; two of them (April, 33 days; June–July, 56 days) account for most of the drawdown-days and align with the two weak months in the month-by-month P&L breakdown. Sitting at a new equity peak with no open drawdown as of the last trade in the series.
- **Single-lot side-project comparison** (`research/prometheus_p2_single_lot/`): trading only lot 1's mechanics (1% target, 1.8% SL, identical entry/EOD/trend-flip rules) — 217 trades, 62.2% win rate, ₹18,664 total P&L, −₹7,657 max drawdown, Calmar 2.44. Cross-validated byte-for-byte against the two-lot backtest's own lot1 column before being trusted. Confirms lot 2 isn't just doubling size — it specifically captures bigger trending moves a single 1% exit structurally can't reach (two-lot total P&L is ~2.46× single-lot's, not ~2×). Decision: keep 2 lots.

### Not yet done / open threads

- CRUDEOIL production deployment — backtest cross-validation only so far; CRUDEOILM is the day-1 production target.
- Production build itself — see [`plans/prometheus-phase2-production.md`](../plans/prometheus-phase2-production.md) for the full architecture (Supertrend seeding, order execution and fill tracking, resilient candle polling, state file / crash recovery, Slack reporting). Not yet implemented.
- `TARGET1_PCT` joint-combined tests beyond the 1.0%/1.75% grid already run.

## Phase 3 — positional 2-lot scale-out (decided, live in production)

Folder: `prometheus_backtest/phase3/`.

**Motivation** (user, 2026-09-01): `ST_PERIOD=10`/`ST_MULTIPLIER=3.0` was never actually calibrated for Prometheus — `configs_p2.py`'s own docstring says "same day-1 starting values as Iris/Prometheus v1, not yet calibrated for this design specifically", and `sweep_p2.py` computes the Supertrend series once, before its sweep loop, so every Phase 2 calibration pass held the entry signal itself fixed and never questioned it. Crude's cleaner trending character (vs. Nifty/Sensex, which 10,3 actually *was* tuned for, via Iris) is a real, testable reason to suspect a different multiplier suits it better.

**Design, deliberately decoupled in two stages:**
1. **Raw signal-quality sweep first** (`backtest_p3.py`, `sweep_p3.py`) — no SL, no profit target, no EOD square-off. The only exit is the opposite Supertrend flip; a position can hold overnight, across multiple days, even across a contract roll. 1 lot, no scale-out. Every trade gets a minute-by-minute log (`trade_paths_p3.py`) tracking running MAE/MFE and unrealised P&L — not to pick a winner by P&L alone (there's no SL/target yet to optimise against), but as the raw material the *next* stage calibrates against without re-running the backtest.
2. **Exit calibration second** (`exit_calib_p3.py`), reusing those per-trade 1-minute logs directly rather than reloading raw price data — same staged, one-variable-at-a-time methodology as `sweep_p2.py` (SL grid → target1 grid → target2 grid, each stage pinning the previous stage's Calmar-selected winner), but at 1-minute fill granularity (finer than Phase 2's native 15-minute bars, since that's what the logs are) and explicitly run against **every** multiplier tested, not just the best-Calmar one — see the overfitting discussion below for why.

Positional design (no EOD square-off, no entry-time gate) carries through the exit-calibration stage unchanged — calibrating SL/target on top of Phase 3's already-decided entry/holding design, not reinstating Phase 2's session structure.

**Files:**
- `configs_p3.py` — signal parameters (`ST_PERIOD`, `ST_MULTIPLIER_GRID`), paths
- `backtest_p3.py` — raw signal-following state machine (trend_flip-only exit)
- `trade_paths_p3.py` — per-trade 1-minute MAE/MFE/unrealised-P&L logs (the actual calibration substrate for stage 2)
- `sweep_p3.py` — runs the raw backtest across the multiplier grid, saves `data_sweep/mult_<X.X>/{trade_summary.csv,trade_logs/}` and `data_sweep/sweep_p3_summary.csv`
- `exit_calib_p3.py` — staged SL/target1/target2 calibration against the saved logs; saves `data_sweep/exit_calib_p3_detail.csv` (every grid point tried) and `data_sweep/exit_calib_p3_winners.csv` (one row per multiplier)
- `bespoke_2lot_p3.py` — full per-trade detail (entry/exit price, reason, P&L per lot) for a specific *already-chosen* bespoke combo, schema-matched to `trade_summary_p2.csv` for direct comparison; saves `data_sweep/mult_<X.X>/bespoke_trade_summary.csv` — for manually inspecting individual trades, not for calibration itself

**Data-quality fix (2026-09-01):** `load_futures_1min` now drops Saturday/Sunday bars entirely. Exactly one such session exists across the whole dataset — MCX's 2026-02-01 Union Budget special session (WTI itself wasn't trading) — but left in, it fed both the raw ST_15 signal computation and SL/target fill checks with thin, WTI-disconnected price action that a live Prometheus process could never have reacted to anyway: the production cron is Mon-Fri only (`15 9 * * 1-5`), so it simply isn't running on a Sunday. Confirmed via a real incident this session — a short entered Friday 2026-01-30 got stopped out one minute into that Sunday session, a fill no live deployment could ever have produced — this is a permanent fix to the historical data, not a one-off patch; every number below reflects it. (The live-production analogue — a known-bad session re-entering the *daily* ST re-seed window rather than one-time historical data — is tracked as `ST_SEED_SKIP_DATES` in `plans/prometheus-phase3-production.md` §10.)

**Backtest/production timing-guard parity fix (2026-09-11) — every number in this Phase 3 section below was recomputed under the corrected simulator, a bigger correction than a normal data refresh.** User caught this by directly questioning a specific trade: a backtested target1 fill that, per production's own execution log, had actually exited at a loss via `trend_flip`. Checked the raw 1-min price data and confirmed the backtest had credited a fill off a single wild opening-bar print (8799–9141 in one minute) that settled into a tight, unrelated range for the rest of the session — exactly the price-discovery-artifact mechanism §11/§11a already document, just hitting the exit side this time instead of the entry side.

Root cause: production has two dynamic session-open guards sharing `_minutes_since_session_open` (`prometheus.py`) — `_past_first_minute_guard`/`NO_EXIT_BEFORE_BUFFER_MIN=1` blocks LTP-driven SL/target checks, `_past_min_entry_guard`/`MIN_ENTRY_BUFFER_MIN=15` blocks fresh entries — until that many minutes have genuinely elapsed since the session's own first bar (dynamic, not a hardcoded clock time, because an evening-only special session opens at 17:00). Neither backtest simulator replicated either guard: `exit_calib_p3.py`/`bespoke_2lot_p3.py`'s bar-walking loop checked SL/target against every bar including the session's very first one, and `backtest_p3.py`'s entry gate used a hardcoded `MIN_ENTRY_TIME='09:15'` clock-time check that coincidentally worked on a normal 09:00 session but did nothing on an evening-only one (17:00 is already past 09:15 on the clock) — the exact same bug class production itself fixed 2026-09-04 (commit `a483c7d`) for its own entry guard, just never ported back into the backtest.

**Scope, measured directly before fixing anything:** 59 of 780 lot-exit events (7.6%) in the then-current CRUDEOILM mult 2.0 set landed exactly on a session's opening bar with an SL/target reason; 1 of 390 entries (the 2026-05-01 evening-only session) would have been blocked by the entry guard. Fixed at the source: `trade_paths_p3.py` now walks a still-open trade to the end of available data instead of skipping it (needed so the exit guard has bars to apply to); `exit_calib_p3.py`/`bespoke_2lot_p3.py` skip any bar inside the `NO_EXIT_BEFORE_BUFFER_MIN` window rather than checking it; `backtest_p3.py`'s entry gate now measures elapsed minutes since each day's own first bar (`_first_bar_by_day`) instead of a clock time. Mirrored into `phase3_crudeoil/`, including the T1 fine-grid follow-up scripts below (their output is a live parameter, not a frozen historical snapshot, so they needed the fix too).

**Acceptance test, independently sourced from the user's own production-log check:** the trade this was caught on now reads `lot1_exit_reason=trend_flip`, `lot1_pnl_rs=-590`, `total_pnl_rs=-1180` — both lots losing via trend-flip, exactly matching what actually happened live. **The T1 fine-grid investigation below was fully re-run under the corrected simulator** (not left as a stale historical snapshot, since its output — `TARGET1_PCT=2.2`— is what production runs today): **T1=2.2% is independently reconfirmed as the fine-grid's own Calmar-optimal point (14.76, the single highest value in the re-run grid)**, not just "near the plateau's best" as the original 2026-09-09 finding had it. The live decision doesn't change; it comes out of this correction with direct confirmation rather than needing to rely on the uncorrected version. Full before/after figures for both bespoke candidates in the "Two calibrated candidates" table below.

**Raw signal-quality sweep results** (`ST_PERIOD=10`, no SL/target/EOD, refreshed 2026-09-12 through 2026-09-11 — part of the routine `refresh_pipeline.py` chain, see "Routine backtest refresh" below; `sweep_p3.py` re-run in full, per CLAUDE.md's "one variable changed" convention there's no reason a data refresh alone should touch the grid selectively):

| Multiplier | Trades | Win % | Total P&L | Max DD | Calmar |
|---|---|---|---|---|---|
| 2.0 | 390 | 42.3% | ₹156,910 | (see sweep_p3_summary.csv) | — |
| 2.5 | 296 | 42.6% | ₹136,290 | (see sweep_p3_summary.csv) | — |
| 3.0 | 242 | 40.1% | ₹73,980 | (see sweep_p3_summary.csv) | — |
| 3.5 | 207 | 38.6% | ₹45,210 | (see sweep_p3_summary.csv) | — |
| 4.0 | 167 | 38.9% | ₹39,700 | (see sweep_p3_summary.csv) | — |
| 4.5 | 137 | 39.4% | ₹60,960 | (see sweep_p3_summary.csv) | — |
| 5.0 | 123 | 39.0% | ₹38,270 | (see sweep_p3_summary.csv) | — |
| 5.5 | 103 | 40.8% | ₹37,920 | (see sweep_p3_summary.csv) | — |

(Trade counts above are closed trades only — each multiplier also has exactly 1 trade still open at data end.) A routine data-only refresh this time (one additional trading day, 2026-09-11) — every multiplier gained 1-2 closed trades, consistent with ordinary new price data rather than any guard/methodology change. `sweep_p3.py`'s own summary doesn't compute Calmar for the raw (no-SL/target) series — the qualitative finding stands regardless: raw Calmar climbed steadily from 5.5 down to 2.5, then flattened extending one step further to 2.0 rather than continuing to climb — the signature that argues against 2.5 being purely an under-explored grid-edge artifact.

**Exit calibration winners, all multipliers** (SL/target1/target2 grids: 1.0–3.5% / 0.5–2.0% / 1.5–6.0%, Calmar-selected at each stage, refreshed 2026-09-11 — see "Backtest/production timing-guard parity fix" below for why every figure moved from the 2026-09-04 vintage previously shown here, not just from fresh data):

| Multiplier | SL% | T1% | T2% | Calmar | Total P&L | Max DD |
|---|---|---|---|---|---|---|
| 2.0 | 2.2 | 2.0 | 6.0 | 13.61 | ₹209,515 | −₹15,394 |
| 2.5 | 1.0 | 0.75 | 2.5 | 11.65 | ₹126,011 | −₹10,814 |
| 3.0 | 1.0 | 0.75 | 2.5 | 8.79 | ₹88,504 | −₹10,070 |
| 3.5 | 1.4 | 1.75 | 2.0 | 3.09 | ₹62,361 | −₹20,171 |
| 4.0 | 2.6 | 1.50 | 2.0 | 2.97 | ₹53,921 | −₹18,162 |
| 4.5 | 1.8 | 1.00 | 2.5 | 6.35 | ₹54,696 | −₹8,620 |
| 5.0 | 1.0 | 0.75 | 5.0 | 8.42 | ₹74,984 | −₹8,909 |
| 5.5 | 1.0 | 1.75 | 6.0 | 10.25 | ₹86,074 | −₹8,400 |

Mult 2.0 still leads on Calmar by a clear margin, unchanged qualitative conclusion from every prior run of this table. **Mult 2.0's own T2 landed at this grid's edge (6.0%, same edge-of-grid question T1 already went through once)** — flagged as an open thread, not resolved here: this base-grid Stage 3 pins T1 at 2.0% throughout (the ORIGINAL pre-widening value), not production's actual T1=2.2%, so it doesn't bear directly on the live config — see the fine-grid reconfirmation below for that. A dedicated T2-widening pass (mirroring T1's own widen-then-fine-grid treatment) is a reasonable next step if this table's own joint optimum is ever wanted, but isn't required to trust the currently-decided T1=2.2%/T2=5.0 combo, which was independently re-derived and re-confirmed on its own terms just below.

(Calmar/max-DD here use the per-trade, lot1+lot2-combined equity series that `exit_calib_p3.py` itself computes; the two candidate write-up below uses a slightly more precise per-lot-*exit* equity series instead — see the artifact note under Supporting analysis for why the two differ by a small amount.)

**These winners don't agree with each other, and that matters.** SL ranges 1.0–2.6%, target1 0.75–2.0%, target2 2.5–6.0% across the grid — nothing close to Phase 2's experience of one combo (1.8/1.0/2.3) cross-validating cleanly across two instruments. A robustness check (fixed SL/T1/T2 combos applied *unchanged* across every multiplier, rather than each getting its own bespoke tuning) found SL 1.8/T1 1.0/T2 3.0 as the most robust single choice — min-Calmar 1.80 across the grid vs. 0.20 for a combo built around 2.5's own bespoke values — but that check predates the 2026-09-01 data refresh and multiplier 2.0's existence (and hasn't been re-run against the 2026-09-04 refresh either), so treat it as directional, not current.

**Drawdown methodology: per-trade, not per-lot-exit (changed 2026-09-11).** Every Calmar/max- drawdown figure below, in the dynamic-sizing sections, and in Risk of Ruin now credits each trade's lot1+lot2 P&L as ONE combined cash-flow event at the later of the two lots' own exit timestamps — not, as before, each lot as its own separate chronological event. Reasoning (user decision): the whole point of this drawdown figure is to inform base-capital sizing and capital efficiency, and the actual sizing mechanism (`_calculate_units()`, mirrored in `dynamic_sizing_sim.py`) only ever reads capital **once per trade, at entry** — using capital *after* the prior trade's both lots have already closed, never a lot1-only intermediate value (confirmed directly in `dynamic_sizing_sim.py`: `capital = capital_after` only updates once, after both lots' P&L is summed). Per-lot-exit was capturing something real — lot1 and lot2 genuinely settle at different timestamps, so real account equity does step through an intermediate value — but that intermediate step is only relevant to a different question (real-time margin-call risk, which this project doesn't currently model at all — no intraday mark-to-market is tracked either way), not to the capital-sizing question this metric exists to answer. `exit_calib_p3.py`'s own calibration grid (the "Exit calibration winners" table above and the T1 fine-grid re-run) was **already** per-trade — it never needed changing; this brings `two_candidate_stats_p3.py` and both `dynamic_sizing_sim*.py` scripts into line with it. **Risk of Ruin was also already per-trade** (bootstrap-resamples `total_pnl_rs`, which was always lot1+lot2 combined) — confirmed by re-running it and getting byte-identical output, so its own section below is unaffected by this change. Numbers move by varying amounts depending on whether a given max-drawdown episode happened to contain an intra-trade lot1-then-lot2 (or lot2-then-lot1) offsetting pattern — CRUDEOILM mult 2.0's trough did, CRUDEOIL mult 2.0's didn't (Calmar unchanged there), illustrating that this is a real, data-dependent effect, not a uniform correction factor.

**Two calibrated candidates — DECIDED 2026-09-04: mult 2.0** (adopted live in `prometheus_production/` the same day, after confirming mult 3.0's live ST matched the chart first). **Mult 2.0's `TARGET1_PCT` updated 2.0 → 2.2 on 2026-09-09** (caveat #1 below) — this is now Prometheus's final exit configuration. **Refreshed 2026-09-12 through 2026-09-11's data, per-trade drawdown methodology** (see immediately above):

| Metric | Mult 2.0 (SL 2.2/T1 2.2/T2 5.0) | Mult 2.5 (SL 1.0/T1 1.25/T2 4.0) |
|---|---|---|
| Total trades | 390 | 296 |
| Win % | 45.38% | 48.65% |
| Total P&L | ₹209,600 | ₹138,466 |
| Avg win / avg loss | ₹3,356 / −₹1,805 | ₹2,467 / −₹1,426 |
| Max win / max loss | ₹14,120 / −₹6,440 | ₹13,220 / −₹6,860 |
| Max drawdown | −₹13,944 | −₹11,973 |
| Calmar | 15.03 | 11.56 |

(Mult 2.5's row is byte-identical to the prior refresh — this refresh's one additional trading day changed mult 2.5's *raw* signal sequence by one trade, but that trade was the previously-"still open" entry finally getting its raw trend-flip confirmed; `bespoke_2lot_p3.py` had already resolved that same entry via its own SL/target walk-forward in the prior refresh, so the bespoke entry-level table doesn't move. Not a bug — see the exit-reason-mix note below for the mult 2.0 analogue.)

**CRUDEOIL cross-validation — done 2026-09-07, re-run 2026-09-09 at T1=2.2%, refreshed again 2026-09-12 through 2026-09-11's data under the same per-trade methodology** (`prometheus_backtest/phase3_crudeoil/`, identical pipeline to the CRUDEOILM run above, `SYMBOL` the only change, stored in a separate sibling folder rather than overwriting this one). Same per-trade Calmar/drawdown methodology as the table above, computed directly from the refreshed `bespoke_trade_summary.csv` files (matching how the CRUDEOILM figures above were produced, not the coarser `exit_calib_p3_winners.csv` method):

| Metric | Mult 2.0, CRUDEOIL | Mult 2.5, CRUDEOIL |
|---|---|---|
| Total trades | 410 | 297 |
| Win % | 42.44% | 49.16% |
| Total P&L | ₹1,968,284 | ₹1,393,729 |
| Avg win / avg loss | ₹34,418 / −₹17,036 | ₹24,146 / −₹14,117 |
| Max win / max loss | ₹140,200 / −₹64,600 | ₹131,400 / −₹69,000 |
| Max drawdown | −₹200,752 | −₹145,437 |
| Calmar | **9.8** (vs. CRUDEOILM's 15.03) | **9.58** (vs. CRUDEOILM's 11.56) |

**The edge holds directionally on the full-size contract but not at matching risk-adjusted quality.** Both candidates stay clearly profitable — win rates land within ~3 points of the mini-contract figures. On CRUDEOIL specifically the two candidates' Calmar values are close enough (9.8 vs. 9.58) that neither is read as the clear winner there — mult 2.0's own max drawdown happened not to contain an intra-trade offsetting pattern (unchanged by the per-trade methodology switch above), while mult 2.5's did, so the gap that favored mult 2.0 before has narrowed to a coin flip; the production decision doesn't depend on the CRUDEOIL ranking either way, since CRUDEOILM is what's actually traded and there mult 2.0 leads clearly (15.03 vs 11.56). P&L scales up only ~9.4x (mult 2.0) / ~10.1x (mult 2.5) while max drawdown scales up ~14.4x (mult 2.0) / ~12.1x (mult 2.5) relative to CRUDEOILM — noticeably more than the 10x lot-size ratio alone would predict for mult 2.0 — so CRUDEOIL's drawdowns still run proportionally deeper against its own return than CRUDEOILM's do. This is not a like-for-like guarantee that the live strategy (calibrated and risk-managed specifically against CRUDEOILM's own tighter profile) would perform equivalently if traded on the full-size contract instead — it's the reason Prometheus trades CRUDEOILM, not a reason to doubt the calibration.

**One-day flip-frequency anomaly checked and cleared, 2026-09-11.** CRUDEOIL picked up 5 new trades this refresh (405→410) against CRUDEOILM's 1 (388→389) from the identical single day of new data (2026-09-10) — all 5 exiting via `trend_flip` within 1.25-3.25 hours each, a rapid-whipsaw shape worth checking given CRUDEOILM's own real production log that same day showed exactly one flip all session. Checked the raw 1-min data at all 5 flip timestamps directly for the DPL circuit-freeze signature (§11a-style: `open==high==low==close` for 3+ consecutive minutes) — found none; every window shows ordinary, continuous, real trading. (CRUDEOIL *did* have a real circuit freeze that day, 21:18-21:31 at the same 9644 level as CRUDEOILM's — but that's hours after all 5 of these trades had already closed, so it isn't the cause.) Read as CRUDEOIL's already-documented choppier character, not a new artifact.

*(Historical, superseded by the 2026-09-09 refresh in the table above — kept for the refresh trail.)* Mult 2.0 refreshed 2026-09-06 (through 2026-09-04) — `bespoke_2lot_p3.py` re-run against the freshly-synced data covering Friday's live session, which included one more raw flip (bearish trade 381 closing into the real bullish position entered 20:15). Mult 2.5's column was still the 2026-09-04-vintage figure (through 2026-09-03) at that point, not re-run that pass. Calmar moved 10.21→10.41 for mult 2.0 (at the then-current T1=2.0%); drawdown reproduced the original figure exactly (−16,625 unchanged), confirming the per-lot-exit-event methodology was applied consistently. Both columns are now on the same current vintage (see table above) and mult 2.0 runs T1=2.2%, not 2.0% — this paragraph's own numbers no longer apply.

**Open caveats on both candidates, not yet resolved:**
1. **Mult 2.0's `TARGET1_PCT` grid-edge caveat — RESOLVED 2026-09-09**, see `exit_calib_p3_t1_widen.py` (new script, `data_sweep/exit_calib_p3_t1_widen_mult20.csv`). Widened the T1 grid from the original 0.5%–2.0% out to 4.0% (0.25% spacing throughout), 381-trade current vintage. One methodology change was forced: the original grid pinned `target2` at `T2_STARTING_DEFAULT=2.3%` while searching T1, but `_simulate_trade`'s own `target2_dist > target1_dist` assertion makes 2.3% infeasible once T1 is tested past 2.3% — so the widened sweep pins `target2` at the actual production value (5.0%) instead, across the *full* 0.5%–4.0% range (not spliced at 2.0%), answering the more relevant question directly: given the real decided SL/T2, is T1=2.0% still the best T1? Under this pin, Calmar at T1=2.0% reads 10.66 (not the original grid's 8.81 — different T2 pin, not a contradiction).

Result: Calmar climbs 0.5%→2.25% (7.03 → 6.93 → 7.65 → 9.03 → 8.78 → 9.36 → 10.66 → **12.31**), then drops sharply at 2.50% (9.62) and stays in the 7–9 range out to 4.0% (max 190,642 total P&L at 4.0%, but Calmar never returns above ~9.6). **This is not a cut-off — the curve genuinely peaks and reverses just past the old edge, confirming the edge-of-grid concern was legitimate but resolvable.** Re-running Stage 3 (T2 grid) at the new T1=2.25% winner confirms T2=5.0% is still the joint optimum (unchanged). Full joint-optimum comparison, same 381-trade set, same methodology:

   | Combo | Total P&L | Max DD | Calmar |
   |---|---|---|---|
   | Current production: SL 2.2 / T1 2.0 / T2 5.0 | ₹173,102 | −₹16,235 | 10.66 |
   | New candidate: SL 2.2 / **T1 2.25** / T2 5.0 | ₹176,884 | **−₹14,364** | **12.31** |

T1=2.25% beats production on every metric — higher P&L, shallower drawdown, +15.5% Calmar.

**Fine-grid follow-up, same day** (`exit_calib_p3_t1_fine.py`, `data_sweep/exit_calib_p3_t1_fine_mult20.csv`) — 0.05% steps from 2.00% to 2.50%, same SL=2.2%/T2=5.0% pins, to check whether the 12.31 peak was a single-grid-point noise artifact. **It is not.** Calmar sits in an 8-point *plateau* from 2.05% to 2.45% (11.80–12.77, one mild dip at 2.30%), and — tellingly — max drawdown is pinned at *exactly* −₹14,364 for 8 of those 11 points (2.05, 2.10, 2.15, 2.20, 2.25, 2.35, 2.40, 2.45), meaning the same worst losing sequence governs the whole plateau and only total P&L wobbles mildly (₹173k–₹183k) within it — the opposite of what single-point grid noise would look like. Then a genuine **structural cliff** at 2.50% exactly: drawdown jumps to −₹18,587 and Calmar falls to 9.62, a discrete break, not a gradual taper — some specific trade's outcome flips right at that threshold.

   | T1 | Total P&L | Max DD | Calmar |
   |---|---|---|---|
   | 2.00% (production) | ₹173,102 | −₹16,235 | 10.66 |
   | 2.05%–2.45% (plateau, 8/11 pts share max DD) | ₹175,928–₹183,402 | −₹14,364 (mostly) | 11.80–12.77 |
   | 2.50% (cliff) | ₹178,854 | −₹18,587 | 9.62 |

**Conclusion: the T1=2.0%→2.25%+ edge is real, not noise** — any value roughly 2.05%–2.45% outperformed the prior production value (2.0%) by a similar margin, which is much stronger evidence than one isolated best point would have been. The single best fine-grid point (T1=2.45%, Calmar 12.77) sits right next to the 2.50% cliff, so picking the exact grid maximum would mean choosing the riskiest point in the plateau if live conditions shift even slightly — **T1=2.2% was adopted instead** (2026-09-09, central-plateau pick, `prometheus_production/prometheus_configs.py`), trading a little backtested Calmar for distance from the cliff. This is now Prometheus's final exit configuration. CRUDEOIL cross-validation of T1=2.2% — done 2026-09-09, see the CRUDEOIL table above and caveat #3; the edge held. Still open: the in-sample-everywhere limitation (caveat #5) — this whole exercise, cliff included, was fit and evaluated on the same 381-trade window it's judged against.
2. **The two candidates are structurally different strategies, not the same mechanism at different scale.** Exit-reason mix (lot1 / lot2, of trades reaching each outcome, refreshed 2026-09-11 under the corrected simulator — see "Backtest/production timing-guard parity fix" above):

   | | Mult 2.0 lot 1 | Mult 2.0 lot 2 | Mult 2.5 lot 1 | Mult 2.5 lot 2 |
   |---|---|---|---|---|
   | trend_flip | 222 (56.9%) | 311 (79.7%) | 45 (15.2%) | 118 (39.9%) |
   | target | 139 (35.6%) | 46 (11.8%) | 145 (49.0%) | 52 (17.6%) |
   | stop_loss | 29 (7.4%) | 33 (8.5%) | 106 (35.8%) | 126 (42.6%) |

At 2.5, the tight 1.0% SL does most of the work (largest single exit-reason bucket for both lots). At 2.0, the wide 2.2% SL barely intervenes — most trades just ride to the raw trend_flip exit. **That trend_flip bucket is not benign for mult 2.0's lot 1**: 222 trades, only 18.5% win rate, −₹120,190 in aggregate — the single biggest loss center in the whole 2.0 system, bigger than the SL bucket itself (−₹60,823). The SL is correctly sized to catch *extreme* individual losers (mean −₹2,097/trade vs. trend_flip's −₹541), but the real drag on 2.0's lot 1 is a large population of trades that never reach either target and bleed out slowly — a signal-quality issue, not something a different SL or T1 fixes. Lot 2's trend_flip, by contrast, is genuinely closer to breakeven (−₹95 avg, 37.9% win rate) — the "let it play out" framing holds there, just not for lot 1. (This refresh's one new trade landed in mult 2.0's lot1-target/lot2-trend_flip cells — lot1's own trend_flip bucket is byte-identical to the prior refresh, so the −₹120,190/−₹541 figures above didn't move.)
3. **CRUDEOIL cross-validation — done 2026-09-07, re-run 2026-09-09 at T1=2.2%, refreshed again 2026-09-12 through 2026-09-11's data under the per-trade drawdown methodology** (see the table above): the edge replicates directionally on the full-size contract, but Calmar drops for both candidates (15.03→9.8 for mult 2.0, 11.56→9.58 for mult 2.5) — CRUDEOIL's drawdowns run proportionally deeper than CRUDEOILM's, not just larger by the 10x lot-size ratio. Doesn't change the mult-2.0 production decision (CRUDEOILM is the live-traded instrument, where mult 2.0 leads clearly), but means the live strategy's risk profile shouldn't be assumed to carry over unchanged if ever run on CRUDEOIL instead.
4. **No transaction costs modeled** (same convention as v1/Phase 2) — mult 2.0 has the highest trade count of any candidate (390 vs. 2.5's 296), making it the most cost-exposed once slippage/brokerage are added.
5. **In-sample selection throughout** — both the multiplier grid and every exit-parameter grid were selected on the same window they're evaluated against; no train/test split or walk-forward check has been run.

### Supporting analysis

- **Multiplier sensitivity (MAE/MFE/P&L distributions, equity curve, drawdown)** — published artifact (private): `https://claude.ai/code/artifact/1ce085fa-bb85-4b92-b777-81cdde674268`.
- **Scale-out vs. raw, Phase 2 vs. Phase 3, and mult 2.0 vs. 2.5** (equity curves, drawdown curves, full per-trade comparison tables, all three as separate sections on one page) — published artifact (private, **2026-09-01 data, not refreshed**): `https://claude.ai/code/artifact/624f0f27-8c12-4d5a-9e3a-9f050b34e087`. Originated the per-lot-exit-event equity/Calmar methodology (as it stood at the time) — a finer-grained cash-flow series than `exit_calib_p3.py`'s own per-trade summary, treating each lot's own exit as its own chronological cash-flow event rather than bundling both lots' P&L at the trade's completion, so its max-DD figures read a little deeper (e.g. mult 2.5: −₹11,219 vs. −₹10,619 in the then-current `exit_calib_p3_winners.csv`) because it can see a dip that opens and closes entirely between one trade's lot 1 exit and its lot 2 exit. **Superseded 2026-09-11**: the two-candidate table above switched to per-trade (lot1+lot2 combined) for a reasoned, deliberate capital-sizing reason — see "Drawdown methodology: per-trade, not per-lot-exit" — so this artifact's own methodology (and its stale 2026-09-01 data) no longer matches the live table; kept here as the historical record of where per-lot-exit-event originated, not as a cross-reference to trust for current figures.
- **Early MFE as a predictor of trade outcome (2026-09-08)** — prompted by watching a live trade stall at only ~2 points of MFE. Ad-hoc analysis (not a committed script) against all 381 mult-2.0 bespoke trades: for each, measured running MFE at fixed early checkpoints (15/30/60/120/240 min since entry) using only trades still genuinely open at that checkpoint — no lookahead, a trade already closed before a given checkpoint is excluded from that checkpoint's cohort — then correlated against the trade's eventual `total_pnl_rs`.

  | Checkpoint | corr(MFE, P&L) | Low-MFE tercile win rate | High-MFE tercile win rate |
  |---|---:|---:|---:|
  | 15 min | +0.25 | 34% | 54% |
  | 30 min | +0.35 | 31% | 60% |
  | 60 min | +0.38 | 27% | 68% |
  | 120 min | +0.39 | 28% | 74% |
  | 240 min | +0.40 | 37% | 92% |

Correlation strengthens the longer the trade survives; the bottom MFE tercile is net-negative in mean P&L at every checkpoint. The user's own trigger case checks out: trades with ≤2 points of MFE within the first 60 minutes (n=21) went on to a 33.3% win rate and −₹652 mean P&L, vs. 46.1%/+₹576 for the rest. Stop-loss trades also have much lower final MFE (median 42 pts) than non-stop trades (median 112 pts). Moderate correlation, not a hard rule — even the worst bucket still has a quarter-to-a-third of eventual winners, so this isn't grounds to override SL/exit logic on its own. Purely descriptive/correlational so far — see the open-threads entry below for where this could go next.
- **Lot 2's trend_flip win rate is a blend of two structurally different populations, not one uniform 37.4% (2026-09-09)** — prompted by noticing lot2's overall trend_flip win rate (37.4%) sits suspiciously close to lot1's target1 hit rate (35.2%). Cross-tabulated lot1's own exit reason against lot2's, for the 305 trades where lot2 exits via trend_flip:

  | Lot1's own fate | Lot2 trend_flip win rate | Share of lot2's trend_flip trades |
  |---|---:|---:|
  | Lot1 also trend_flipped (never reached target1) | 18.6% | 220 / 305 (72%) |
  | Lot1 hit target1 first | **85.9%** (avg +₹997) | 85 / 305 (28%) |

The first row isn't a separate finding — when lot1 and lot2 exit at the same simultaneous trend-flip event (neither having reached a target), they share the same entry and exit price, so their win/loss outcome is identical by construction; that row's 18.6% matches lot1's own overall trend_flip win rate exactly, as it must. The second row is the real signal: once lot1 has already banked +2.2%, the Supertrend has clearly trailed up enough that a *later* flip still leaves lot2 net-positive 86% of the time. Zooming out further — given lot1 hits target1 at all (136 trades), lot2 wins **88.2%** of the time regardless of how it eventually exits (target2: 47, trend_flip- and-still-winning: 73, stop_loss: only 4). So "lot1 reaching T1" is a genuine forward signal for lot2's eventual outcome, not a coincidence in the aggregate stats. See the open-threads entry below — this turned out to be actionable, though not via the first (breakeven) rule tried.

### Not yet done / open threads

- **Early-MFE signal (above): worth watching, not yet turned into a rule.** Open questions before this becomes anything actionable: (1) does the same early-MFE/outcome relationship hold on CRUDEOIL's own 398 trades, or is it a CRUDEOILM-specific artifact of this particular price history? (2) if a live rule were built on it (e.g. an early tightened stop, or an alert rather than an auto-action, when MFE stays below some threshold past a fixed time), what's the false-positive cost — the ~30% of low-MFE trades that still win would be the ones a premature exit gives up? (3) is there a cleaner single early-checkpoint to standardize on (60min looks like a reasonable point where the signal is already fairly strong without waiting too long) rather than reporting all five? None of this has been tested as an actual rule change yet — currently just a live-monitoring signal to watch, per the user's request, not a backtested optimization.

- ~~**Lot2 stop-trailing after lot1's target1** (2026-09-09) — DECIDED: documented, not adopted.~~ Breakeven rejected, a genuine-looking improvement found at a shallower trail, but it didn't survive CRUDEOIL cross-validation. Candidate rule: once lot1 exits via target1, move lot2's own stop-loss up from the original wide `SL_PCT` distance to some level between breakeven and target1 itself. Three stages:

  1. **Exact breakeven, `phase3/lot2_breakeven_after_t1_p3.py`** — full minute-by-minute re-simulation (not just the categorical cross-tab above), same 386 trades. **The qualitative reasoning going in was wrong in one respect, caught only by actually simulating it**: a breakeven stop can still fire on a trade that later goes on to a big win (price dips to breakeven, the stop fires, price then recovers and would have hit target2) — 7 of 386 trades lost upside this way, one of them a −₹4,782.5 hit (a trade that would have reached target2; walked through in detail as trade #111 in this session's conversation — a bearish trade during a real overnight volatility spike). Against that, 13 trades improved (previously-losing post-target1 cases correctly converted to scratches). Net: total P&L improves modestly (₹184,892 → ₹186,480, +0.9%) but max drawdown worsens (−₹15,267 → −₹16,247) and **Calmar drops** (12.11 → 11.48) — on this project's own primary decision metric, exact breakeven is a net negative. **Rejected as a candidate.**
  2. **Was it a gap problem?** Checked whether the retracements that punish a tight trail are mostly rare gap events (which a rule could special-case around) or routine continuous-trading moves. Of the 136 lot1-booked trades, 25 ever retraced back through breakeven; only **6 were preceded by a real time/session gap**, the other **19 (76%) retraced through breakeven during perfectly ordinary continuous trading**. So this isn't a tail-event problem — Supertrend's own ATR-based trailing band (mult 2.0, 15-min bars) is routinely wider than the fixed 2.2% breakeven/SL levels, independent of gaps. Trade #111 itself breached breakeven gradually (26 minutes after T1, ordinary 1-minute bar spacing) — the dramatic overnight move happened *after* a breakeven stop would already have fired, so it isn't actually a gap-driven counterexample to this rule despite the eye-catching magnitude.
  3. **Grid search over the trail level, `phase3/lot2_trail_after_t1_grid_p3.py` (0.2% steps, 2.2% down to breakeven) then `phase3/lot2_trail_after_t1_fine_p3.py` (0.05% steps, 0.6–1.8%, covering the coarse grid's apparent plateau)** — same noise-vs-genuine-structure check already applied to the `TARGET1_PCT` grid (caveat #1). Result: a genuine, broad plateau from **0.85% to 1.75%** (18 consecutive 0.05%-spaced points sharing the exact same −₹14,364 max drawdown — not a single lucky point), peaking at **trail=0.90%**: total P&L ₹200,073, max drawdown −₹14,364, **Calmar 13.93** — beating the no-rule baseline (₹184,892 / −₹15,267 / 12.11) on *every* metric simultaneously, and sitting comfortably mid-plateau rather than at a fragile edge.

  4. **CRUDEOIL cross-validation — done 2026-09-09, result: does NOT replicate.** `phase3_crudeoil/lot2_trail_after_t1_grid_p3.py` then `_fine_p3.py`, identical grids and methodology, CRUDEOIL's own 403-trade `bespoke_trade_summary.csv`. **No trail level in either grid beats the no-rule baseline** (Calmar 7.40, ₹1,624,376, −₹219,408). The coarse grid's best point (trail=2.0%, Calmar 7.11) already falls short; the fine grid confirms it — best point trail=1.8% at Calmar 6.94, still below baseline. Worse, max drawdown *deepens* to −₹243,776 (an 11% deeper drawdown than baseline) across nearly the entire 0.0%–1.75% range on CRUDEOIL, the opposite of the shallower drawdown found on CRUDEOILM's own plateau. This is a real, decisive negative cross-check, not a marginal one — the edge that looked robust on CRUDEOILM (broad plateau, both metrics improving together) is CRUDEOILM-specific, not a property of the underlying signal/mechanism that should be expected to transfer.

  5. **Why it fails on CRUDEOIL — trade-level investigation, done 2026-09-09/10, at CRUDEOILM's own chosen trail=0.90% for direct comparability.** Of CRUDEOIL's 139 lot1-hit-target1 trades, 50 had lot2's outcome changed by the trail; net effect −₹20,825, matching the grid's negative result. But that net hides a sharp concentration: **5 single trades account for −₹203,712 (73%) of all lost upside**, and all 5 share the same shape — lot2 was already on track for the full **target2** win (the strategy's biggest payoff) and got stopped out early instead, with no credit for the move it was already going to complete. Two sub-mechanisms:
     - **Gap-driven (2 of 5).** Trade #114: a 3-day weekend gap bar opened *below* the trail level entirely (open 8867 vs. trail 9099), forcing a same-bar exit near the gap price — then that same bar rallied to a high of 9610, clearing target2 (9468.9). The original 2.2% SL (8819.6) survived the gap by 0.4 points and banked +₹45,090; the trail turned it into −₹15,100 — a −₹60,190 swing from one trade. Trade #5 is a same-bar T1-then-trail-check variant at a holiday reopen.
     - **Ordinary, no-gap retracements (3 of 5 — the more important case).** Trades #165, #128, #94 show no gap at all (`gap_min=1.0` throughout). Trade #94 walked in detail: 20 minutes after T1, price pulled back a routine ~120 points (1.4% of entry) — overshooting the 0.90% trail by just 8 points — then reversed immediately and rallied 350+ more points to clear target2 comfortably the same session. The trail wasn't defending against a crash; ordinary noise clipped it. Across all 50 affected trades, only 20% (10/50) are gap-preceded — close to the 24% found for CRUDEOILM's trade #111 (stage 2 above), so this isn't "CRUDEOIL is gappier." The real mechanism: CRUDEOIL's typical post-target1 pullback depth, as a % of entry price, runs deeper than what a trail fitted to CRUDEOILM's own pullback distribution can survive — so it repeatedly stops out CRUDEOIL's biggest winners (the target2 trades) before they complete, which is exactly why max drawdown *worsens* rather than improves across nearly the whole CRUDEOIL grid. This is the concrete mechanism behind the CRUDEOILM-specificity concern raised in the decision below.

**Decision (user, 2026-09-09): leave documented, not adopted for now.** CRUDEOILM is the live-traded instrument, so the negative CRUDEOIL result doesn't mechanically block using the rule there — but it materially weakens confidence that the CRUDEOILM plateau reflects a genuine, general mechanism (lot2 stop-trailing after a T1 hit) rather than a pattern specific to CRUDEOILM's own price history. The stated reasoning for not pursuing it further right now: even CRUDEOILM's own sample is too small to call this decisively — the plateau's *shape* looks real (18 fine-grid points sharing an identical max drawdown, not one lucky point), but only 136 of 386 trades ever have lot1 reach target1 at all, and only ~20 of those are where the different trail levels actually diverge from each other — a genuinely small base to build a new, not-yet-existing production mechanism on top of, on top of the usual in-sample caveat (fit and evaluated on the same window it's judged against, no train/test split). Revisit if a substantially longer backtest window becomes available, or if this pattern reappears independently in a future data refresh.

- ~~**CRUDEOIL cross-validation** for mult 2.0 (the decided candidate)~~ — done 2026-09-07, see open caveat #3 above and the two-candidate table. Edge replicates, Calmar is meaningfully lower on the full-size contract.
- ~~**Mult 2.0's `TARGET1_PCT` grid edge** (open caveat #1)~~ — done 2026-09-09, see caveat #1 above, including same-day fine-grid follow-up and CRUDEOIL re-validation. Confirmed a genuine ~2.05-2.45% plateau (not a single noisy point, not a cut-off), beating the prior T1=2.0% on P&L, drawdown, and Calmar across the whole plateau, bounded by a real structural cliff exactly at 2.50%. **T1=2.2% adopted as Prometheus's final exit configuration** (central-plateau pick, not the exact grid maximum) and cross-validated on CRUDEOIL the same day — edge held (Calmar 7.40 vs. mult 2.5's 7.87, consistent ranking with CRUDEOILM, figures refreshed again 2026-09-09 through 2026-09-08's data). Still open: a train/test split given caveat #5's in-sample-everywhere limitation still applies here too — this whole exercise, cliff included, was fit and evaluated on the same 381-trade window it's judged against.
- Re-run the robustness check (fixed combo across the whole multiplier grid) against the 2026-09-04 data — the version quoted above predates the 2026-09-01 refresh and multiplier 2.0's existence, and hasn't been re-run since.
- Transaction-cost modeling, given how trade-count-sensitive the candidates are to each other.
- Once a candidate is chosen: fold it into `configs_p3.py` as the default, and decide whether Phase 3 supersedes Phase 2 as the production target or runs alongside it.

## Phase 4 — 1h/15m ST alignment entry filter (tested, SHELVED 2026-09-04)

Folder: `prometheus_backtest/phase4/`.

**Motivation.** `prometheus_production/` already has an entry-time regime-confirmation gate built in (`_check_1h_alignment`, plan §17 preview) — a 1-hour-timeframe Supertrend that must agree with the 15m entry signal's own direction before a fresh entry, a Rule 7 re-entry, an evening rollover reopen, or a missed-rollover recovery reopen is allowed to proceed. It ships gated off (`ENTRY_FILTER_1H_ALIGN_ENABLED=False`) pending this backtest. ST_15 held fixed at Phase 3's decided mult-2.0 candidate (period 10, mult 2.0) throughout — only the 1h filter's own `ST_1H_PERIOD`/`ST_1H_MULTIPLIER` varies, one variable changed per experiment.

**Method.** The filter can only *remove* an entry a 15m flip would otherwise take, never add one — ST_15 flips are price-only and position-independent, so blocking one entry never moves any other trade's own `signal_ts`/`exit_ts`. This makes it valid to evaluate by filtering Phase 3's already-generated raw mult-2.0 trade list (`phase3/data_sweep/mult_2.0/`) rather than running a second parallel backtest engine: build the 1h ST series (`filter_1h_p4.build_1h_st_series`, reusing `data_loader.resample_ohlcv` unchanged — its per-day `origin=day.index[0]` anchor already constructs genuine trailing partial 1h buckets, including on the 7/153 evening-only special sessions that start at 17:00 rather than 09:00, confirmed against the user's own chart), check each trade's alignment at its own decision time (`filter_1h_p4.check_alignment` — decision_ts = signal_ts + 15min, the moment the 15m bar's window actually closes and the flip becomes known; only the LAST FULLY-CLOSED 1h bar as of that instant may inform the gate, via `pd.merge_asof(..., direction='backward')` — same no-lookahead rule as `_check_1h_alignment` in production, and the same bug class as Phase 1's previously-fixed 15-min regime-filter lookahead, just a 6x wider window), then re-apply the mult-2.0 bespoke exits (SL 2.2%/T1 2.0%/T2 5.0%) to both the kept and blocked subsets via `bespoke_2lot_p3._simulate_trade_detailed`.

**Files:**
- `configs_p4.py` — ST_15 and exits fixed at Phase 3's decided values; `ST_1H_PERIOD_GRID`/ `ST_1H_MULTIPLIER_GRID` for the 1h filter under test; `DECISION_OFFSET_MIN=15`
- `filter_1h_p4.py` — `build_1h_st_series`, `check_alignment` (no-lookahead alignment check)
- `run_p4.py` — loads Phase 3's raw trades/paths once, loops the full grid, applies bespoke exits to kept/blocked subsets, computes per-lot-exit-event Calmar (same methodology as Phase 3's two-candidate table, for direct comparability) for both, and reports `n_1h_flips` per cell (a degenerate-overfitting tripwire — as the 1h filter's own period/multiplier shrink toward ST_15-like responsiveness, it stops being a regime filter and becomes a near-duplicate of the entry signal itself). Doubles as the sweep entry point (loops `configs_p4.py`'s grid); no separate `sweep_p4.py` was needed.

**Result: every cell underperforms the unfiltered baseline, and the failure mode is structural.**

Grid: period ∈ {3, 4, 5, 7, 10} × multiplier ∈ {1.0, 1.5, 2.0, 2.5, 3.0} (25 cells), against the mult-2.0 baseline (380 trades, ₹169,779, Calmar 10.21 — see Phase 3's two-candidate table).

| | Best cell (period 3, mult 2.5) | Worst cell (period 10, mult 2.0) |
|---|---|---|
| Kept trades | 153 | 135 |
| Kept total P&L | ₹105,401 | ₹72,765 |
| Kept Calmar | 9.67 | 2.05 |
| Blocked Calmar | 2.64 | 5.15 |
| Δ vs. baseline Calmar | −0.54 | −8.16 |

No cell in the grid reached baseline's Calmar of 10.21. Two distinct failure modes, split cleanly by multiplier:

1. **Below mult 2.5 (15 of 25 cells): the filter anti-selects.** Blocked-set Calmar exceeds kept-set Calmar at every one of these cells (e.g. the original preview setting, period 10/mult 2.0: kept Calmar 2.05 vs. blocked Calmar 5.15) — the filter systematically keeps the *worse* trades and blocks the *better* ones. Confirmed not a directional bug (bullish/bearish pass rates are balanced, 38.1%/33.0% at the preview setting) and not concentration of the known-bad `trend_flip` population in one bucket (that population is ~55% of both kept and blocked, but the kept share of it has a *worse* win rate — 12.0% vs. 17.2% blocked). At this multiplier range the 1h ST flips 110–245 times over the window — too often to function as a slow regime confirmation, not often enough to track ST_15 usefully either.
2. **At mult ≥ 2.5 (10 of 25 cells): the sign corrects, but there's still no edge.** Fewer, more decisive 1h flips (62–76 over the window) do correctly separate better trades from worse (kept Calmar > blocked Calmar in all 10 cells) — but the best of these (9.67) still trails the unfiltered baseline (10.21) on both Calmar and raw P&L, while discarding ~60% of trades to get there. There's no compensating edge once the sign is right, only a smaller sample of a strategy that was already fine.

**Conclusion: shelved, not re-tuned.** This isn't a calibration gap — the full grid was already run. The 1h/15m alignment premise doesn't survive contact with the data: it either damages trade selection outright (mult < 2.5) or simply subtracts a large fraction of otherwise-good trades for no benefit (mult ≥ 2.5). `ENTRY_FILTER_1H_ALIGN_ENABLED` stays `False` in `prometheus_production/`; no live re-test is warranted on this evidence. The mechanism (and its production call sites — fresh entry, Rule 7 re-entry, rollover reopen, missed-rollover recovery) remains in the codebase, gated off, in case a future signal redesign wants a differently-shaped regime filter — but the specific "1h Supertrend agreement" mechanism itself is the finding here, not just this particular grid.

### Not yet done / open threads

- No CRUDEOIL cross-validation — moot while the mechanism is shelved on CRUDEOILM.
- A wider grid (finer multiplier steps between 2.0 and 3.0, where the sign flips) was not run — not pursued, since even the sign-correct region never beat baseline; diminishing-returns territory rather than a promising lead.

## Position sizing — volume/participation analysis for scaling on CRUDEOILM (2026-09-07)

**Context.** User's plan is to trade CRUDEOILM as the primary instrument (not CRUDEOIL — see the cross-validation finding above; the mini's lower per-lot capital requirement and MCX's 1000-lot freeze limit both leave plenty of headroom to scale). Before scaling `STATIC_UNITS` up, the question was how much real market depth backs each additional lot, since Prometheus's backtest P&L (like every phase above) is deliberately cost-free (`SLIPPAGE_ENABLED = False` throughout) and says nothing about fill quality at size.

**Units check, done first.** `getCandleData`'s `volume` field is number of **contracts (lots)** traded in the bar, not underlying barrels — confirmed by comparing CRUDEOILM (10 bbl/lot) and CRUDEOIL (100 bbl/lot) at identical timestamps: both report comparable per-minute volume magnitudes (e.g. 2026-09-04 23:2x: CRUDEOILM 25–566, CRUDEOIL 26–154). If `volume` were in barrels, CRUDEOIL's 10x-larger lot would inflate its barrel-count roughly 10x for comparable participation; instead the two contracts print the same order of magnitude, consistent with each being its own independently-traded pool of lots. Every figure below is in lots, directly comparable to an order size in lots. Tick size is 1.0 (all OHLC prints are whole numbers) → 1 tick = ₹10/lot on CRUDEOILM.

**Method.** Loaded the full CRUDEOILM 1-min series via `data_loader.load_futures_1min` (same front-month de-duplication as every backtest phase). Whole-day volume averages flatter the picture, so participation was measured on the 1-min bar starting at each 15-min mark (:00/:15/ :30/:45, from 09:15 onward matching `MIN_ENTRY_TIME`) — 8,594 such bars across 6.5 months (2026-01-30 to 2026-09-04) — since that's the bar an ST_15-triggered order actually needs to fill against. 8 rows (of 130,366) carry a negative `volume` value, a known data-pipeline artifact; none fall on a 15-min boundary, so they don't affect this analysis and weren't otherwise investigated.

**Participation by order size** (share of that boundary-minute's own volume; percentile columns read as "in the worst N% of boundary-minutes, participation is at least this"):

| Order size (lots) | Median | p25 | p10 | p5 | Boundary-minutes ≥20% participation |
|---:|---:|---:|---:|---:|---:|
| 1 | 0.65% | 0.32% | 0.17% | 0.12% | 133 / 8,594 |
| 10 | 6.54% | 3.16% | 1.68% | 1.18% | 1,612 |
| 20 | 13.07% | 6.33% | 3.36% | 2.37% | 3,077 |
| 50 | 32.68% | 15.82% | 8.40% | 5.92% | 5,770 |
| 100 | 65.36% | 31.65% | 16.81% | 11.83% | 7,443 |
| 200 | 130.72% | 63.29% | 33.61% | 23.67% | 8,294 |

Median boundary-minute volume is 153 lots; the 10th-percentile (a genuinely thin minute) is 29 lots. Liquidity is meaningfully time-of-day-dependent: 15:00–close boundary minutes run ~2x 09:15–15:00 ones (median 204 vs. 96 lots) — thin-liquidity risk concentrates in the morning session.

**Slippage: framed in ticks, not a modeled ₹ figure.** No square-root-impact coefficient is applied — it would need calibration this dataset can't provide, and multiplying real volume data by an uncalibrated constant produces a number that looks derived without being one. The backtest's existing fill convention (`_target_fill_price`/`_stop_fill_price`) already prices in adverse gap-through at the bar open, so this is specifically about *additional* size-driven impact on top of that. Grounded read: at single-digit-to-teens lots, comfortably inside the spread most of the time. Past ~20-30% participation (roughly 30-50 lots per the table above), expect to reliably cross the spread and likely walk 1-2 ticks beyond (₹10-20/lot) on the worse-liquidity minutes; past ~100 lots, plan for multi-tick slippage and likely order-splitting well before MCX's 1,000- lot freeze limit (`freeze_qty=10000` underlying units / `LOT_SIZE=10` on CRUDEOILM).

**Decision (2026-09-07):** user's current capital supports scaling to 50 lots; plan is to scale up gradually, not in one step. 1-2 ticks of slippage on worst-liquidity minutes is acceptable at that size. 50 lots sits at the boundary where participation regularly exceeds 20-30% on a meaningful minority of boundary-minutes (5,770 / 8,594, i.e. ~67%, at ≥20%) — consistent with "acceptable, not free" rather than "negligible," matching the decision.

Not itself a slippage model (no fill data exists yet to calibrate one against) — a starting point for judging how much headroom exists before participation, and therefore expected slippage, becomes uncomfortable. Worth re-cutting against real fill data once trading at meaningful size.

## Position sizing — CRUDEOIL (main contract) liquidity comparison (2026-09-08)

**Context.** The dynamic-sizing simulation above (both the no-slippage and slippage-adjusted runs) sizes purely in CRUDEOILM lots and reaches units the 2026-09-07 analysis already flagged as past comfortable participation. Question: would switching to CRUDEOIL (the main/full-size contract, 100 bbl/lot vs. CRUDEOILM's 10) buy meaningfully more headroom to scale, at least beyond some exposure level? Same method as the 2026-09-07 analysis, extended to CRUDEOIL and cross-compared on a barrel-equivalent basis — a raw lot count means 10x different things on the two contracts, so lots alone aren't a fair comparison.

**Method.** Identical to the CRUDEOILM analysis: participation measured on the 1-min bar at each 15-min mark (:00/:15/:30/:45, 09:15 onward) across the full available history (2026-01-30 to 2026-09-07 — three days further than the original cut). CRUDEOILM: 8,651 boundary-minutes; CRUDEOIL: 8,570 (a handful fewer — gaps in the raw feed, not investigated, immaterial at this sample size). Zero negative-volume rows land on a boundary minute on either contract. Units re-confirmed with a fresh timestamp sample (both print comparable per-minute *lot* counts at matching moments, e.g. 2026-01-30 23:2x: CRUDEOILM 27–92, CRUDEOIL 11–63) — volume is lots on both, consistent with the original check.

**CRUDEOIL's own participation table** (lots, same shape as the CRUDEOILM table above):

| Order size (lots) | Median | p25 | p10 | p5 | Boundary-minutes ≥20% participation |
|---:|---:|---:|---:|---:|---:|
| 1 | 2.08% | 0.93% | 0.48% | 0.33% | 487 / 8,570 |
| 10 | 20.83% | 9.35% | 4.78% | 3.26% | 4,412 |
| 20 | 41.67% | 18.69% | 9.57% | 6.52% | 6,265 |
| 50 | 104.17% | 46.73% | 23.92% | 16.31% | 7,934 |
| 100 | 208.33% | 93.46% | 47.85% | 32.62% | 8,401 |
| 200 | 416.67% | 186.92% | 95.69% | 65.24% | 8,528 |

Median boundary-minute volume is 48 lots (vs. CRUDEOILM's 153) — CRUDEOIL trades roughly a third as many *lots*, unsurprising for a contract with 10x the lot size and (presumably) a smaller retail base. Read alone, this table looks worse than CRUDEOILM's — it isn't, once lots are converted to the exposure they actually represent.

**Barrels, not lots, are the fair comparison unit.** Re-expressed at matched underlying exposure (a CRUDEOILM lot is 10 bbl, a CRUDEOIL lot is 100 bbl):

| Barrels | CRUDEOILM lots | CRUDEOILM median part. | CRUDEOIL lots | CRUDEOIL median part. |
|---:|---:|---:|---:|---:|
| 10 | 1 | 0.65% | 0.10 | 0.21% |
| 100 | 10 | 6.54% | 1.00 | 2.08% |
| 200 | 20 | 13.07% | 2.00 | 4.17% |
| 500 | 50 | 32.68% | 5.00 | 10.42% |
| 1,000 | 100 | 65.36% | 10.00 | 20.83% |
| 2,000 | 200 | 130.72% | 20.00 | 41.67% |

CRUDEOIL's participation runs at roughly **a third** of CRUDEOILM's for the same barrel exposure, consistently — checked at the tails too, not just the median: at 1,000 bbl, CRUDEOILM's p10/p5 are 16.81%/11.90% against CRUDEOIL's 4.78%/3.26%, the same ~3x gap. This isn't lots being re-sliced — CRUDEOIL's real underlying pool is deeper: median boundary-minute volume in barrel terms is 4,800 bbl (CRUDEOIL) vs. 1,530 bbl (CRUDEOILM), a 3.14x ratio, and that ratio is what drives every barrel-equivalent comparison above.

**Where the "1-2 tick" crossover sits, in barrels.** The 2026-09-07 analysis put CRUDEOILM's crossover into the "expect to reliably cross the spread, walk 1-2 ticks" 20-30%-participation band at roughly 30-50 lots — 300-500 bbl. CRUDEOIL's median participation crosses that same band around 10 lots — 1,000 bbl — call it 2-3x the barrel exposure before hitting the same qualitative slippage zone, consistent with the 3.14x pool-size ratio. Per-barrel, a tick of slippage costs the same on either contract (both quote the same underlying commodity price, tick size 1.0 on both) — what changes is how much barrel exposure a given participation band tolerates, not the cost of a tick itself. No calibrated ₹ coefficient here either, for the same reason the 2026-09-07 analysis declined one — this is a real-volume-grounded qualitative read, not a fitted model.

**Checked against where the simulation already is.** *(Peak-units figures refreshed 2026-09-11 for the dynamic `MARGIN_PER_UNIT` sizing formula — margin per unit is now computed per trade from that trade's own price rather than a frozen constant, so the peak reflects the new formula, not new price data. Participation percentages rescaled linearly from the 2026-09-08 base run rather than re-pulled from raw volume data, which is exact for this purpose: participation_pct = order_size / that_minute's_volume × 100 is linear in order_size for any fixed minute, so every quantile of the distribution — median, p10, p5 — scales by the same ratio as the lot count itself. No approximation, just algebra. The CRUDEOILM-to-CRUDEOIL participation ratio (~3.14x) is a property of the two markets' relative liquidity, unaffected by the sizing formula, so it carries over unchanged.)* The dynamic-sizing runs above now reach peak units of 226 (no-slippage, down from 308 under the old frozen-constant formula) and 158 (slippage-adjusted, anchor coefficient, down from 204) — 452 and 316 CRUDEOILM lots respectively (2 lots/unit), i.e. 4,520 bbl and 3,160 bbl of exposure:

| Simulation | CRUDEOILM lots | CRUDEOILM median part. | CRUDEOIL-equivalent lots | CRUDEOIL median part. |
|---|---:|---:|---:|---:|
| No-slippage peak (226 units) | 452 | 295.4% | 45.2 | 94.1% |
| Slippage-adjusted peak (158 units) | 316 | 206.5% | 31.6 | 65.8% |

CRUDEOIL is a consistent ~3.1x better at both points — but neither point is actually *comfortable* on either contract at this exposure; CRUDEOIL just pushes the same problem out roughly 3x in barrel terms, it doesn't remove it. The genuinely comfortable (<20-30%) CRUDEOIL zone tops out around 1,000 bbl (≈10 CRUDEOIL lots ≈ 100 CRUDEOILM-lot-equivalent) — both simulations' sizing has already run well past that by the time units reach the 150-250 range.

**Freeze-limit parity.** MCX's `freeze_qty` is 10,000 underlying units for both contracts — 1,000-lot ceiling on CRUDEOILM, 100-lot ceiling on CRUDEOIL, identical in barrel terms (10,000 bbl either way). Order-splitting risk kicks in at the same total exposure regardless of which contract carries it.

**Reading this.** CRUDEOIL offers meaningfully more room to scale the *same* capital-equivalent exposure than CRUDEOILM does — roughly 3x, consistently, across the whole size range and both tails checked — but "more room" isn't "unlimited room": past ~1,000 bbl (~10 CRUDEOIL lots), the same qualitative slippage concerns reappear, just later. Whether switching (or splitting exposure across both contracts) is actually worth it also depends on the strategy's own edge holding up equally well on CRUDEOIL — already cross-validated 2026-09-07, re-validated 2026-09-09 at T1=2.2%, refreshed again 2026-09-12 through 2026-09-11's data under the per-trade drawdown methodology — above, with Calmar running lower there (9.58-9.8 vs. CRUDEOILM's 11.56-15.03). The margin-per-lot side is no longer an open question: the `/3` divisor behind §26's dynamic `MARGIN_PER_UNIT` formula (`entry_price × LOT_SIZE / 3 × 4`) was checked by the user against both instruments' own real margin requirements, not derived from CRUDEOILM alone — see `prometheus_production/README.md`'s §26 and `prometheus_configs.py`'s `MARGIN_CONTRACT_VALUE_DIVISOR` comment.

**Not done here, by design** (this pass was scoped to liquidity/slippage only): no equity curve, drawdown, or trade-performance re-simulation using CRUDEOIL-based sizing — queued as the next step.

**Follow-up, done 2026-09-08**: the equity curve / drawdown / trade-performance re-simulation queued above is now done — see the CRUDEOIL dynamic-sizing simulation below, in its own artifact.

## Dynamic-sizing equity simulation — CRUDEOIL (main contract) (2026-09-08, sizing formula updated 2026-09-11)

Same question as the CRUDEOILM dynamic-sizing simulation earlier in this README, asked of the main contract: what if Prometheus had gone live on 2026-01-30 with `DYNAMIC_SIZING=True` on CRUDEOIL instead, using starting capital Rs 55,00,000 (user-supplied). Same live production combo (mult 2.0, SL 2.2%/T1 2.2%/T2 5.0% — T1 updated 2026-09-09, see Phase 3 caveat #1), run against CRUDEOIL's own 410-trade backtest (`phase3_crudeoil/data_sweep/mult_2.0/bespoke_trade_summary.csv`). Same per-trade equity/drawdown methodology (see "Drawdown methodology: per-trade, not per-lot-exit" above), and both a no-slippage run and a slippage-adjusted run using the identical participation model from the CRUDEOIL liquidity comparison above (same A=0.3 anchor, carried over rather than re-fit, since a tick costs the same Rs/barrel on either contract).

**Sizing formula updated 2026-09-11** (§26 of `plans/prometheus-phase3-production.md`): margin per unit is no longer the flat `MARGIN_PER_UNIT`=Rs 10,00,000 constant — it's now recomputed PER TRADE from that trade's own `entry_price` (`entry_price × LOT_SIZE(100) / 3 × 4`, mirroring production's `Prometheus._calculate_margin_per_unit()`), ranging Rs 7,45,600–Rs 14,30,667 over the backtest window. The old constant happened to sit close to the window-start price, so this is a genuine sizing-methodology change, not a data refresh — headline figures below moved for that reason, not because new price data arrived.

**No-slippage result** (refreshed 2026-09-12 through 2026-09-11's data): 410 trades, Rs 55L → Rs 2.26Cr (+310.4%), max drawdown −16.0%, Calmar 19.39. Units start at 6, dip to a low of 4 during the early-March drawdown, and recover to a peak of 21 — a far more modest range than CRUDEOILM's 40→226, because CRUDEOIL's ~10x-larger per-unit margin means the same rupee P&L moves units far less.

**Slippage-adjusted result** (same feedback-loop mechanics as the CRUDEOILM slippage run — units resized from post-slippage capital every trade, and margin per unit itself computed from the same per-trade formula): final capital Rs 1.83Cr (+231.8%), max drawdown −19.0%, Calmar 12.17, peak units damped from 21 to 18. Coefficient sensitivity (0.5x/1x/2x anchor): Calmar 15.81 → 12.17 → 7.24.

[Chart + table (both runs, comparison charts, sensitivity table)](https://claude.ai/code/artifact/704b21e1-1343-489b-8793-7d19240279ef) — structured identically to the CRUDEOILM artifact, rebuilt 2026-09-11 for the new sizing formula. Scripts: `phase3_crudeoil/dynamic_sizing_sim.py` and `phase3_crudeoil/dynamic_sizing_sim_slippage.py`, both committed. Detailed CSVs (`dynamic_sizing_trades.csv`, `dynamic_sizing_equity_curve.csv`, and their `_slippage` counterparts, now each carrying a per-trade `margin_per_unit_rs` column) in `phase3_crudeoil/data_sweep/mult_2.0/` (gitignored, run the scripts to regenerate).

## Risk of Ruin at 50-unit sizing (2026-09-08)

**Why 40% drawdown is the ruin threshold, not an arbitrary number.** MCX's actual required margin for CRUDEOILM is ₹50,000/unit. `MARGIN_PER_UNIT` in `prometheus_configs.py` is set to ₹1,00,000 — double the raw requirement — by design: the user allocates capital per unit so that a 40% drawdown plus a further 10% negative MTM swing (50 percentage points of adverse capital use, together) can be absorbed without ever touching the raw margin itself (`raw_margin / (1 − 0.40 − 0.10) = 50,000 / 0.50 = 1,00,000`). So "ruin" at 40% drawdown isn't a round-number risk tolerance pulled from convention — it's the exact point at which the strategy starts eating into the 10%-MTM-swing reserve that sits between the drawdown allowance and an actual margin call. A drawdown beyond 40% that doesn't recover quickly is the scenario the sizing was explicitly built to avoid.

**Method.** Monte Carlo bootstrap over the real backtested trades from Phase 3's live production combo (mult 2.0, `phase3/data_sweep/mult_2.0/bespoke_trade_summary.csv`), resampled with replacement (each trade's `total_pnl_rs` treated as one atomic outcome — lot1+lot2 combined, appropriate for synthesizing new orderings rather than reconstructing the original timeline). This was already the per-trade methodology now used everywhere else in this README too (see "Drawdown methodology: per-trade, not per-lot-exit" in the Phase 3 section above) — resampling independently drawn trades can't preserve a finer lot-level chronology anyway, so this section's own numbers are unaffected by that 2026-09-11 change; confirmed by re-running and getting byte-identical output. Scaled to 50-unit sizing (linear ×50 on each trade's 1-unit P&L). Capital base: ₹50,00,000 (50 units × `MARGIN_PER_UNIT`'s ₹1,00,000 — the fully-buffered allocation per unit, not the raw ₹50,000 margin). 20,000 simulated paths, each ~2 years long at the backtest's own observed trade pace. Ruin defined as: max drawdown > 40% at any point, **and** equity has not recovered back to its pre-drawdown peak by the end of the 2-year horizon.

**Refreshed 2026-09-12 through 2026-09-11's data** (390 trades; `phase3/risk_of_ruin_p3.py`, fixed seed 20260909 for reproducibility — a routine data-only refresh, not a methodology change like the parity-fix refresh that preceded it). Ruin remains vanishingly rare:

**Result: P(ruin) = 0.00%** — 0 of 20,000 simulated paths met the full definition.
- P(max drawdown > 40% at any point): 1.93% (387/20,000 paths).
- Of those 387, every single one recovered within the 2-year horizon — 35.7% within 1 month, 93.3% within 3 months, 100% within 6 months, 100% within a year. None qualified as a "long recovery."
- P(equity ever negative — literal wipeout): 0.00%.
- Max drawdown distribution: p50 16.5%, p90 28.3%, p95 33.2%, p99 44.8% — even the 99th-percentile bad-luck path is only borderline past the 40% mark, not blown through it.
- Median terminal equity after 2 years: ₹3.93 crore, from a ₹50,00,000 base — reflects the combo's edge (win rate 45.4%, avg win ₹3,356 vs. avg loss ₹1,805 per unit at 1-unit sizing), which is exactly why ruin is this rare in the simulation.

**Why this "0%" shouldn't be read as a guarantee.**
- Bootstrap resampling treats the trade sample as a fixed, stationary distribution — it cannot model the edge decaying or the strategy meeting a genuinely different regime than the backtest window produced.
- No slippage or execution cost is modeled here, consistent with every backtest phase (`SLIPPAGE_ENABLED=False` throughout) — but the volume/participation analysis above already found 50 lots sees ~33% median participation at the moments Prometheus fills, so real P&L at this size runs somewhat worse than a clean ×50 linear scale-up assumes.
- Resampling destroys whatever serial correlation the real historical sequence has (trending periods clustering, say) — trades are treated as independent draws, which is unlikely to be exactly true of the real market.
- The capital-base assumption (bare `MARGIN_PER_UNIT` × units, no further buffer beyond what's already built into that constant) is the single biggest lever on this number.

Checked in as `phase3/risk_of_ruin_p3.py` (2026-09-09, was ad-hoc before that) — re-run if the underlying trade sample changes materially (a re-calibration, a longer backtest window, or once real fill/slippage data exists to replace the linear-scaling assumption).

### CRUDEOIL companion analysis (2026-09-09, refreshed 2026-09-12 through 2026-09-11's data)

Same method (`phase3_crudeoil/risk_of_ruin_p3.py`), run against CRUDEOIL's own 410-trade `bespoke_trade_summary.csv`. **Sizing scale differs from CRUDEOILM's**: CRUDEOILM's 50 units came from a specific real decision ("user's current capital supports scaling to 50 lots," Position sizing section above); there's no equivalent documented decision for CRUDEOIL. This run uses 5 units (Rs 50,00,000 capital, `MARGIN_PER_UNIT`=Rs 10,00,000) — matching CRUDEOIL's own dynamic-sizing simulation's actual starting scale, not a stated capital-allocation decision. If a different CRUDEOIL scale is actually intended, this is a judgment call worth revisiting.

**Result: P(ruin) = 0.00%** (0 of 20,000 paths):
- P(max drawdown > 40% at any point): 3.12% (624/20,000 paths) — still meaningfully higher than CRUDEOILM's 1.93%, consistent with CRUDEOIL's own lower Calmar (9.8 vs. CRUDEOILM's 15.03) and proportionally deeper drawdowns already found in the cross-validation above.
- Of those 624, all recovered within the 2-year horizon (100%, matching CRUDEOILM) — 29.5% within 1 month, 88.9% within 3 months, 99.5% within 6 months, 100% within a year.
- P(equity ever negative): 0.00%.
- Max drawdown distribution: p50 17.7%, p90 30.7%, p95 36.2%, p99 49.4% — the p95/p99 tail still sits meaningfully past the 40% ruin threshold, unlike CRUDEOILM's (28.3%/33.2%-region tail that stayed borderline). CRUDEOIL's ruin risk remains structurally higher than CRUDEOILM's at this sizing, not just a smaller version of CRUDEOILM's result.
- Median terminal equity: Rs 3.71 crore, from a Rs 50,00,000 base (vs. CRUDEOILM's Rs 3.93 crore at 10x the margin-per-unit — not a like-for-like capital-efficiency comparison, since the unit scales themselves were chosen differently, per the sizing-scale note above).

## Side project: WTI 5-minute approximation (2026-09-08)

`prometheus_backtest/side_wti_5m/` — tests the already-decided Phase 3 combo(s) unchanged against 14.6 years of WTI crude 5-min data (Kaggle), as a loose cross-market sanity check. **Not a validation like `phase3_crudeoil/`** — different exchange, different data source/quality, kept deliberately out of the numbered phases and out of the production go-live decision chain. **Result: the edge does not clearly transfer** (Calmar 0.05 / −0.29 vs. CRUDEOILM's own 10.21/10.78 at the time this side project ran — mult 2.0's T1 has since changed to 2.2%, not re-tested here) — full methodology, the real data-quality finding behind it (weekend synthetic-fill contamination), and caveats in `side_wti_5m/README.md`.

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

Symbol switch: `SYMBOL` in `configs.py` / `configs_p2.py` / `configs_p3.py` — `'CRUDEOILM'` (default, primary calibration target) or `'CRUDEOIL'` (cross-validation, full-size contract). Phase 3's CRUDEOIL cross-validation (2026-09-07) lives in its own sibling folder, `prometheus_backtest/phase3_crudeoil/` — an exact copy of `phase3/`'s pipeline with only `configs_p3.py`'s `SYMBOL` changed, rather than overwriting `phase3/`'s own CRUDEOILM output by flipping the constant in place. See Phase 3's own section above for results.

All generated output (`data/`, `data_sweep/`, per-trade logs) is gitignored — every number in this README was verified against a fresh run of the current code, not carried over from memory of an earlier session.
