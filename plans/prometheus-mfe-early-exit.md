# Plan: Prometheus — early-MFE signal into a testable optimization

**Status: Phase 0 not yet started. This is a research plan, not a decision record — nothing below
is built.** Originated 2026-09-08 from watching a live trade stall at ~2 points of MFE and asking
whether that was a leading indicator. `prometheus_backtest/README.md`'s Phase 3 "Supporting
analysis" section has the descriptive finding this plan builds on; this file is where the
follow-through work lives, per Fable's advisor review of the finding before any grid gets built.

---

## Context

**The finding, restated precisely.** Across the 381 mult-2.0 CRUDEOILM bespoke trades, running MFE
measured at fixed early checkpoints (15/30/60/120/240 min since entry, causal — a trade already
closed before a checkpoint is excluded from that checkpoint's cohort, no lookahead) correlates with
the trade's eventual `total_pnl_rs`: +0.25 at 15min rising to +0.40 at 240min. The bottom MFE
tercile is net-negative in mean P&L at every checkpoint; trades with ≤2 points of MFE within 60min
(n=21, the live trigger case) went on to a 33.3% win rate and −₹652 mean P&L vs. 46.1%/+₹576 for
the rest.

**Trade-path file-matching verified, 2026-09-08 (Fable's review flagged this before anything else):**
`trade_logs/` holds 753 files for 381 unique trade IDs — 370 of them duplicated, a stale
pre-data-refresh backtest run's per-trade path CSVs sitting alongside the current run's under the
same trade_id but different entry timestamps (e.g. trade #6 has both a 2026-02-01 09:15 file and a
2026-02-02 19:45 file). The original ad-hoc analysis matched by trade_id alone, which could have
silently read the wrong trade's path for any of those 370. Re-ran with entry_ts+direction keyed
matching instead (753 files → 385 unique keys, 381/381 bespoke trades matched, zero unmatched) —
**results were numerically identical to the original pass**, so the README table stands uncorrected.
Root cause of the duplication (why the stale run's files were never cleaned up) not investigated —
harmless here since the two runs' content coincided for every matched trade, but the underlying
`trade_logs/` directory is carrying dead files and a future analysis over a data range where the
refresh actually changed things would not be so lucky. Worth a cleanup pass independent of this plan.

**The confound Fable's review said matters more than the headline number: does MFE add anything
beyond current unrealised P&L at the same checkpoint?** Low MFE at a given checkpoint means the
trade is at-or-below entry *by construction* — the interesting question is whether MFE (peak
favorable excursion so far) predicts outcome beyond what "is this trade currently winning or
losing" already tells you. Checked via partial correlation (MFE residualized against
unrealised-P&L-at-checkpoint, then correlated with final outcome):

| Checkpoint | corr(unrealised P&L, outcome) | partial corr(MFE \| unrealised P&L, outcome) |
|---|---:|---:|
| 15 min | +0.32 | +0.02 |
| 30 min | +0.42 | +0.04 |
| 60 min | +0.42 | +0.10 |
| 120 min | +0.41 | +0.12 |
| 240 min | +0.40 | +0.12 |

**Reading this honestly: at 15-30min, MFE is telling you almost nothing beyond "is the trade
currently profitable" — the marginal signal is close to zero.** It only becomes a real, separate
signal from 60min onward, and even there it's modest (partial r ≈ 0.10-0.12), not the dominant
driver current-P&L already is. This matters for what gets built: a rule keyed on current unrealised
P&L alone (a plain time-stop) would already capture most of what a full MFE-based rule captures,
and is simpler to build, explain, and reason about. MFE earns its place in a rule only past ~60min,
and only as an addition on top of a P&L-based trigger, not a replacement for one.

**Secondary check: does MAE split the low-MFE cohort into "chopping near entry" vs. "going
wrong"?** Within the low-MFE-at-60min tercile (n=123), split by concurrent MAE (median split):
low-MFE+low-MAE ("chop", n=62) win rate 27.4%, mean P&L −₹530; low-MFE+high-MAE ("going wrong",
n=61) win rate 26.2%, mean P&L −₹880. **Both regimes are bad, at nearly identical win rates** — MAE
adds some information about how bad the eventual loss runs (worse mean P&L in the high-MAE half)
but doesn't cleanly separate "safe to hold" from "should exit" the way a hoped-for chop/wrong split
would. Treat this as a mild negative result, not a reason to build an MAE-gated rule family.

**The framing that decides whether any of this is worth building, stated up front because it's the
reason Phase 4 got shelved despite a plausible-sounding story too:** the descriptive finding shows
*early MFE (and, more precisely, current unrealised P&L) predicts outcome*. A rule is only worth
having if *acting on it* — exiting or tightening at the checkpoint — beats what those trades would
have realised by just staying in, net of the ~25-30% of low-signal trades that go on to recover.
Correlation is not the bar. Calmar delta, out-of-sample, is the bar.

---

## Phase 0 — Live monitoring, no backtest needed, do this first

Production's per-trade running log already carries `running_mfe` (same columns as the backtest's
own per-trade path logs, per `trade_paths_p3.py`'s docstring). Add MFE-since-entry and
minutes-since-entry to the periodic Slack trade-update (`_send_trade_update`,
`prometheus_production/prometheus.py`) — a small, additive change, not a new mechanism. This:

- Gives the live early-warning signal the user asked for originally, immediately, before any
  backtest work is done.
- Starts accumulating live checkpoint data (real fills, real slippage, real operator behavior)
  that the backtest-only analysis above can never fully substitute for.
- Carries zero strategy risk — display-only, no exit logic changes.

**Not started. Do this whenever, independent of everything below — it doesn't block or depend on
Phases A-E.**

---

## Phase A — Finish the descriptive pass before touching infrastructure

Two items from the confound analysis above are already done (partial correlation vs. current P&L;
the MAE chop/wrong split) and folded into Context. What's left before grid-building is justified:

1. **Pick one checkpoint to standardize the rule grid on.** 60min is the natural candidate — it's
   the earliest point where MFE's partial signal (beyond current P&L) becomes non-trivial (+0.10),
   without waiting so long that most of the trade's move has already happened (median hold time is
   ~6h raw / somewhat less for bespoke exits, so 60min is still genuinely early). State this choice
   explicitly in Phase B rather than grid-searching the checkpoint itself as a free parameter —
   that's an extra dimension the 381-trade sample can't support without overfitting.
2. **Run the same three checks (headline correlation, partial-vs-current-P&L, MAE split) against
   CRUDEOIL's own 398 mult-2.0 trades** before assuming the signal generalizes. Not yet done. If the
   partial-correlation pattern doesn't hold on CRUDEOIL (e.g. MFE's marginal signal is flat/zero
   even at 240min there), that's evidence the CRUDEOILM finding is somewhat data-specific, and the
   bar for Phase D's cross-contract gate should be read accordingly.

