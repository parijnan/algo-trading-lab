# Plan: Poseidon — Continuous Trend / Crisis-Alpha Overlay

**Codename: Poseidon** (working name — god of earthquakes/storms; strategy profits from
market upheaval rather than calm. Rename freely if a better fit turns up.)

**Status: SHELVED (2026-07-03).** Step 0 complete — MTM gap too weak to justify the engine.
§8 fallback (cheaper VIX-threshold fix) tested and also rejected. See §9 for the outcome.
No further work planned; this file is kept for the record and the call-back conditions in §8.

---

## 0. Context — why this idea, and what it isn't

This repo already designed and correctly killed a new-strategy candidate: **Ares**
(`plans/range-vega-strategy.md`), gated on the VIX-router and range-anchored-strike research
threads. Both closed with no exploitable edge (`79f8bd9` — router closed, containment-only;
`project_next_session` memory — range-anchored strikes tested as a non-lever for Artemis).
All other incremental research (OI, TC, Greek-based SL tuning) is similarly closed. The
productive vein inside the *existing* three engines (Artemis, Athena, Apollo/Iris) has been
mined out — a new strategy needs a different risk factor, not a better parameter.

**The diagnosis:** Artemis/Athena/Apollo/Iris all key off a *level* (VIX threshold, or an
already-open position). None of them are proactive to a *move*. And the book's only current
risk stat — Calmar 22.2, max drawdown ₹14,537 (`leto_backtest/analysis.py` on
`leto_trade_log.csv`) — is computed on **cumulative realized trade P&L**, not intraday
mark-to-market equity. A short-vol book can sit deeply underwater mid-trade and close near
flat once the SL/theta engine works it out. That number is almost certainly optimistic about
true tail risk, and it's the reason nobody in this repo has actually seen the book's real
drawdown.

Checked against 2020 (COVID crash) rather than assumed: Feb 24–Mar 4 2020 (VIX 14→24, index
down ~15%), Artemis/Athena stayed in normal short-vol mode and closed positive on realized
P&L. From Mar 13 (VIX 41→86) routing hands off to Iris, which scalped strong gains through
the acute crash — this is why 2020 is the book's best year, not a contradiction of the
"they all break together" thesis. So the book *does* have a reactive crisis sleeve
(VIX > 25 → Iris/Apollo). What it doesn't have is coverage for the **proactive window** —
trend-onset to the moment VIX actually crosses 25 — where Artemis/Athena are still fully
short-gamma. We have no visibility into how deep the intraday MTM dip was in that window
before it recovered on realized numbers. That gap is the target.

**Why trend-following over the alternatives considered:**
- *Event-vol selling* (IV-crush around RBI MPC / Union Budget / Fed nights) — genuinely new,
  not built anywhere in this repo, and a real returns idea. But it's short gamma/vega, same
  risk factor as Artemis/Athena — it loses in the same regime they lose in. Returns-only,
  not a drawdown fix. Worth a separate plan; not this one.
- *Long-vol tail hedge* (systematically owning cheap OTM vega) — the textbook drawdown answer,
  but bleeds negative carry in every calm month, which is most months here.
- *Continuous trend overlay* — different risk factor (price momentum, not vol level), roughly
  self-funding if whipsaw costs are controlled, and structurally positioned to catch the
  proactive window Apollo/Iris miss because it isn't VIX-gated.

---

## 1. Step 0 — MTM equity curve for the existing book (hard gate, build first)

**Why first:** the entire case for spending engineering effort on a diversifying overlay
rests on the existing book's true drawdown being materially worse than ₹14,537. If it isn't,
the urgency (and possibly the sizing) of this whole plan changes. This must be measured, not
assumed — it is the single sharpest open question from the research so far.

**Goal:** a single portfolio-level equity curve at 1-min resolution, 2020–2026, marking every
concurrently open position (Artemis + Athena + Apollo/Iris) to market bar-by-bar, not just at
entry/exit.

**Reuse, don't rebuild:** `research/greek_analysis/` already did most of this work for
Athena (`pnl_attribution/run.py`, 124-trade per-bar Taylor decomposition) and for Artemis
(`exit_timing/run_artemis.py`). The IV cache
(`research/greek_analysis/data/iv_cache/trade_NNNN_YYYY-MM-DD.parquet`) and `greek_engine.py`
give per-bar option values already reconstructed for both. What's missing:
- Per-bar MTM reconstruction for Apollo and Iris (their trade logs exist:
  `apollo_backtest/data/trade_logs*`, `iris_backtest/data/trade_logs`) — check if these are
  option-price logs (usable directly) or need the same IV-cache treatment as Athena/Artemis.
- A portfolio-level merge: at each 1-min timestamp, sum MTM across whichever strategy/strategies
  hold an open position at that moment (per the routing in `leto_backtest/router.py` — no
  overlaps, confirmed by the existing `[PASS] No overlapping trades` check, so this is a
  concatenation, not a true multi-strategy overlap problem).

