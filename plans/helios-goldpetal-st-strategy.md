# Plan: Helios — Gold Petal ST-Based Strategy Discovery

**Status (2026-09-29): Signal + exit config DECIDED. Phases 0-3 done for GOLDPETAL, including the production-parity backtest and full per-trade logs (§4h). Phase 4/5 (sizing, risk of ruin) not yet started.** Written at the user's request ("let's start work on Helios") following the same design trajectory Prometheus and Selene both used: rollover mechanics research first, then a raw signal sweep, then exit calibration, then position sizing/liquidity, then risk of ruin, then production.

**Decided config (2026-09-29): ST_PERIOD 10, ST_MULTIPLIER 3.5, SL 1.6% of entry price, no profit target — trend-flip is the only exit, same design shape as Selene's. 1 unit = 20 lots (20 grams), single tranche, no scale-out.** Chosen over both the wider 5.0% stop the naive full-window calibration picked (§4f) and every N=2..4-tranche structure tested (§4f) — the walk-forward validation (§4g) showed 5.0% was overfit to GOLDPETAL's recent trending stretch, and SL=1.6% is the value that actually holds up on held-out data (pre-period Calmar% 12.90 vs. true out-of-sample post-period Calmar% 12.04 — close to each other, unlike every other multiplier/SL combination tested). Full-window stats at this config (spliced series): 1,166 trades, 38.9% win rate, Rs 388,024 P&L, Rs −26,160 max drawdown, Calmar% 21.09 (2021-10-04 → 2026-09-25). Equity curve: [Helios Equity Curve](https://claude.ai/artifact/5kVFk4Ar5gdhRx6VagXTBR). **The production-parity backtest (§4h, roll-aware, not spliced) confirms this and does somewhat better**: 1,129 trades, 39.1% win rate, Rs 475,733 P&L at the 20-lot unit, Calmar 16.74 — the spliced series was pessimistic, not optimistic, same direction Selene found for SILVERMIC.

This document mirrors that plan's structure and, where useful, points at it directly rather than re-deriving what already transfers.

---

## 0. Why Gold Petal, and today's unblocking decision

`plans/selene-production.md` §11 named Gold Petal (working name **Helios**) as the next instrument after Selene, in the user's own priority order, deferred until Selene was running. Selene went live (paper) on Delos 2026-09-29, satisfying that condition.

Separately, Gold Petal had a second, harder blocker: an unreconciled discrepancy between the user's own chart-eyeballing ("so many minute bars where the volume traded was 0" for GOLDPETAL/GOLDTEN, not seen in SILVERMIC/NATGASMINI) and the liquidity screen's own numbers, which showed the opposite — GOLDPETAL *better* than SILVERMIC on zero-volume minutes, both full-window and in a tight recheck (`research/mcx_liquidity_screen/README.md`). That discrepancy was never actually reconciled — the follow-up question (platform, timeframe, date) was never answered. **2026-09-29, user decision: "Drop the blocker, proceed."** This unblocks Gold Petal research on the strength of the existing data screen, not because the original observation was explained. If GOLDPETAL's live behavior ever looks inconsistent with the backtest in a way that smells like a data-quality issue, this open thread is the first thing to revisit.

---

## 1. Structural differences from CRUDEOILM and SILVERMIC — read this before reusing any Prometheus/Selene code wholesale

**1.1 Contract facts, from the cached instrument master (`data_pipeline/data/mcx_instrument_master.csv`, synced 2026-09-28 23:56, no live AngelOne call needed — a live strategy is trading right now and ad-hoc AngelOne logins are avoided while that's true, per standing practice).** `lotsize=1` (1 gram — GOLDPETAL is a 1g contract, confirmed against the liquidity screen's own table), `tick_size=100` (paise-scaled master convention, same as SILVERMIC → real tick ₹1), `freeze_qty=10000` (lots, i.e. 10,000 grams = 10 kg — much smaller in absolute lot-count terms than SILVERMIC's 600 or CRUDEOILM's ceiling, but each lot is tiny, so this needs converting to real notional before it means anything), `is_cas_enabled=False`. Six months are listed simultaneously (Sep26 through Feb27) at monthly spacing — **monthly cadence, like CRUDEOILM, not SILVERMIC's quarterly** — worth double-checking empirically against the live scrip master periodically rather than assumed static, same caveat as Selene's §1.3.

**1.2 Physical settlement — delivery, not just an elevated margin (same category of risk as Selene's §1.2, tender period length not yet confirmed for this instrument specifically).** `plans/selene-production.md`'s own roll-policy note says Gold Petal is physical and "does" need the tender-period roll discipline crude's cash-margin logic doesn't strictly require. Selene's own tender period (5 working days before expiry) was confirmed by the user for SILVERMIC specifically (§1.3 of its plan) — **that confirmation does not automatically transfer to Gold Petal.** This is Phase 0's first concrete task here too: confirm Gold Petal's own tender margin period length before any production config, not carried over from Selene by convention. The shared roll rule in `hestia_core/roll_policy.py` is written to be uniform across instruments (5 working days), but if Gold Petal's real tender period differs, that's a real per-instrument gap in the "one roll rule for every commodity" decision, worth surfacing rather than silently assuming it fits.

**1.3 Cost model — likely to matter much more here than for Selene, and needs to be in the first sweep, not deferred to a later slippage stage.** A 1-gram lot means a very small notional and a very small ₹-per-tick-per-lot compared to SILVERMIC's 1kg lot or CRUDEOILM's lot size. Flat per-order charges (brokerage, exchange/SEBI/stamp charges) that were a rounding error against SILVERMIC's or CRUDEOILM's typical per-trade P&L could plausibly eat a much larger share of Gold Petal's, unless sized with proportionally more lots per unit. Selene's own sweep (§4 of its plan) deliberately ran cost-free first and layered costs on later (slippage in Phase 4/5) — that ordering may not be safe to copy here; a first pass at realistic per-order costs belongs in the raw sweep or immediately after it, before any ranking of multipliers is trusted.

**1.4 Margin formula — not yet derived, do not reuse Selene's `/8 × 4` or Prometheus's `/3 × 4`.** Both of those were instrument-specific, derived from a real observed margin figure at the time (`prometheus_production/README.md` §26, `plans/selene-silvermic-st-strategy.md` §1.5). Gold Petal's own divisor needs its own cross-check against a real observed margin figure before any sizing work — this is a Phase 0 task, not assumed.

**1.5 Liquidity — already screened, not blocking, but the underlying disagreement (§0) is worth keeping in mind during Phase 4/5's own liquidity work,** not just waved off after the blocker decision. `research/mcx_liquidity_screen/README.md`'s numbers: ADV 221,799 lots, 97.3% tradable minutes, median boundary volume 181 lots, p10 28, max size at 20% participation 36 lots (full window) — comparably liquid to SILVERMIC by this screen's own methodology, but the boundary/participation numbers are in **lots** and each lot is 1 gram, so the real per-order size ceiling in grams (or ₹) is much smaller than the lot-count alone suggests. Convert to real notional before comparing position-sizing headroom against SILVERMIC or CRUDEOILM.

**1.6 Roll cadence being monthly (§1.1) means Prometheus's own roll-mechanics code is probably the closer analog to reuse/adapt than Selene's**, which was written and tuned around a quarterly cadence with far fewer real rollover events per year to calibrate against. Worth checking `hestia_core/roll_policy.py`'s own tests for whether they already cover a monthly-cadence engine, or whether Gold Petal would be the first monthly-cadence engine exercising it for real (Prometheus itself is monthly, so this is likely already covered — confirm rather than assume).

---

## 2. Phase 0 — rollover mechanics research (blocking; do this before any backtest work)

Mirrors Selene's own Phase 0 (`plans/selene-silvermic-st-strategy.md` §2). None of the following is done yet.

1. **Confirm Gold Petal's real tender margin period length**, from MCX's own contract specification or circular — not from aggregator blog posts (the same standard Selene's §1.3 held itself to, including the lesson that two aggregator sources can disagree and the standard variant's rule doesn't always match a mini/micro variant's own).
2. **Confirm the live listing cadence** empirically via the scrip master (done once already above, §1.1 — re-check periodically) and settle whether the shared 5-working-day roll rule needs a per-instrument override here or whether Gold Petal's own tender period lands on the same 5 days crude and silver both did.
3. **Derive the margin-per-unit divisor** for Gold Petal from a real observed margin figure, the same cross-check method Selene used (§1.5): the user reports what one contract's margin actually is, and the divisor is backed out from `price × lotsize / divisor × multiplier`.
4. **Put a first-pass cost model on the table before the sweep is trusted** (§1.3) — real brokerage/exchange charge figures per order, converted to the same points/Rs-per-lot basis the sweep will report in.
5. Backfill/assemble history the same way Selene's Phase 1 did (§3 below) if not already sufficiently covered.

## 3. Phase 1 — data assembly

Fyers coverage already looks strong (`data_pipeline/data/mcx_fyers/GOLDPETAL/`: monthly contract files from 2021-11-30 through 2026-08-31, ~57 months per `plans/fyers-mcx-data-integration.md`'s own table) — no backfill gap identified yet, but not yet audited the way Selene's data assembly was (source-preference tiering, holiday-calendar cross-check, void-gap identification). Same blended-source architecture as `prometheus_backtest/data_loader_p3.py` / `selene_backtest/selene_data_loader.py` (front-month resolved empirically per day, not assumed from the filename) is the right starting point, reused rather than copied.

## 4. Phase 2 — raw signal-quality sweep, run 2026-09-29

**Mid-phase detour: GOLDTEN added as a second candidate.** Before the sweep ran, the user flagged GOLDPETAL's 1-gram lot as too small and asked to compare against GOLDTEN (10-gram lot). Checked before running anything: GOLDTEN and GOLDPETAL are comparably liquid in real GRAM terms (the lot-count-only liquidity screen numbers made GOLDTEN look ~9x thinner, but that's a unit-size artifact — GOLDTEN's lot is 10x bigger, so ADV in grams is actually ~241,800 g/day for GOLDTEN vs ~221,799 g/day for GOLDPETAL, comparable). GOLDTEN's bigger lot directly addresses the flat-cost concern (§1.3); its tradeoff is much less history — Fyers coverage only goes back to 2025-04-01 (~17 months) vs GOLDPETAL's 2021-10-01 (~5 years). **User's call: run both, compare on the actual sweep numbers.** `helios_backtest_goldten/` was built mirroring `helios_backtest/` exactly (`helios_configs_goldten.py`, `helios_data_loader_goldten.py`, `backtest_helios_goldten.py`, `trade_paths_helios_goldten.py`, `sweep_helios_goldten.py`) — same Prometheus/CRUDEOIL-style separate-directory pattern, tender period and margin ratio both carried over from GOLDPETAL as unconfirmed working assumptions for GOLDTEN specifically (flagged in that file's own docstring).

`python helios_backtest/sweep_helios.py` (GOLDPETAL, 2021-10-01→2026-09-28, 1,094,431 1-min bars, 93.5% Fyers) and `python helios_backtest_goldten/sweep_helios_goldten.py` (GOLDTEN, 2025-04-01→2026-09-28, 322,882 1-min bars, 78.5% Fyers) both ran clean on the first try — no loader errors, no crude-specific assumption broke (both are monthly cadence like CRUDEOILM, unlike SILVERMIC's quarterly). ST_PERIOD 10, multipliers 1.0–6.0, raw signal only (no SL/target/costs), 1 lot.

**Bug found and fixed before this table was finalized, 2026-09-29: GOLDTEN's Rs figures were initially 10x inflated.** `backtest_helios_goldten.py` was ported from the GOLDPETAL/Selene pattern, which converts `pnl_rs = pnl_points * LOT_SIZE`. That's correct for GOLDPETAL and SILVERMIC only because their price quote unit happens to equal their lot unit (1 gram, 1 kg respectively) — exactly the "hidden unit-conversion factor" trap Selene's own plan §1.6 flagged for GOLD/GOLDGUINEA/ALUMINIUM. Checked directly: GOLDTEN's LTP is already the *whole 10-gram lot's* value (confirmed by comparing same-minute closes: GOLDTEN ₹150,599 vs GOLDPETAL ₹15,116 at 2026-09-02 09:01, a ~10.0x ratio consistent with real per-gram spot parity), not a per-gram price — so one tick/point move is Rs 1 on the whole lot already, and multiplying by `LOT_SIZE=10` double-counted it. Fixed via a new `RS_PER_POINT_PER_LOT=1` constant in `helios_configs_goldten.py`, kept distinct from the physical `LOT_SIZE=10` (still used for gram-liquidity comparisons). The sweep was re-run after the fix; figures below are correct. This does not affect the %-normalized comparison further below, which was already computed as `pnl_points / entry_price` and never used `LOT_SIZE`.

**Full-window headline numbers, each instrument's own full history (not comparable to each other directly — different windows and lot sizes):**

| Mult | GOLDPETAL trades | win% | P&L Rs | GOLDTEN trades | win% | P&L Rs |
|---|---|---|---|---|---|---|
| 1.0 | 6,778 | 32.1 | 5,881 | 2,254 | 34.6 | 12,012 |
| 2.0 | 2,707 | 34.5 | 16,125 | 895 | 37.2 | 86,370 |
| 3.0 | 1,462 | 37.9 | 18,352 | 492 | 42.7 | 144,608 |
| 4.0 | 967 | 40.5 | 22,850 | 338 | 41.7 | 174,658 |
| 5.0 | 702 | 44.2 | 21,687 | 262 | 41.2 | 135,780 |
| 6.0 | 565 | 44.2 | 17,031 | 198 | 43.4 | 140,511 |

Every multiplier is net positive on both instruments — same broad shape as Selene's own Phase 2 finding (win rate rises with multiplier as trades get fewer/longer; a plateau rather than a sharp peak, here around 3.5–5.5 on both). GOLDPETAL's full-window P&L is smaller in absolute Rs (max ~₹22.9k vs GOLDTEN's ~₹174.7k, about 7.6x, not the ~76x the pre-fix numbers implied) — expected and **not meaningful on its own**, since it reflects five years of a 1-gram lot at earlier, generally lower gold prices, against 17 months of a 10-gram lot mostly at recent, higher prices. Comparing full-window totals directly would just be comparing lot size × price level, not signal quality.

**Same-window, normalized comparison (both restricted to GOLDTEN's own full window, 2025-04-01→2026-09-28; P&L per trade expressed as % of that trade's own entry notional, removing both the lot-size and price-level effects):**

| Mult | GP trades | GP win% | GP avg %/trade | GT trades | GT win% | GT avg %/trade |
|---|---|---|---|---|---|---|
| 1.0 | 2,046 | 34.6 | 0.023 | 2,254 | 34.6 | 0.002 |
| 2.0 | 830 | 38.0 | 0.116 | 895 | 37.2 | 0.071 |
| 3.0 | 459 | 40.7 | 0.195 | 492 | 42.7 | 0.207 |
| 4.0 | 308 | 40.6 | 0.378 | 338 | 41.7 | 0.351 |
| 5.0 | 222 | 45.9 | 0.505 | 262 | 41.2 | 0.343 |
| 5.5 | 201 | 44.8 | 0.529 | 231 | 42.0 | 0.395 |
| 6.0 | 189 | 40.7 | 0.426 | 198 | 43.4 | 0.475 |

**Reading.** Trade counts and win rates are close between the two instruments at every multiplier over the same window, but **"nearly identical" overstated it — checked directly (2026-09-29) and corrected.** Matching entry timestamps exactly, only 51–56% of one instrument's trades share the other's exact entry minute. Loosening to "a same-direction trade on the other instrument within ±2 hours" (mult 4.0 detail): 55.5% exact match, +28.2% near-match not exact (83.7% combined), only 1.0% actually disagree in direction nearby, and 15.3% have no match on the other instrument within the window at all. **Read correctly: the two signals are strongly correlated and directionally consistent (same underlying, as expected) but genuinely not the same trade-for-trade — real bar-level timing noise exists between the two contracts (different tick-level price paths → ST flips land a bar or two apart, or don't fire on one side at all in a given window), not just a denomination difference.** This doesn't overturn the normalized-P&L comparison below (each instrument's own trades are still compared to its own %-of-notional performance), but it does mean GOLDTEN's own signal quality is a genuinely separate thing to validate, not simply "GOLDPETAL's signal in bigger units."

On the normalized (%-of-notional) basis, **GOLDPETAL is at least as good as GOLDTEN in this window, and ahead at most multipliers** (e.g. 5.0: 0.505% vs 0.343%; 5.5: 0.529% vs 0.395%) — the "GOLDTEN's bigger lot must mean a better edge" intuition does not hold once lot size and price level are controlled for. **This reframes the original instrument-choice question: it is largely a practicality tradeoff between GOLDPETAL's much longer usable history (5 years vs 17 months, better for calibrating and validating exit rules and risk of ruin) and GOLDTEN's bigger lot (smaller flat-cost drag per unit of real exposure, §1.3) — but with the caveat just above, it is not quite as pure a "same edge either way" situation as first framed; each instrument's own signal still needs its own validation.**

**The cost concern that motivated GOLDTEN largely dissolves under Angel One's actual fee structure, checked 2026-09-29.** Angel One charges a **flat Rs 20 per executed order**, independent of trade value or lot count ([Chittorgarh](https://www.chittorgarh.com/brokerage_charges/angel-broking/14/), consistent across other aggregator sources), plus CTT/GST/stamp/exchange charges that scale with turnover (% of notional, so roughly equal between instruments at equal real exposure). This means: at 1 GOLDPETAL lot vs 1 GOLDTEN lot, GOLDPETAL is indeed far more cost-disadvantaged (the flat Rs 20/order is a much bigger fraction of its tiny ~Rs 15,000 notional and thin per-trade P&L). But **at EQUAL real exposure — e.g. 10 GOLDPETAL lots vs 1 GOLDTEN lot, both ~Rs 150,000 notional — the flat-fee drag is identical, since Angel One charges per order, not per lot**, and GOLDPETAL's freeze quantity (10,000 lots) is nowhere near binding at 10 lots. **The original premise for considering GOLDTEN — that a 1-gram lot is structurally more cost-disadvantaged — does not actually hold once GOLDPETAL is sized in multiples of lots rather than pinned to 1 lot per unit, which is what any real position-sizing scheme would do anyway (Phase 4/5).** Combined with §4's own findings (GOLDPETAL at least as good on a normalized basis, 3x the history, and — per the timing-divergence check above — GOLDTEN's own signal is a separate thing that would need its own validation with much less data to validate it against), this meaningfully weakens the case for carrying GOLDTEN forward into Phase 3 at all. Surfaced back to the user rather than silently dropping GOLDTEN, since "keep running both" was an explicit, recent decision this finding bears directly on. **User's decision, put to them directly with this finding in hand: still keep running both.** GOLDTEN continues into Phase 3 alongside GOLDPETAL, not as a cost-driven choice but (per the user) worth validating properly in its own right.

**Not yet done:** costs beyond the flat-fee check above (deliberately absent from the sweep itself per the user's Phase 2 decision, §10), drawdown/Calmar (needs the return-% work, deferred like Selene's), and the same regime-break check Selene's Phase 2 flagged (§4 below) — not yet run on either instrument.

---

## 4b. Phase 2 — raw signal-quality sweep (original scope, superseded by §4's actual run above)

`ST_PERIOD` × `ST_MULTIPLIER` grid, no SL/target/EOD, across the full available history — same shape as `sweep_p3.py`/`sweep_selene.py`. Per §1.3, decide with the user before running whether a first-pass cost estimate goes in alongside the raw sweep or stays deferred to Phase 3, rather than defaulting to Selene's ordering by habit.

## 4c. Instrument decision — GOLDPETAL only, 2026-09-29

**GOLDTEN dropped. "Since we're going with GOLDPETAL"** — after §4's findings (comparable-or-better normalized edge, 5 years vs 17 months of history, and the flat-fee finding that removed GOLDTEN's original cost rationale), the user settled on GOLDPETAL alone going forward. `helios_backtest_goldten/` stays in the repo as a completed, documented comparison (§4) — not deleted, just not carried into Phase 3.

**Unit redefined: 1 unit = 20 lots (20 grams), not 1 lot.** Raised by the user specifically because of GOLDPETAL's tiny 1-gram lot size: a 20-lot unit is still a small notional (~Rs 300,000 at current prices) but large enough to split into more than the 2-lot scale-out Prometheus/Selene both used. This is a genuinely new design space for this repo — every prior engine's scale-out was fixed at 1 or 2 lots per unit (Selene explicitly settled on 1 lot per unit, plan §12; Prometheus's 2-lot T1/T2 split is its whole exit design). GOLDPETAL/Helios's own exit calibration (Phase 3, below) needs its own multi-tranche design, not a direct port of either.

## 4d. MFE-band discovery, run 2026-09-29 — no distinct bands found

Before designing the N-tranche structure, the user asked whether the trade data itself shows distinct MFE (maximum favorable excursion) "bands" that would justify particular exit levels, rather than picking a tranche count arbitrarily. Checked across all six shortlisted multipliers (3.0–5.5), winners only, MFE as % of entry price: percentile tables and 12-bin histograms all show the same shape — a single mode around 0.4–1.0%, then a smooth, continuous, monotonic decay out to a long right tail (p95 around 4-6%, p99 out to 8-15% depending on multiplier). **No multi-modality, no gaps, no natural clustering at any multiplier.** This is a negative result worth recording as such, not glossed over: the tranche count and spacing are calibration parameters to grid-search (same as Prometheus/Selene picked T1/T2 by Calmar), not something the MFE distribution will hand us directly. The shape itself (dense near-in gains, thin long tail) does support scale-out generally — book the dense region early, let a smaller remainder ride the tail — just not a specific N.

## 4e. Phase 3 design, confirmed with the user 2026-09-29

**Not forcing targets.** User's explicit framing: if the data shows Helios does better with trend-flip-only exits (Selene's own decided design — ST 2.5, trend-flip, 3% SL, no targets, `plans/selene-silvermic-st-strategy.md` §12), that is a legitimate outcome to land on, not a fallback to avoid — "that's something I want backed with data." Selene's own Phase 3 found exactly this for SILVERMIC (every staged target winner earned *less* than the raw/SL-only signal). Phase 3 here must therefore compare, on equal footing:

- **SL-only** (trend-flip is the only profit exit, one stop-loss grid-searched) — the "Selene-style" candidate, mirroring `exit_structures_selene.py`'s Stage A.
- **N-tranche structures, N = 1, 2, 3, 4** — equal-weight tranches (20/N lots each), SL plus N staged profit targets, same flat-%-of-entry-price grid convention Prometheus/Selene both used.

All five (SL-only, N=1..4) compared by Calmar on the same trade set, same multiplier shortlist (3.0–5.5, §4's plateau). No candidate is assumed better going in.

## 4f. Phase 3 — exit calibration, run 2026-09-29

`python helios_backtest/exit_calib_helios.py` — generalized N-tranche simulator (`exit_calib_helios.py`, new, nothing in the repo generalized past Prometheus's fixed 2-lot before), staged calibration (SL grid, then T1..TN each staged), `DISABLED_PCT` available as an explicit per-stage candidate so a stage can legitimately conclude a tranche needs no real target rather than being forced to pick one. Two build issues found and fixed before the results below were trustworthy: (1) the original `SL_GRID` (max 4.0%) was hit at its own edge — widened after checking further out found the true plateau at 5.0% (results identical 5.0–10.0%, meaning no trades bind that wide); (2) the original `TARGET_GRID` (max 8.0%) was exhausted mid-run at N=4's 4th stage (crashed) — fixed properly via (1) above (DISABLED_PCT as an explicit candidate), not by endlessly widening the numeric grid.

**Results, all 6 shortlisted multipliers, SL-only vs N=1..4 (Calmar%, the %-of-entry-price-based Calmar, is what picks winners — same convention as Selene's `calmar_pct`):**

| Mult | SL-only | N=1 | N=2 | N=3 | N=4 | Winner |
|---|---|---|---|---|---|---|
| 3.0 | 12.87 | **15.22** | 15.15 | 15.08 | 14.79 | N=1 |
| 3.5 | 23.21 | **24.68** | 24.68 | 24.65 | 24.57 | N=1/N=2 tie |
| 4.0 | 17.72 | 18.62 | **23.93** | 21.10 | 19.95 | N=2 |
| 4.5 | 15.63 | **18.90** | 18.70 | 17.78 | 17.18 | N=1 |
| 5.0 | 17.45 | **20.84** | 19.86 | 19.28 | 18.83 | N=1 |
| 5.5 | 13.30 | 14.74 | **16.01** | 15.39 | 14.82 | N=2 |

**Reading — a real, if partial, answer to the "don't force targets" question (plan §4e).** Every multiplier's SL-only candidate is the *worst* of the five (13-23% Calmar% vs each multiplier's own N=1/N=2 best) — unlike Selene's SILVERMIC finding, **targets do genuinely help for GOLDPETAL, not just a wash**. But **N=3 and N=4 never win, anywhere** — Calmar% falls monotonically past whichever of N=1/N=2 wins at that multiplier. The user's original "freedom of multiple exit points" instinct does not pay off past 2 tranches on this data; a 20-lot unit does not benefit from more than a 2-way split, at least under this staged, equal-weight, flat-%-grid design.

**One caveat worth flagging before trusting the N=2 wins (4.0, 5.5) at face value.** Both of N=2's winning configs picked an unusually tight first target — 0.3% at both 4.0 and 5.5, the grid's own tightest option — which converts roughly half the unit into a near-immediate scratch exit (win rate jumps to 54.0%/50.9% from ~40%/38%) while total P&L drops sharply against the N=1 alternative at the same multiplier (mult 4.0: Rs 289,603 vs N=1's Rs 490,996; mult 5.5: Rs 191,669 vs N=1's Rs 331,275). Calmar% rewards the smoother, lower-drawdown curve this produces, but it is a real question whether "book half the position almost immediately" is a genuinely robust exit rule or a narrow fit to this particular trade sequence's early-move pattern — the same "Calmar surface is jumpy, driven by a handful of trades" caveat Selene's own plan flagged (§12 there). SL itself also jumps around a lot between multipliers (5.0% at 3.0/3.5/4.0, but 0.5% at 4.5/5.5, 1.2% at 5.0) with no obvious smooth trend, another sign worth taking as a caution flag, not yet a red one.

**Not yet done (at the time §4f was written):** the walk-forward pre/post split — needed before trusting any winner, exactly because of the jumpiness just flagged.

## 4g. Walk-forward validation and the decided config, 2026-09-29

Given §4f's jumpiness caveat, the SL-only design (no targets, trend-flip exit — the "don't force targets" candidate the user asked be tested on equal footing, §4e) was walk-forward validated properly: SL was re-derived using **only** pre-2025-01-01 trades (no look-ahead), then tested out-of-sample on the held-out post-2025-01-01 trades, for every shortlisted multiplier.

| Mult | SL from pre-only | Pre Calmar% | Post out-of-sample Calmar% | (naive full-window Calmar%, for comparison) |
|---|---|---|---|---|
| 3.0 | 1.6% | 15.76 | 6.06 | (12.87) |
| **3.5** | **1.6%** | **12.90** | **12.04** | (23.21) |
| 4.0 | 1.2% | 10.96 | 7.99 | (17.72) |
| 4.5 | 1.2% | 15.23 | 7.60 | (15.63) |
| 5.0 | 1.6% | 21.03 | 9.92 | (17.45) |
| 5.5 | 1.6% | 12.90 | 6.46 | (13.30) |

**Every out-of-sample number is well below its naive full-window headline** — the wide 5.0% stop §4f's naive calibration picked for 3.0/3.5/4.0 was fitting to GOLDPETAL's recent trending stretch, not a robust choice; the genuinely defensible SL is much tighter, ~1.2-1.6%. **Mult 3.5 is the standout**, and for the right reason: it is the only multiplier where pre-period Calmar% (12.90) and true held-out post-period Calmar% (12.04) sit close to each other — every other multiplier shows a large drop from pre to post (e.g. 5.0: 21.03 → 9.92), meaning their apparent in-sample edge didn't generalize forward.

**Decided: ST_MULTIPLIER 3.5, SL 1.6%, no target (SL-only/trend-flip design).** This is the N=1-tranche SL-only candidate from §4e/§4f's comparison, at the walk-forward-validated stop rather than the naive full-window one. Full-window stats at this exact config (1,166 trades, all of 2021-10-04 → 2026-09-25): 38.9% win rate, Rs 388,024 P&L, Rs −26,160 max drawdown, Calmar% 21.09 — **but this full-window number itself overstates typical performance** (same regime-dependence Selene found for SILVERMIC and Prometheus found for CRUDEOILM on the Fyers track): splitting the same run at 2025-01-01 shows the pre-period alone did Rs 93,440 P&L / Rs −7,400 DD / Calmar% 12.90 on 746 trades, and the post-period Rs 294,584 / Rs −26,160 / Calmar% 12.04 on 420 trades — 80% of total profit came from the most recent 1.75 years, and neither half's own Calmar comes close to the full-window's 21.09 (that number is an artifact of stitching a calm low-drawdown early stretch to a high-return full-drawdown recent one). The honestly-expected edge is a Calmar% around 12-13, not 21, though the consistency between pre and true out-of-sample post is real evidence this specific SL holds up, not just a full-window fluke. Win rate itself is stable across both halves (38.7% vs 40.0%), so the signal quality isn't degrading — it's specifically the magnitude of return, not the hit rate, that the recent stretch inflates.

Equity curve and drawdown, full window: [Helios Equity Curve](https://claude.ai/artifact/5kVFk4Ar5gdhRx6VagXTBR) (published 2026-09-29).

**Not yet done (at the time this section was written):** the production-parity backtest, and per-trade minute-by-minute trade logs / a proper `trade_summary.csv` at the decided config — done in §4h, below, the same session.

## 4h. Production-parity backtest and trade logs, run 2026-09-29

Built `parity_backtest_helios.py` + `parity_trade_logs_helios.py`, a near-verbatim port of Selene's own `parity_backtest_selene.py`/`parity_trade_logs_selene.py` (§13 of `plans/selene-silvermic-st-strategy.md`) — event-driven, per-contract ST (no splice), production's actual roll machinery (coincident-flip re-entry, close-and-switch, rollover-time veto with historical-basis stop recalibration). The port was mechanical because Selene's `simulate()` was *already* single-lot, SL-only/trend-flip-exit with no target machinery at all — exactly Helios's own decided design (§4g) — so only the imports (`helios_configs`/`helios_data_loader`) and the final spliced-reference comparison needed real adaptation, not the roll logic itself.

**Fyers void confirmed identical in shape to Selene's SILVERMIC finding:** GOLDPETAL's April-2026 and May-2026 contract files both effectively end 2026-03-31 (the May file has almost no real data, 1,260 rows, all pre-void), and the next real contract data (July-2026 file) starts 2026-06-30 — the same systemic gap, 2026-04-01 → 06-29, affecting every MCX instrument in this stretch. `PARITY_END = '2026-03-31'`, extended to `PARITY_END_EXTENDED = '2026-09-25'` with AngelOne filling the void, same `ANGELONE_OWN_FROM = '2026-09-02'` pipeline-wide date Selene's loader uses.

Full window (2021-10-01 → 2026-09-25): **1,129 trades**, 39.1% win rate. Roll events: 2 flat-switch, 11 coincident-flip re-entries, 24 non-coincident switches, 1 stop-mid-switch, 13 rollover-fallback GOs, 4 NO-GOs, 2 forced rolls (void-period, no production roll possible), 15 naive-fallback days — substantially more roll activity than Selene ever saw (SILVERMIC's whole window had 24 rolls total), expected given GOLDPETAL's monthly cadence vs SILVERMIC's quarterly one (~60 possible rolls over 5 years vs ~20-24). Exit reasons across all legs: 1,111 trend-flip, 17 rollover, 14 stop-loss, 2 forced-roll; 15 multi-leg trades.

**A real bug found and fixed before the comparison meant anything: a 20x unit-scaling mismatch, not a roll-jump effect.** The first run's printed comparison showed parity total P&L at ~16x *less* than the spliced reference (Rs 23,787 vs Rs 388,024) — alarmingly large, well past anything Selene's own roll-jump analysis found (a few percent). Spot-checking individual non-roll trades in a clean stretch (2022-03 to 2022-05, no rolls at all) showed every matched trade pair differed by an exact, constant 20.00x ratio — the signature of a units bug, not genuine roll noise (real roll effects don't produce a clean constant multiplier across every trade, rolled or not). Root cause: `to_trades(legs)`'s `pnl_pts` is raw PER-LOT points (correct convention, matches Selene's own `parity_trades.csv` — correct *there* because Selene's own unit is 1 lot), while `exit_calib_helios.run_variant`'s `pnl_rs` is already scaled by `UNIT_LOTS=20` (Helios's own "1 unit = 20 lots" decision, plan §4c) — comparing the two columns directly under the same name silently compared per-lot points against per-20-lot-unit rupees. Fixed in `parity_backtest_helios.py`'s `main()`: the comparison now explicitly scales `trades['pnl_pts'] * configs.UNIT_LOTS` before comparing; `parity_trades.csv` itself is left as raw per-lot points, unchanged, for downstream sizing work (same convention Selene's dynamic-sizing script consumes).

**Corrected result, scaled to the 20-lot unit — parity beats the naive spliced estimate, same direction Selene found, bigger gap:**

| | Trades | Win % | P&L (Rs, 20-lot unit) | Max DD (Rs) | Calmar |
|---|---|---|---|---|---|
| **Parity (roll-aware)** | 1,129 | 39.1% | **475,733** | −28,420 | **16.74** |
| Spliced (naive continuous series) | 1,166 | 38.9% | 388,024 | −26,160 | 14.83 |

Parity beats spliced by +22.6% total P&L (vs Selene's own +4.3% for SILVERMIC) — plausibly a bigger gap because GOLDPETAL's monthly cadence gives ~2-3x more roll events for the real roll machinery (coincident re-entry, historical-basis stop recalibration) to matter, each one a chance for the naive splice's unadjusted contract-to-contract price jump to misstate what a real trader would have captured. **The spliced backtest was pessimistic, not optimistic, here too** — same qualitative finding as Selene's own §13, just larger in magnitude. This doesn't overturn the walk-forward validation above (that used percent-of-entry-price ratios, not raw points, so it's insensitive to the per-lot-vs-per-unit scaling that caused this bug), but it does mean the *actual* expected edge is somewhat better than the spliced equity curve published earlier suggested, not worse.

**Trade logs now exist, matching Selene's and Prometheus's own convention** — the thing this session was asked about directly. `parity_trade_logs_helios.py` writes one CSV per trade (1,129 files, 102 MB, `helios_backtest/data_sweep/parity_trade_logs/trade_<id>_<entry date>_<HHMM>_<B|S>.csv`): one row per 1-minute bar of the contract actually held, entry to exit, with `unrealised_pts`, running MAE/MFE, the current stop level, and `entry`/`exit:<reason>` event markers — verified directly against trade 1's own file (correct entry/exit rows, MAE/MFE tracking, `sl_px` constant at 4735.58 for its whole hold as expected for a fixed-percentage stop). `parity_trades.csv` gained `final_mae`, `final_mfe`, `hold_hours` (same three columns as Selene's).

**Not yet done:** position sizing / liquidity and dynamic sizing / risk of ruin at the 20-lot unit (Phase 4/5, below) — not yet started.

## 5. Phase 3 — exit calibration

Re-derive from scratch for Gold Petal's own price/volatility character, same discipline both prior instruments followed — do not assume either CRUDEOILM's or SILVERMIC's calibrated exits transfer.

## 6. Phase 4 — position sizing / liquidity, and Phase 5 — dynamic sizing / risk of ruin

Reuse the CRUDEOILM/SILVERMIC methodology (boundary-minute participation table, tick-cost framing), converted to real notional per §1.5's caveat. Dynamic-sizing simulation and risk-of-ruin Monte Carlo follow once an exit config is decided.

## 7. Phase 6 — production build

Not scoped in detail here — deferred until the backtest phases above produce a decided config, same order Prometheus and Selene both followed. The `helios_engine/` build itself should be materially smaller than Selene's own P7 effort, since Hestia's shared machinery (roll policy, request lifecycle, ledger reconciliation, reporting) is already built and tested — the new work is mostly Gold Petal's own signal/exit parameters and registry wiring, not new host infrastructure.

## 8. Naming

**Helios** — decided 2026-09-28 (`plans/selene-production.md` §11): the Titan of the sun, the classical Sun↔Gold pairing, and Selene's brother (both children of Hyperion and Theia).

## 9. Open items / risks carried forward

- The original zero-volume-bar chart discrepancy (§0) is unblocked by user decision, not resolved. If Gold Petal's live behavior ever looks off in a way that smells data-related, revisit `research/mcx_liquidity_screen/README.md`'s four candidate explanations.
- Tender period length (§1.2/§2.1), margin divisor (§1.4/§2.3), and cost model (§1.3/§2.4) are all genuinely open — nothing here should be treated as decided until each is confirmed the way Selene confirmed its own equivalents.
- Data assembly (§3) needs the same audit pass Selene's got (source-preference tiering, holiday-calendar cross-check, void-gap identification) before being trusted for a sweep — not yet done, just not yet found lacking either.
- Module naming: use `helios_backtest/` with `helios_configs.py` / `helios_data_loader.py` etc., prefixed the same way Selene's were, since the Prometheus loader chain does a bare `import configs`.

## 10. Scope decisions, 2026-09-29

**Cost model timing: cost-free sweep, like Selene.** Phase 2's raw sweep stays pure signal quality, no costs — same ordering Prometheus and Selene both used. Costs come in starting Phase 3/4, not folded into the raw sweep despite §1.3's flagged concern about 1-gram-lot economics; the concern stands and should be checked for real once Phase 4/5 costs are added, not dropped.

**Fact sourcing: independent research first, user confirms.** For the tender-period length (§2.1) and margin divisor (§2.3), pull MCX's own contract circular / public margin figures first and propose values; the user sanity-checks both against what they actually see at the broker before either is treated as decided. Unlike Selene, this is not simply "the user reports a number" — a first pass is researched independently.

**Web research done, 2026-09-29 — partial, genuinely inconclusive on the two open facts, same shape as Selene's own §1.3 difficulty:**
- **Lot/tick/order-size facts cross-checked clean.** Three independent sources ([Zerodha Varsity](https://zerodha.com/varsity/chapter/gold-part-1/), a general MCX search summary, and [tradejini](https://www.tradejini.com/blogs/gold-futures-and-options-guide-for-indian-traders)) all agree: 1 gram lot, ₹1 tick (= ₹1 P&L per tick per lot), 10 kg max order size — matching the cached instrument master (`lotsize=1`, `tick_size=100` paise, `freeze_qty=10000`) exactly. §1.1's facts are now independently confirmed, not just cached.
- **Tender period: NOT confirmed for Gold Petal specifically.** Multiple sources ([Angel One](https://www.angelone.in/knowledge-center/commodities-trading/what-is-tender-period-in-mcx), [Groww](https://groww.in/blog/what-is-tender-period-in-mcx), a general search summary) converge on "5 working days before expiry" for gold generally, but Angel One's own article explicitly states "the length and timing of the tender period vary by commodity and contract specification, so there's no common deadline" and does not confirm the figure for Gold Petal/mini variants specifically — the exact same gap Selene's research hit for SILVERMIC (§1.3 there: "these may both be true... this needs settling from MCX's own contract specification or circular before it goes anywhere near a production config — not from aggregator blog posts"). MCX's own site (`mcxindia.com`) timed out on fetch; a third-party PDF claiming to be the official contract spec was unreadable (corrupted extraction) and separately gave a lot size (100g) that contradicts the confirmed 1g figure, so it's discarded as unreliable. **Working assumption for now: 5 working days, same as CRUDEOILM and SILVERMIC, pending the user's own broker confirmation** — not yet promoted to "resolved" the way Selene's was.
- **Margin divisor: not usable from public sources.** The only figure found ("as low as Rs.154" margin) is from an undated aggregator page and is inconsistent with GOLDPETAL's current price level (~₹15,000/gram in the local data) — a margin that low implies either a much older, far lower gold price, or a stale/promotional figure. **Needs a real current figure from the user's own broker screen**, the same way Selene's `/8 × 4` was ultimately settled (`plans/selene-silvermic-st-strategy.md` §1.5: "the user reports one SILVERMIC contract's margin is almost the same as CRUDEOILM's, which is a plausibility cross-check on the divisor").

**Both resolved by the user, 2026-09-29:**
- **Tender period: 5 working days**, confirmed by the user for Gold Petal's rollover — same as CRUDEOILM and SILVERMIC. `TENDER_ROLL_TRADING_DAYS=5` carries over, no per-instrument override needed after all.
- **Margin divisor, derived from one real observed point:** LTP ₹14,893, margin required ≈₹1,380 → margin/notional ≈ **9.27%** (`1380 / (14893 × lot_size=1)`). In the `margin_contract_value_divisor` / `margin_sizing_multiplier` config shape `hestia_core`'s engines already use (`selene_engine/levels.py`'s `margin_per_unit()`: `ltp × lot_size / divisor × multiplier`), this is `divisor=1, multiplier≈0.0927` — there's no clean small-integer split the way Prometheus's `/3×4` or Selene's `/8×4` had, so it's recorded as a direct ratio rather than forced into one. **Single-point derivation, same caveat as Selene's own §1.5** (not a measurement, a plausibility anchor from one reported figure) — worth re-checking against a second observed margin figure at a different price level before trusting it for real sizing work, but sufficient to unblock Phase 0.

Phase 0 is now complete. Moving to Phase 1 (data assembly) and Phase 2 (raw ST sweep).