---

## Phase B — Infrastructure: `prometheus_backtest/phase5/`

Follow `phase4/`'s structural template — it already does this exact job for a different
modification (1h entry filter): takes Phase 3's raw trades, re-applies a modified exit rule,
computes per-lot-event Calmar, and doubles as the grid sweep runner.

- **New folder `prometheus_backtest/phase5/`**, not a modification of `phase3/` in place — same
  reasoning `phase4/` used (keep the decided, live combo's own folder untouched).
- **`configs_p5.py`**: the rule grid (checkpoint T fixed at 60min per Phase A; trigger threshold X
  swept as **% of entry price, not raw points** — the 2-point trigger was ~0.023% at ~8850, the
  same 2 raw points at January's ~5800 prices is a different, larger % move, so points alone aren't
  comparable across the dataset's price range).
- **`run_p5.py`**: applies each grid cell's rule to Phase 3's raw trades, doubles as the sweep
  driver — same shape as `phase4/run_p4.py`.
- **Copy `_simulate_trade_detailed`, don't cross-import it from `phase3/bespoke_2lot_p3.py`.** It
  reads `LOT_SIZE` from `configs_p3` at module scope — the WTI side-project hit exactly this
  import-coupling trap already; phase5 needs its own copy, matching the precedent phase4 and
  phase3_crudeoil both already set.
- **New fill convention needed for the time-stop/tightened-SL exit**, since it doesn't correspond
  to a signal-driven event the way SL/target/trend_flip do. Decide and document explicitly (don't
  leave it implicit): exit at the **open of the bar immediately after the checkpoint bar**, same
  adverse-gap-through convention `_target_fill_price`/`_stop_fill_price` already use elsewhere in
  this repo — consistent with how every other exit type in this backtest prices its fill, not a
  new assumption invented just for this rule.

---

## Phase C — The rule grid

Priority-ordered by simplicity — cheapest, most interpretable rule first, escalate only if it
doesn't clear the bar:

1. **Time-stop (current-P&L based, not MFE)**: if unrealised P&L is still ≤0 (or below some small
   positive threshold) by T=60min, exit both lots at market. This is the simplest rule the
   confound analysis in Context justifies testing *first*, given how much of MFE's signal turned
   out to just be current P&L. If this alone clears the bar in Phase D, the MFE-specific rules
   below may not add enough to be worth the extra complexity.
2. **MFE-based time-stop**: if MFE < X% of entry price by T=60min, exit both lots at market — the
   rule the original finding most directly suggests. Compare its Calmar delta against #1's; the gap
   between them **is** the answer to "does MFE actually add anything actionable," not just
   descriptively but in backtested terms.
3. **Tightened SL instead of a hard exit**: same trigger (either #1 or #2's), but move `SL_PCT`
   from 2.2% to a tighter grid value (0.8-1.5%) rather than exiting outright — keeps the ~25-30%
   recovery-case trades' upside partially alive while still cutting the worst tail.
4. **Lot-selective**: trigger acts on lot1 only (book the stall, let lot2 keep riding the original
   thesis) or lot2 only — a softer intervention than exiting the whole position.
5. **Alert-only, no auto-action**: Phase 0 already covers this — listed here only for completeness
   of the rule-family spectrum, not as a phase-C backtest item (nothing to backtest, it's a no-op
   on P&L by definition).

Grid: T fixed at 60min (Phase A); X swept over 3-4 levels in % of entry price for whichever rule
family is being tested; SL_PCT swept 0.8-1.5% for rule #3. One variable changed per experiment,
matching this repo's existing controlled-testing convention (same shape `phase4/` already used for
its own two-dimensional grid).

---

## Phase D — Evaluation protocol (this is where most candidate rules should die)

- **Primary metric: per-lot-exit-event Calmar**, against the 10.21 CRUDEOILM baseline (and 6.80 for
  CRUDEOIL, cross-contract gate below) — same methodology already established for every other
  headline table in this repo. Report win rate, trade count, and **total P&L separately** from
  Calmar — a rule that cuts drawdown by cutting return is a different trade-off than one that adds
  both, and the plan/decision write-up needs to say which kind any surviving cell is.
- **CRUDEOIL as a gate, not an afterthought.** A candidate cell must beat its own contract's
  baseline on *both* CRUDEOILM and CRUDEOIL, same T/X, no re-tuning per contract — exactly the
  standard the mult-2.0-vs-2.5 decision and every other Phase 3 candidate was already held to.
- **Time-split validation**: calibrate the grid search on Jan-May trades, validate the winning
  cell(s) on Jun-Sep. 381 trades against a ~12-cell grid (per rule family) is not enough sample to
  trust an in-sample-only winner — this is the same concern that sank Phase 4's plausible-looking
  cells.
- **Smoothness check**: only accept a cell if its immediate neighbors in the grid also beat
  baseline. A single isolated spike surrounded by non-improving neighbors is noise, not signal.
- **Bootstrap CI on the Calmar delta.** The Risk of Ruin section already has trade-level bootstrap
  machinery (`prometheus_backtest/README.md`) — reuse it rather than re-deriving a resampling
  scheme, applied to (rule Calmar − baseline Calmar) to get a confidence interval, not just a point
  estimate.
- **Overlay the slippage model last, only on whatever survives everything above.** The time-stop
  exit is an extra market order at a non-boundary minute — real cost the cost-free baseline doesn't
  pay. Reuse `dynamic_sizing_sim_slippage.py`'s participation model (`A=0.3` anchor) rather than
  building a second slippage estimate from scratch.

**Shelve criterion, stated up front, same shape Phase 4 used**: if no grid cell beats baseline on
*both* contracts with a bootstrap CI clear of zero, after the time-split and smoothness checks —
document the negative result in `prometheus_backtest/README.md` next to Phase 4's own shelved
writeup, and stop. A plausible descriptive story is not sufficient on its own; Phase 4 already
demonstrated that for this exact repo.

---

## Phase E — Production decision, only if Phase D produces a survivor

If a rule clears every filter in Phase D: fold it into `configs_p3.py` (or a new
`configs_p5_production.py`, TBD at that point) behind its own flag, matching the existing pattern
`ENTRY_FILTER_1H_ALIGN_ENABLED` set for Phase 4 (gated off by default, built and unit-verified,
enabled only after a deliberate go-live decision). Not attempted until Phase D actually produces
something to decide on.

---

## Open questions

- Does the current-P&L-only time-stop (Phase C, rule #1) capture most of the achievable
  improvement, making the MFE-specific rules (#2-#4) not worth their added complexity? This is the
  single most important question the grid needs to answer, given how much of MFE's descriptive
  signal turned out to be redundant with current P&L (Context).
- What's the false-positive cost in trader terms, not just aggregate Calmar — the ~25-30% of
  low-signal trades that go on to win are the ones any of these rules gives up on. Worth a
  side-by-side table of "what would this rule have exited early that actually recovered" for
  whatever cell survives Phase D, not just the aggregate stats.
- Is the `trade_logs/` stale-file duplication (Context) present in `phase3_crudeoil/`'s and any
  future phase folder's own `trade_logs/` too? Not checked. A quick audit and cleanup is cheap and
  independent of this plan, but worth doing before it causes a real mismatch in some future
  analysis that isn't as lucky as this one turned out to be.
- Should Phase 0's live Slack MFE display eventually also show the checkpoint threshold from
  whatever Phase D lands on (e.g. a visual flag once a trade crosses into "would have triggered the
  rule"), even before/regardless of whether the rule itself goes live? Worth deciding once Phase D
  has an actual answer, not before.