**Metrics to produce:**
- True max drawdown (₹ and %) on the MTM curve vs the realized-P&L drawdown (₹14,537) — the
  gap is the number that matters.
- MTM-based Calmar, recomputed.
- Worst intraday drawdown duration and depth.
- Specific replay of the Feb 24–Mar 12 2020 proactive window — did Artemis/Athena's realized
  "closed positive" outcome hide a large intraday dip, or did the SL/exit logic genuinely
  contain it in real time too?
- Same replay for any other known 2022/2023–25 VIX spike episodes in the data.

**Gate rule (recalibrates, doesn't kill):**
- If MTM max drawdown is within ~1.5–2× the realized figure → the "hidden risk" argument is
  weak; the case for Poseidon then rests solely on the narrower proactive-window argument —
  still worth checking Step 1, but size conservatively and reconsider whether a cheaper fix
  (see §9) covers the same gap.
- If MTM max drawdown is materially worse (order of magnitude) → strong standalone
  justification for a diversifying sleeve regardless of what Step 1 finds.

---

## 2. Core design (pending Step 0 + Step 1 results)

### 2.1 Signal (candidates to test, not prescribe)
- Moving-average crossover or N-day breakout on Nifty/Sensex daily/intraday price.
- Reuse `research/range_detection/range_detector_pa.py` — it already validates established
  PA ranges and their breaks (§7 gate passed, `validate_gate.py`). A confirmed break of an
  established range is a natural, already-validated trend-entry trigger — cheap reuse instead
  of building a new signal from scratch.
- Supertrend, same pattern already used on VIX in `annotate_athena.py` (p=7/m=3.0 daily,
  p=10/m=3.0 75-min), applied to the underlying instead of VIX.
- Parameter budget: keep to ≤ 2–3 params total (repo convention, per `research/vix_router/`)
  — a trend signal with many knobs is exactly the overfitting risk that killed several prior
  research tracks.

### 2.2 Execution instrument
- Needs proactive, continuous directional exposure without heavy theta drag. Candidates:
  - Nifty/Sensex index futures via SmartConnect — **must verify** Angel One futures access
    and margin terms; current `data_pipeline/` only downloads options + index spot, no
    futures data, so this would need a new (small) data pipeline addition.
  - Deep-ITM long options (delta ≈ 1) as a synthetic future — Apollo/Iris already have this
    machinery (symbol/expiry/strike selection for ITM directional bets); likely the faster
    path to a first backtest, at the cost of some theta drag vs true futures.
- Decide via a feasibility check, not upfront — record the answer here once done.

### 2.3 Sizing and activation
- Small, dedicated capital sleeve — this is a diversifier, not the primary return engine.
  Sizing must not cannibalize Artemis/Athena margin.
- **Continuous, not VIX-gated** — this is the entire differentiation from Apollo/Iris. No
  VIX threshold for activation; entries come from the trend/breakout signal alone.

### 2.4 Exit
- Trailing stop / trend-reversal exit, not a fixed profit target — CTA-style: let winners
  run, cut losers small. Fixed targets would undercut the exact tail-catching behaviour this
  strategy exists for.

---

## 3. What to test for (validation, in order)

1. **Standalone edge:** does the trend signal have real Sharpe/Calmar on its own, net of
   realistic slippage, or is it whipsaw-negative in the mostly-calm 2020–2026 sample? Indian
   index trend-following has a thin reputation outside crisis windows — test this honestly,
   don't assume it.
2. **Correlation with the existing book**, using Step 0's MTM curve as ground truth — must be
   low/negative, tested specifically on the existing book's worst MTM-drawdown days, not just
   full-sample correlation (full-sample correlation can look fine while crisis-day correlation
   is what actually matters).
3. **Marginal portfolio effect:** combined MTM equity curve (existing book + overlay) vs
   existing book alone — does Calmar improve, does max drawdown shrink?
4. **Whipsaw/calm-period cost:** since most of the sample is calm, quantify the bleed. The
   "roughly self-funding" claim in §0 is a hypothesis, not a finding — this test either
   confirms or kills it.
5. **Stress-window replay:** Mar 2020, any 2022 rate-hike/VIX episodes, any 2023–25 spikes in
   the data — does the overlay actually produce convexity when it matters, not just in
   aggregate stats?

---

## 4. Backtest design

- **Data:** `data_pipeline/data/indices/nifty.csv`, `sensex.csv` (1-min), `nifty_daily.csv`,
  `india_vix.csv` — all already on disk, 2020–2026 (options data not needed for signal
  generation, only for the execution-instrument leg if going the synthetic-ITM route).
- **Engine:** new, function-based per repo convention (not class-based — backtest layer rule).
- **No lookahead:** signal at bar close, execute next bar — standard discipline used
  throughout this repo.
- **Baselines to beat:** existing book's MTM Calmar (from Step 0) with and without the overlay
  stacked on.

---

## 5. Risks & failure modes

- Trend-following on Indian indices may simply have thin edge outside genuine crisis years —
  realistic framing is "insurance that sometimes pays a premium," not a fourth alpha engine.
  Set expectations accordingly before any capital discussion.
- Futures access/margin on Angel One is unverified — may force the deep-ITM synthetic route,
  which drags on theta and dilutes the "self-funding" thesis.
- Capital drag: sleeve size directly trades off against condor/calendar capital — must size
  small enough not to cannibalize the primary income engines.
- Correlation risk: if the overlay actually correlates with the book during crisis days
  (contrary to the differentiation thesis), there's no diversification benefit — §3 step 2 is
  the explicit gate on this, not an assumption.

---

## 6. Phased path

1. **Step 0 (this plan's priority):** MTM equity curve for the existing book, reusing
   `research/greek_analysis/` infra for Athena/Artemis and extending it to Apollo/Iris.
   Produces the true drawdown-gap number.
2. **Step 1:** standalone trend-signal validation (pure index price, no options) — is there
   real edge net of costs?
3. **Step 2:** execution-instrument feasibility (futures vs deep-ITM synthetic).
4. **Step 3:** correlation + stress-window test against Step 0's MTM curve.
5. **Step 4:** sizing and portfolio-level Calmar/drawdown improvement quantification.
6. **Step 5:** production design — only if Steps 1–4 all clear their gates.

---

## 7. Data & infra reference

- `data_pipeline/data/indices/nifty.csv`, `sensex.csv`, `nifty_daily.csv`, `india_vix.csv`
- `research/greek_analysis/greek_engine.py`, `pnl_attribution/run.py`,
  `exit_timing/run_artemis.py` — MTM/Taylor decomposition machinery for Step 0
- `research/range_detection/range_detector_pa.py` — validated breakout signal, reusable as
  the trend-entry trigger (§2.1)
- `artemis_backtest/data/trade_summary_{nifty,sensex}_rerun.csv`,
  `athena_backtest/data/trade_summary_vix_all.csv`, `apollo_backtest/data/trade_logs*`,
  `iris_backtest/data/trade_logs` — needed for Step 0's concurrent-position reconstruction
- `leto_backtest/router.py`, `leto_backtest/data/leto_trade_log.csv` — confirms no overlapping
  trades across strategies, simplifying the Step 0 merge to concatenation

---

## 8. When to call back

- If Step 0 shows the MTM drawdown gap is small (§1 gate) **and** Step 1 shows no standalone
  trend edge → don't build Poseidon. The diversification case would rest solely on the
  proactive-window argument, which is narrower — check first whether the same benefit is
  available more cheaply by lowering Apollo/Iris's VIX-activation threshold (a config change,
  not a new engine).
- If Step 1 fails (no edge) but Step 0 confirms a large MTM gap → the actionable finding
  shifts to "the existing book needs earlier/better hedging in the proactive window," not
  necessarily a new strategy — revisit the VIX-gate threshold question before building a
  separate engine.
- If both validate and the correlation/stress-window gate (§3.2, §3.5) passes → proceed to
  production design.

---

## 9. Outcome (2026-07-03)

**Step 0 complete** — `research/mtm_equity/`. MTM max DD ₹18,986 vs realized ₹14,537 (1.3×),
below the §1 gate's 1.5–2× weak-evidence threshold. The full-sample hidden-risk case is weak.
The 2020 COVID proactive-window dip is real but modest: ₹2,031 below window start on ~₹12K
starting equity, recovered within 2 days.

**§8 fallback tested and rejected** — `research/iris_threshold/`. Swept `ROUTING_VIX_HIGH`
(25→22→20→18); at every lower value, book total P&L fell monotonically (₹3,22,733 → ₹2,76,951
at 18) because the threshold is shared between Athena's ceiling and Iris's floor, and 20–25 is
Athena's most profitable VIX band. The specific 2020 window this was meant to fix got *worse*,
not better (+₹6,877 → +₹708 at threshold 22). No cheap fix exists.

**Genuine mechanism found, not fixed:** tracing the baseline COVID window showed Iris didn't
fire on Mar 6/9 2020 despite VIX > 25 because Athena's Mar 4 trade was still an open position
(exits Mar 11, `pre_expiry`) and the simulator's no-concurrent-trade constraint blocks routing
checks — including Iris's — until the active slot frees up. Iris can't preempt an open
Athena/Artemis position on a VIX spike; it only takes over once that position exits on its own
terms. Fixing this for real means mid-trade VIX-escalation exit logic in Athena/Artemis, not a
router config change — out of scope given the ₹2,031/2-day size of the gap it would close.

**Decision:** Poseidon is shelved. No trend-signal work (§2–§6) will be built. This file stays
as documentation; revisit only if the §8 call-back conditions change (e.g. a future MTM re-run
shows a materially worse gap, or a new proactive-window episode in fresh data is much larger
than 2020's).
