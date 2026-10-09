# Plan: early-MFE exit across the four live engines

**Status (2026-10-09): Phases A to D done as a first look, result negative. No early-exit rule is adopted for any engine, and nothing is built in production.** Two weak, era-dependent Prometheus cells are recorded (below) and not pursued. Originated 2026-09-08 as `prometheus-mfe-early-exit.md` (a live Prometheus trade stalled at about 2 points of MFE and the user asked whether that was a leading indicator); renamed and reworked 2026-10-09 after the multiplier change (Prometheus now runs ST 2.5 with SL 1.0 / T1 1.25 / T2 4.0) and extended to Selene, Helios and Typhon. Code `research/early_mfe/` (`early_configs.py`, `early_mfe_study.py`, `robustness.py`, small outputs in `outputs/`), tests `tests/test_early_mfe_study.py`. The 2026-09-08 write-up is kept at the bottom as a dated record.

---

## Verdict

| Engine (live config) | Does early MFE add anything beyond current unrealised P&L? | Does an early-exit rule beat holding? |
|---|---|---|
| Selene (2.5, SL 3.0%) | No (semi-partial 0.06 at 60 min, 0.09 at 120, 0.01 at 240) | No. Best cell (exit if underwater at 120 min) lifts Calmar 13.31 to 14.18 but gives up 42 points of total return and loses the 2026 half (6.37 vs 6.64) |
| Helios (3.5, SL 1.6%) | No (0.07, 0.02, -0.01) | No. No cell beats the baseline Calmar of 17.97 (best 17.87 with total 120.9% against 184.2%) |
| Typhon (3.0, SL 0.8%, target 15%) | No (-0.06, -0.03, -0.06) | No. Best cells beat 6.72 by 0.1 on 26 or fewer triggered trades, noise |
| Prometheus (2.5, SL 1.0 / 1.25 / 4.0) | No (-0.00, 0.03, 0.04 on the long history) | Not robustly. One cell per rule family looks good in one era and reverses in another (below) |

The thing the 2026-09-08 plan was built on, that MFE adds a separate signal from 60 minutes onward (semi-partial 0.10 to 0.12), **does not reproduce at the live config on any engine**. The headline correlation is real and rises with the checkpoint on every engine (corr(MFE, outcome) 0.28 to 0.38 at 60 minutes, 0.5 to 0.76 by 240 to 480) but current unrealised P&L explains the same ground: corr(U, outcome) is 0.36 to 0.46 at 60 minutes, always at least as high. A time-stop on unrealised P&L is therefore the only rule worth testing, and it fails the split tests.

---

## What changed since 2026-09-08 and whether it matters

- **The live config changed twice**: Prometheus went from mult 2.0 (SL 2.2 / T1 2.2 / T2 5.0) to mult 2.5 (SL 1.0 / T1 1.25 / T2 4.0) on 2026-10-05, and the 2026-09-08 sample (381 trades, mult 2.0, Angel One data) was recomputed under the 2026-09-19 contract-rollover fix and the 2026-10-06 Fyers void fill.
- **Attribution, tested rather than assumed**: the same study run on mult 2.0 with today's data (the `mult 2.0 reference` tracks, R = 2.2%) gives semi-partial 0.03 / 0.02 / -0.03 at 60 / 120 / 240 minutes on the 2,531-trade Fyers-history track and 0.04 / 0.04 / -0.06 on the 454-trade Angel One 2026 track, against 0.10 / 0.12 / 0.12 on 2026-09-08. So the multiplier change did not remove the signal: the original number did not survive the rollover fix and the filled data. Treat the 2026-09-08 partial correlation as a data-vintage result.
- **Samples used**: Selene 2,487 trades (2021-04 to 2026-09), Helios 1,174 (2021-10 to 2026-09), Typhon 1,403 (2023-04 to 2026-09), all at their decided configs through each engine's own parity simulator (baselines reproduce the saved results: 322.8%, 184.2%, 138.5% of entry, summed per lot). Prometheus: 1,871 trades on the Fyers-primary track (2023-03 to 2026-09-18), the 340-trade Angel One 2026 track and the 337-trade CRUDEOIL cross-check. The Fyers 2026 stretch and the Angel One 2026 track cover the same months, so they are one period seen through two data sources, not two confirmations.

---

## Method

