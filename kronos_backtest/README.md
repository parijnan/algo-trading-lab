# Kronos Backtest

Monthly Nifty short-premium, expiry-avoidant. Design and rationale live in
`plans/kronos-monthly-premium.md`; this directory is the code that tests it.

Kronos sells a defined-risk iron condor on the Nifty **monthly** expiry roughly
35 days out and closes it before expiry week. The thesis is in the exit, not the
entry: expiry-day settlement risk under the Closing Auction Session is a
per-expiry-*event* risk, and a monthly position that closes before expiry week
meets it zero times a year.

Built in isolation — no Leto routing, no VIX gate, no visibility into any other
strategy (Decisions B and C of the plan). Nothing here may import from a
strategy directory.

## Layout

| File | Contents |
|---|---|
| `configs.py` | Every parameter. Nothing else holds a tunable value. |
| `loader.py` | Holidays, index series, the monthly contract universe, option bars. |
| `expiry_rules.py` | Monthly identification by calendar rule; entry and exit date resolution. |
| `greeks.py` | mibian IV/delta, chain scanning, target-delta strike selection. |
| `phase0.py` | Phase 0 validation and the entry-feasibility measurement. |
| `engine.py` | Phase 1 simulation (static condor): entry, marking, management, single-slot loop. |
| `analysis.py` | Phase 1 reporting and the kill-gate verdict. |
| `regime_signal.py` | Decision E: containment + trend state series, confirmation-window smoothing. |
| `run.py` | Entry point. |
| `data/` | Generated output — gitignored. |

## Running

```bash
python kronos_backtest/run.py --phase 0            # validation, uses cached feasibility scan
python kronos_backtest/run.py --phase 0 --refresh  # re-scan the option chains (several minutes)
python kronos_backtest/run.py --phase 1            # baseline backtest and kill-gate verdict (FAILED)
python kronos_backtest/run.py --phase signal        # regime signal build + validation report
python kronos_backtest/run.py --phase signal --refresh  # rebuild the signal cache
```

Phase 0 writes `data/phase0_calendar.csv` (entry and all four exit dates per
contract) and `data/phase0_feasibility.csv` (chain depth at each candidate entry
DTE). `data/contract_data_start.csv` caches per-contract data coverage.

**Phase 1 ran on 2026-08-17 and FAILED the kill gate.** Rs 12,084 over 77
trades on one lot across seven years, breaking even at 1.30 points of per-leg
slippage. Full write-up in §9 of the plan.

**Pivoting to active management, 2026-08-18 (Decision E, plan §10).** Kronos
evolves in place — same codename, same infrastructure — into a decision tree
targeting 2-3% net/month: containment (`research/range_detection`, gate
passed) and trend (Apollo/Iris's live Supertrend) route between four states,
each with its own structure and sizing. The static condor's failure mode
(undefended directional drift, losses at *below*-average VIX) is exactly what
this targets. The regime signal is built and validated (`regime_signal.py`,
§10.6); the engine is not yet built.

The kill-gate thresholds live in `configs.py` as `KILL_GATE_*` and were written
there **before** the first run, which is what makes the verdict worth anything.
Do not move them to fit a result.

## What Phase 0 changed

Two of the plan's design defaults moved on Phase 0 evidence. The Nifty monthly
chain is only quoted within a band around spot, and the band is narrower the
further from expiry you look — so a wing target is a claim about liquidity, not
just about risk. Both-sides availability at a 30-minute staleness bound:

| Entry DTE | 0.15Δ short | 0.075Δ wing | 0.05Δ wing |
|---:|---:|---:|---:|
| 30 | 86/87 | 82/87 | 72/87 |
| 35 | 87/87 | 82/87 | 68/87 |
| 40 | 84/87 | 70/87 | 55/87 |
| 45 | 82/87 | 66/87 | 51/87 |
| 50 | 83/87 | 57/87 | 35/87 |
| 60 | 76/87 | 43/87 | 19/87 |

Entry moved from 45 to **35 DTE** and the wing from 0.05 to **0.075Δ**, giving
82/87 fillable — **80/87 once the liquidity filter is applied**, still the best
row at every volume floor up to 1000. Above that 30 DTE overtakes it. The Phase 2 sweep is capped at 45 DTE for the same reason.
The binding constraint is the outer leg alone — moving the *short* closer to ATM
widens the spread at no cost in availability, which is the Phase 3 lever if the
0.15/0.075 band proves too thin on credit.

## Single slot

Kronos never holds two positions at once — the previous trade closes before the
next opens (`ALLOW_CONCURRENT_TRADES = False`). This is not a neutral rule: at a
35 DTE entry the next contract's scheduled entry lands on or before the previous
scheduled exit for 0% of pairs under E1, 65% under E2a/E2b and 100% under E3. So
the exit policy decides how much of the year the single slot is occupied, and
Phase 4 compares **return on deployed capital per unit time**, not P&L per trade.

On collision the engine defers to the first trading day after the previous
position *actually* closes — an early profit-target exit frees the slot sooner —
provided `DEFERRED_ENTRY_MIN_DTE` of runway remains. That depends on realised
exits, so it is engine state: `phase0.py` reports scheduled collisions and
cannot enforce anything. Enforcement is a Phase 1 requirement, along with
recording `entry_dte_target` against `entry_dte_realised` so the Phase 2 sweep
does not silently measure something other than its own axis.

## Liquidity and strike substitution

A print inside the staleness bound says a strike traded once, not that an order
would fill. `loader.liquidity_stats()` measures volume and the count of distinct
traded minutes over a window ending at the decision time; `is_liquid()` applies
the config thresholds. If the target-delta strike fails the bar,
`greeks.select_strike()` substitutes a neighbour one strike away and records the
offset as `substituted`.

Direction errs toward safety on both legs — the **short moves outward** (less
risk, less credit), the **wing moves inward** (more protection, more cost).
Nearest-first alternation was rejected: it can widen the spread on both legs at
once, the one substitution that increases max loss.

## Two things that fail silently, and are therefore asserted

**Monthly contracts are identified by calendar rule** — the last expiry of each
calendar month — never by a lead-time threshold. The original data audit filtered
on lead time and, because it took the *latest* first-print across a contract's
strike files rather than the earliest, concluded that six 2020 monthlies had no
usable history. They all have 60+ days. Phase 0 asserts coverage rather than
assuming it.

**Prices carry an age.** The option files are trade-derived: a minute with no
trade produces no bar, so a naive "last price before now" lookup can return a
print from days earlier. Harmless for a 0.15-delta short, ruinous for a far-OTM
wing, where it manufactures both a fill that could not happen and an IV backed
out of a dead quote. `get_option_price` enforces
`MAX_PRICE_STALENESS_MINUTES`; `get_option_price_with_age` exposes the age for
measurement.