- **Units**: every result is percent of entry price per lot (Prometheus: the two lots averaged), cumulated in entry order, so engines and price eras compare. Calmar here is cumulative-percent over max drawdown and is not the rupee Calmar of the README tables (the same 340 Prometheus trades read 6.89 here and 8.09 there: rupee-weighted per-lot-exit events against price-normalised per-trade results).
- **Checkpoints** in trading minutes since entry (bars, so an overnight hold does not inflate them): 15, 30, 60, 120, 240, 480. A trade whose decided exit comes before the checkpoint is excluded from that checkpoint's cohort (causal, no lookahead). MFE and MAE use only the bars before the checkpoint; U is the price at the open of the checkpoint bar, which is also the fill a rule would get.
- **Semi-partial correlation**: corr(MFE with U's linear effect removed, outcome). The outcome is not residualised, which is the same definition the 2026-09-08 study used, so the two are comparable (it is not the full partial correlation).
- **First-look rules** (the old plan's rule families 1 and 2): R1 exit both lots (or the single lot) if U <= 0 at checkpoint T; R2 exit if MFE < q x R at T, where R is the engine's own stop distance in percent (q = 0.1, 0.25, 0.5) so the threshold scales across silver, gold, gas and crude. T in {60, 120, 240}: 12 cells per engine. Prometheus applies the rule to the lots still open (a lot already booked at its target keeps its result).
- **Evaluation**: total and max drawdown and Calmar against the engine's own baseline, before and after 2026-01-01 (the split every engine's calibration uses), recovered-winner share, and for the candidates a calendar-year split, a split inside 2026 at 2026-06-01 (the 2026-only tracks have no pre-2026 half), and the extra cost per triggered exit at which the gain disappears.

---

## Results

**Descriptive (semi-partial correlation of MFE with outcome, beyond unrealised P&L)**

| Track | 60 min | 120 min | 240 min | Low / high MFE tercile win rate at 60 min |
|---|---:|---:|---:|---|
| Selene | 0.063 | 0.087 | 0.011 | 27.6% / 54.0% |
| Helios | 0.072 | 0.015 | -0.009 | 31.1% / 47.9% |
| Typhon | -0.057 | -0.029 | -0.056 | 29.9% / 48.7% |
| Prometheus 2.5, Fyers history | -0.001 | 0.031 | 0.040 | 27.0% / 62.1% |
| Prometheus 2.5, Angel One 2026 | -0.050 | 0.015 | -0.031 | 34.0% / 78.4% |
| Prometheus 2.5, CRUDEOIL | -0.065 | 0.027 | 0.000 | 36.8% / 80.0% |
| Prometheus 2.0 reference, Fyers history | 0.027 | 0.023 | -0.025 | 25.8% / 58.4% |
| (2026-09-08, mult 2.0, original) | 0.10 | 0.12 | 0.12 | 27% / 68% |

The bottom MFE tercile is net-negative in mean outcome at 60 and 120 minutes on Selene, Typhon and Prometheus, flat on Helios, and turns positive at 480 minutes (survivorship: only long, winning trades are still open). **The MAE split** (low-MFE trades at 60 minutes, median split on MAE) now separates a little better than in 2026-09-08: "going wrong" trades win less than "chopping near entry" on every engine (Selene 26.1% vs 29.2%, Helios 29.0% vs 33.2%, Typhon 25.9% vs 34.0%, Prometheus 23.0% vs 30.9% on the long history), but both groups are still mostly losers and the gap is not a basis for a rule on its own.

**Rule first look, best cell per engine against its baseline** (full table in `outputs/rules.csv`): Selene U<=0 at 120 min Calmar 14.18 against 13.31, total 281.5% against 322.8%, after-2026 6.37 against 6.64; Helios best 17.87 against 17.97; Typhon best 6.83 against 6.72 on 26 triggered trades and 6.59 on 197; Prometheus see below. Across the 12 cells per single-lot engine, exactly one beats its baseline on Calmar in both calendar halves with total at least equal: Typhon's MFE below 0.08% at 240 minutes, which triggers on 26 trades, adds 2.3 points of total over 1,403 trades and moves the after-2026 half from -0.02 to 0.00. That is noise, not a rule.

**Prometheus candidates, tested hard** (`outputs/robustness.txt`; gain = result with the rule minus result without, in percent of entry per lot over the track):

| Cell | Fyers history 2023 to 2026-09 | Inside 2026 (Fyers track) | Angel One 2026 track | CRUDEOIL 2026 | Break-even cost per exit |
|---|---|---|---|---|---|
| U<=0 at 60 min (the old plan's rule 1) | -16.3 (by year 2023 -0.1, 2024 -16.7, 2025 +0.4, 2026 +0.1) | H1 +10.1, after 1 June -10.0 | +4.9 (H1 +14.5, after 1 June -9.6) | +3.6 (H1 +14.4, after 1 June -10.7) | negative on the long history, 0.03 to 0.04% on the 2026 tracks |
| MFE < 0.5% (half the stop) at 240 min | +16.8 (2023 +8.5, 2024 +8.8, 2025 -0.4, 2026 -0.1) | H1 +5.3, after 1 June -5.3 | +0.5 | +1.7 | 0.05% on the long history, 0.02 to 0.08% on 2026 |

- **The 60-minute underwater rule only "passes" on 2026 data, and only in the first half of 2026**: positive on all three 2026 views up to 1 June (47 to 65 triggers) and negative after it on all three, and it loses 16 points over 2023 to 2026 (all of it 2024). The three 2026 views are one period, not three confirmations.
- **The 240-minute half-stop rule earns its gain in 2023 and 2024 and nothing in 2025 or 2026**; its neighbours (0.35% and 0.7% thresholds, 180 and 300 minutes) are mostly negative in 2026, and the gain is gone at 0.05% extra cost per exit (0.02% keeps about 60% of it). At the user's original trigger scale (MFE below about 0.1% at 60 minutes) the exit earns -0.258 per trade against -0.253 for holding: indistinguishable.
- **Reading**: regime-dependent in both directions, the same character the earlier Fyers-track regime work found for the strategy itself. Neither cell is a candidate to build; both are the "plausible story, fails the time split" shape Phase 4 already documented. One plausible reason, not tested, is that the 1.0% stop already does the early cutting these rules are after.

---

## Decision and what would reopen it

- **Shelved.** No early-exit rule for any of the four engines. Nothing to change in `prometheus_engine`, `selene_engine`, `helios_engine` or `typhon_engine`.
- **Phase 0 (live MFE in the Slack trade update) is not worth building.** The 2026-09-08 plan wanted it as an early-warning signal, but MFE carries no information beyond unrealised P&L, which each engine's existing periodic `#trade-updates` message (`_maybe_send_trade_update`, now in Hestia's engines, not `prometheus_production/prometheus.py`) already shows.
- **Phase B (`prometheus_backtest/phase5/`) is replaced** by `research/early_mfe/`, which reads every engine's own simulator and the Prometheus trade logs without touching a decided-config folder.
- **Reopen if** a longer live or backtest sample shows a semi-partial correlation near 0.10 or higher on an engine at 60 to 240 minutes in both halves, or if the regime work yields a volatility gate under which an early-exit rule holds in both eras. A rule that only works in one era is not enough, and the exit cost (a market order at a non-boundary minute) must be below the cell's break-even.
- **Open housekeeping from 2026-09-08, still open**: `trade_logs/` directories carry stale duplicate files for earlier runs (the mult-2.5 folders hold 650 files for CRUDEOIL's 337 trades and 1,132 for CRUDEOILM's 340); this study matches by trade id, entry time and direction so it is unaffected, but a future analysis must do the same, or the directories should be cleaned.

---

## Historical record: the 2026-09-08 finding (mult 2.0, SL 2.2%, 381 trades) — kept as written, superseded by the section above

**The finding, restated precisely.** Across the 381 mult-2.0 CRUDEOILM bespoke trades, running MFE measured at fixed early checkpoints (15/30/60/120/240 min since entry, causal — a trade already closed before a checkpoint is excluded from that checkpoint's cohort, no lookahead) correlates with the trade's eventual `total_pnl_rs`: +0.25 at 15min rising to +0.40 at 240min. The bottom MFE tercile is net-negative in mean P&L at every checkpoint; trades with ≤2 points of MFE within 60min (n=21, the live trigger case) went on to a 33.3% win rate and −₹652 mean P&L vs. 46.1%/+₹576 for the rest.

**Trade-path file-matching verified, 2026-09-08 (Fable's review flagged this before anything else):** `trade_logs/` holds 753 files for 381 unique trade IDs — 370 of them duplicated, a stale pre-data-refresh backtest run's per-trade path CSVs sitting alongside the current run's under the same trade_id but different entry timestamps (e.g. trade #6 has both a 2026-02-01 09:15 file and a 2026-02-02 19:45 file). The original ad-hoc analysis matched by trade_id alone, which could have silently read the wrong trade's path for any of those 370. Re-ran with entry_ts+direction keyed matching instead (753 files → 385 unique keys, 381/381 bespoke trades matched, zero unmatched) — **results were numerically identical to the original pass**, so the README table stands uncorrected. Root cause of the duplication (why the stale run's files were never cleaned up) not investigated — harmless here since the two runs' content coincided for every matched trade, but the underlying `trade_logs/` directory is carrying dead files and a future analysis over a data range where the refresh actually changed things would not be so lucky. Worth a cleanup pass independent of this plan.

**The confound Fable's review said matters more than the headline number: does MFE add anything beyond current unrealised P&L at the same checkpoint?** Low MFE at a given checkpoint means the trade is at-or-below entry *by construction* — the interesting question is whether MFE (peak favorable excursion so far) predicts outcome beyond what "is this trade currently winning or losing" already tells you. Checked via partial correlation (MFE residualized against unrealised-P&L-at-checkpoint, then correlated with final outcome):

| Checkpoint | corr(unrealised P&L, outcome) | partial corr(MFE \| unrealised P&L, outcome) |
|---|---:|---:|
| 15 min | +0.32 | +0.02 |
| 30 min | +0.42 | +0.04 |
| 60 min | +0.42 | +0.10 |
| 120 min | +0.41 | +0.12 |
| 240 min | +0.40 | +0.12 |

**Reading this honestly: at 15-30min, MFE is telling you almost nothing beyond "is the trade currently profitable" — the marginal signal is close to zero.** It only becomes a real, separate signal from 60min onward, and even there it's modest (partial r ≈ 0.10-0.12), not the dominant driver current-P&L already is. This matters for what gets built: a rule keyed on current unrealised P&L alone (a plain time-stop) would already capture most of what a full MFE-based rule captures, and is simpler to build, explain, and reason about. MFE earns its place in a rule only past ~60min, and only as an addition on top of a P&L-based trigger, not a replacement for one.

**Secondary check: does MAE split the low-MFE cohort into "chopping near entry" vs. "going wrong"?** Within the low-MFE-at-60min tercile (n=123), split by concurrent MAE (median split): low-MFE+low-MAE ("chop", n=62) win rate 27.4%, mean P&L −₹530; low-MFE+high-MAE ("going wrong", n=61) win rate 26.2%, mean P&L −₹880. **Both regimes are bad, at nearly identical win rates** — MAE adds some information about how bad the eventual loss runs (worse mean P&L in the high-MAE half) but doesn't cleanly separate "safe to hold" from "should exit" the way a hoped-for chop/wrong split would. Treat this as a mild negative result, not a reason to build an MAE-gated rule family.

**The framing that decides whether any of this is worth building, stated up front because it's the reason Phase 4 got shelved despite a plausible-sounding story too:** the descriptive finding shows *early MFE (and, more precisely, current unrealised P&L) predicts outcome*. A rule is only worth having if *acting on it* — exiting or tightening at the checkpoint — beats what those trades would have realised by just staying in, net of the ~25-30% of low-signal trades that go on to recover. Correlation is not the bar. Calmar delta, out-of-sample, is the bar.

---

---

## The original 2026-09-08 plan, superseded

For the record, the phases as first written: **Phase 0** add MFE-since-entry and minutes-since-entry to the Slack trade update (display only); **Phase A** pick one checkpoint (60 minutes) and run the three checks on CRUDEOIL's 398 trades; **Phase B** build `prometheus_backtest/phase5/` on `phase4/`'s template with a configs file and a sweep runner; **Phase C** a rule ladder: (1) exit if unrealised P&L is still <= 0 at 60 minutes, (2) exit if MFE is below X% at 60 minutes, (3) tighten the stop instead of exiting, (4) act on one lot only, (5) alert only; **Phase D** evaluate on per-lot-exit Calmar against the baseline on both contracts, a time split, a smoothness check, a bootstrap interval and a slippage overlay, shelving if no cell beats the baseline on both contracts; **Phase E** a flagged production build only if something survived. As executed 2026-10-09: A done for all four engines, B replaced, C rules 1 and 2 first-looked (3 and 4 not needed once 1 and 2 failed), D applied through the split, cost and neighbour checks to the two Prometheus candidates, E not reached.
