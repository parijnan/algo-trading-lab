# Plan: Kronos — Monthly Short-Premium, Expiry-Avoidant

**Codename: Kronos** (Titan of time — the strategy's entire thesis is which slice of the decay curve it harvests, and which one it deliberately refuses).

**Status: Phase 1 run 2026-08-17 — kill gate FAILED (§9).** `kronos_backtest/` holds the loader, calendar rules, greeks, Phase 0 validation and the Phase 1 engine. Phases 2–6 are not built and should not be until the §9 decision is taken.

**Design settled for a standalone backtest.** Scope decisions taken by Pari on 2026-08-10 (§4): build in isolation, no VIX gating, exit policy compared across arms rather than chosen. Live routing and capital allocation are explicitly deferred, not pending. No code exists yet — next step is `kronos_backtest/`.

**Brief**: `plans/monthly-option-selling-feeder.md`. CAS investigation detail: `plans/closing-auction-session.md`. Where the feeder and CLAUDE.md disagree on Athena's exit paths, this plan uses the feeder's code-verified version.

---

## 1. Thesis — why monthly, and why now

The obvious framing of "go monthly" is *slower theta, less noise, fewer commissions*. That is not the argument. The argument is CAS-specific and quantitative.

§5.2 of the feeder establishes that **expiry-day settlement risk is a per-expiry-event risk**, not a per-day risk. Two mechanically distinct blowup modes were observed on the first CAS-era expiry of each index — a moneyness-flip/gamma event on Nifty (24,500 CE roughly doubled at the settlement print) and an extrinsic-collapse event on Sensex (78,800 straddle 505.95 → ~154.65 inside ~15 minutes). Both are unhedgeable in the auction window because there is no continuous underlying tape to react against, and both are currently **un-backtestable** — no CAS-era data exists at all for the cadence this strategy would trade (§5.4 of the feeder; confirmed in §3 below, where zero post-CAS monthly expiries have yet occurred).

A weekly short-premium strategy meets that event ~52×/year. A monthly one meets it 12×. **A monthly strategy that closes before expiry week meets it zero times.**

That last step is the actual thesis. Closing before expiry week converts the book's single largest un-modellable tail risk into *no exposure*, rather than into a mitigation whose effectiveness cannot be measured. Artemis's `evaluate_expiry_day_close()` is a good patch — it force-closes at 15:15 on expiry day — but it is still a patch on a strategy whose structure requires it to be near expiry constantly. Kronos would be the first strategy in the book whose *structure*, not its exit logic, keeps it away from the auction.

The cost is explicit and should not be glossed: **the final weeks are where theta accelerates.** Kronos deliberately gives up part of the richest section of the decay curve in exchange for never holding a gamma-loaded position into an event with no historical distribution. How much it should give up is not assumed — the exit policies are compared head-to-head (§4 Decision A, §6 Phase 4), spanning from an early 21-DTE exit to holding all the way through expiry.

### Why it isn't a clone

| | Instrument | Sold expiry | Vega | Hold period | Expiry-day CAS exposure |
|---|---|---|---|---|---|
| **Artemis** | Sensex | weekly | short | days | ~52/yr (patched: 15:15 close) |
| **Athena** | Nifty | 2nd weekly | **long** | ~1 week | ~52/yr |
| **Iris** | Nifty | n/a (long option) | ~flat | intraday | 0 |
| **Ares** (concept) | Nifty | weekly | *router-selected* | days | ~52/yr |
| **Kronos** | Nifty | **monthly** | short | 3–4 weeks | **0–12/yr** (exit policy under test) |

No strategy in the book currently holds a short *monthly* expiry. Athena buys monthly legs, so monthly-expiry mechanics and data handling are not unexplored — but its monthly leg is the long side of a calendar, structurally the opposite exposure. Kronos occupies a genuinely vacant theta/vega/gamma cell: low gamma, meaningfully short vega, slow but stable decay.

---

## 2. Scope — what this phase is and isn't

**Kronos is being developed in isolation.** It has no visibility into any other strategy, no routing integration, and no VIX gate. It must stand or fall as a standalone strategy that works across all VIX regimes. Two consequences worth stating plainly:

- **Deferred, not pending**: live routing and coexistence architecture, capital allocation, margin contention with the existing book, and any portfolio-level interaction analysis. §4 records what has already been established about these so the work isn't redone later, but none of it gates the backtest.
- **Only one constraint is permanent**: the backtest is 100% pre-CAS and cannot be otherwise (§3). Not a task to complete — a property of the evidence. Carried as risk §7.1.

Nothing here blocks starting. The one thing to avoid is building any dependency on Leto, the routing state, or another strategy's state files into Kronos while it is in isolation.

---

## 3. Data — audited, not assumed

Measured directly against `data_pipeline/data/` on this machine (2026-08-10):

| | Monthly contracts | Range | Typical lead time | Schema |
|---|---|---|---|---|
| **Nifty** (`nifty/options/`) | **87** | 2019-05-30 → 2026-07-28 | 40–92 days, median 90 | 1-min OHLC + `volume` + `open_interest` |
| **Sensex** (`sensex/`) | 23 | 2024-10-31 → 2026-08-06 | **14–27 days** | 1-min OHLC + `volume` only |

Supporting series: `indices/nifty.csv` and `indices/india_vix.csv`, 1-min, 696k rows each, 2019-01-28 → 2026-08-07. Both cover the full Nifty option history with room to spare.

**This settles the instrument question on its own, before CAS enters the argument.** Sensex monthly contracts carry only 14–27 days of data before expiry — *less than the DTE at which this strategy would enter*. A 45-DTE Sensex entry cannot be backtested because the data does not exist at entry time; a 30-DTE entry barely can, on some contracts. Sensex option files also lack `open_interest`, ruling out OI-based strike or liquidity filters.

CAS reinforces the choice rather than driving it: §5.1's overnight-gap-through-stop mechanism (BSE's closing auction producing **zero trades** for major constituents across 18 of 18 stock-days tested, leaving Sensex's close stale and dumping the information into the next open) is a Sensex-specific live-hold risk, and §6 of the feeder confirms there is no BSE constituent-level auction feed to monitor it with. A strategy that holds for three weeks is maximally exposed to exactly that.

**Nifty only. Not close.**

### Corrected 2026-08-17: there is no 2020 coverage hole

An earlier revision of this section reported that lead time degrades sharply with entry DTE — 57 of 87 contracts usable at 60 DTE, 74 at 45 — and that six 2020 monthlies, including the COVID-crash contract 2020-03-26, had too little history to use at any entry DTE. **That was a measurement artifact and both claims are withdrawn.** The audit behind them took the *latest* first-print across a contract's strike files rather than the earliest. Strikes are listed progressively as spot moves, so a strike created during the crash itself — `6150pe.csv` in the 2020-03-26 directory, first printing on expiry day — made a contract with 90 days of history look like it had none.

Measured correctly, by the earliest print anywhere in a contract's strike files (`kronos_backtest/loader.contract_data_start`, asserted in Phase 0): **coverage runs 40 to 92 days, median 90.** Exactly one contract falls short of 60 days — **2025-04-30, at 40 days**, which cannot be entered above 40 DTE. All twelve 2020 monthlies carry 87–90 days. The COVID regime is fully present in the sample.

Two things follow. The Phase 2 sample-composition concern largely dissolves: at any entry DTE from 30 to 60 the universe is 86 or 87 contracts, not 57 to 81, so the fixed-contract-set requirement below is now a cheap formality rather than a constraint that would distort the sweep. And risk §7.2's strongest claim — that the regime which matters most is missing from the evidence — is withdrawn.

What does vary with entry DTE is something the old measure could not see at all: whether the strikes the structure needs are *quoted* at entry. That is measured in "Entry feasibility" below, and it is the real constraint.

**Methodological consequence for Phase 2 (unchanged in principle)**: the entry-DTE sweep must still run over a fixed contract set — the intersection across all DTEs tested — with the full-universe result reported separately, per the repo's one-variable-per-experiment rule. It simply costs almost nothing now.

Monthly contracts must be identified by **calendar rule** (last expiry of the month, trailing incomplete month excluded), *not* by a lead-time threshold. This was always the right rule and the artifact above is exactly why: a lead-time filter would have discarded genuine monthlies for a defect in how their history was measured. Identification and usability are kept in separate functions in `expiry_rules.py` and `loader.py` so the two cannot be confused again.

### Entry feasibility — the real constraint, and it moved two design defaults

The measure that matters is not how far back a contract's data reaches but whether the strikes the structure needs are *quoted at the moment of entry*. Measured across all 87 monthlies at six candidate entry DTEs, both sides, counting a strike as fillable only if it has a print no more than 30 minutes old (`kronos_backtest/phase0.py`, output in `data/phase0_feasibility.csv`):

| Entry DTE | 0.15Δ short | 0.075Δ wing | 0.05Δ wing |
|---:|---:|---:|---:|
| 30 | 86 / 87 | 82 / 87 | 72 / 87 |
| **35** | **87 / 87** | **82 / 87** | 68 / 87 |
| 40 | 84 / 87 | 70 / 87 | 55 / 87 |
| 45 | 82 / 87 | 66 / 87 | 51 / 87 |
| 50 | 83 / 87 | 57 / 87 | 35 / 87 |
| 60 | 76 / 87 | 43 / 87 | 19 / 87 |

The Nifty monthly chain is only quoted within a band around spot, and the band is narrower the further from expiry you look. **At the 45 DTE / 0.05Δ wing combination §5 originally specified, the condor is constructible for 51 of 87 contracts — 59%.** That is not a tuning inefficiency; it is the structure being unbuildable for two-fifths of the sample. The failures are roughly symmetric between the CE and PE sides and scattered across years rather than clustered in a regime.

The staleness bound is not what drives this. Relaxing it from 30 minutes to unbounded moves the 45 DTE / 0.05Δ figure from 51 to 52. Tightening it to 5 minutes moves it to 47. The chain is genuinely unquoted out there, not merely sparse — a fixed-width wing does no better (52/87 at short ± 500 points).

**Adding the liquidity filter costs little, and does not move the choice.** The table above counts a strike as fillable if it has a print inside the staleness bound — which says it traded once, not that an order would fill. Requiring real liquidity (Decision D: volume over a 30-minute window ending at the decision time, across at least 3 distinct traded minutes), both sides, 0.075Δ wing:

| Volume floor | 30 DTE | 35 DTE | 40 DTE | 45 DTE | 50 DTE | 60 DTE |
|---|---:|---:|---:|---:|---:|---:|
| none | 82 | 82 | 70 | 66 | 57 | 43 |
| **≥250 (configured)** | 79 | **80** | 70 | 62 | 51 | 37 |
| ≥500 | 79 | 80 | 69 | 61 | 49 | 35 |
| ≥1000 | 79 | 78 | 69 | 60 | 48 | 30 |
| ≥2000 | 77 | 74 | 68 | 54 | 41 | 25 |

At the configured floor, 35 DTE goes from 82/87 to **80/87 (92%)** — still clear of the 90% gate, and still the best row. The Phase 2 fixed contract set drops from 60 to **55 of 87**. So the entry-DTE and wing choices stand.

One thing to keep in view: 35 DTE's advantage over 30 is thin and erodes as the bar rises — at a ≥2000 floor, 30 DTE (77) overtakes 35 (74), because the extra five days of chain depth stop compensating for thinner trading. If live fills prove worse than the backtest assumes, 30 DTE is the fallback, not a wider wing.

**Two defaults changed on this evidence**, both within §5's stated status as sweep starting points rather than settled values:

- **Entry DTE 45 → 35.** 35 is where short-side availability peaks (87/87) and wing availability is at its joint best. Entry is a band, `DTE ∈ [30, 35]`, so real entries will scatter within it; both endpoints measure at 82/87 for the wing, so the whole band is covered rather than just the target.
- **Wing delta 0.05 → 0.075.** At 35 DTE this takes the structure from 68/87 to 82/87 (94%).

The Phase 2 sweep is capped at **30–45 DTE** for the same reason. Including 50 and 60 would collapse the fixed contract set from 60 to 29, and the sweep would then be measuring how deep the chain is quoted rather than where the decay curve pays best.

One consequence to carry into Phase 3: a 0.15Δ short against a 0.075Δ wing is a narrow band, so net credit per condor will be thin. The binding constraint is the **outer** leg alone — moving the short closer to ATM widens the spread at no cost in availability. If Phase 1 shows credit too thin to clear costs, widening via a 0.20–0.25Δ short is the lever to try before abandoning the structure, and it should be tried before concluding the strategy does not work.

Seven contracts remain unfillable at the chosen settings once the liquidity filter is on (five without it). The engine must skip them rather than substitute a nearer strike — that skip path does not exist yet, because the engine does not, and it is a Phase 1 requirement. Phase 0 fails if the unfillable share ever exceeds 10%.

**Zero CAS-era monthly expiries exist.** The last Nifty monthly in the data is 2026-07-28, pre-CAS by cadence; CAS went live 2026-08-03; the first post-CAS Nifty monthly (~2026-08-25) has not occurred yet. Every number the backtest produces will be pre-CAS. Under an expiry-avoidant exit policy this matters much less than it otherwise would — the strategy never touches the mechanism the missing data would describe — but it is not zero, because IV behaviour in the 45→21 DTE window could still shift in a CAS regime.

---

## 4. Decisions taken (Pari, 2026-08-10)

### Decision A — exit policy: compare arms on evidence, don't pick one

Three policies, implemented as a single `EXIT_POLICY` config axis and swept head-to-head in Phase 4:

- **E1 — DTE exit.** Close when DTE ≤ 21 (the DTE value itself is a sub-sweep). Earliest exit, zero expiry exposure, forfeits the most decay.
- **E2 — Pre-expiry-week exit** (Pari's, and the one to beat). Close shortly before expiry week so the position never reaches ELM and never carries the final pre-expiry weekend. Two readings, both tested — see below.
- **E3 — Hold to expiry**, force-close 15:15 on expiry day (Artemis's pattern). Maximum decay capture, accepts §5.2 settlement risk and mandatory ELM handling.

**E2's rationale** — Pari's — is that it buys most of what E3 offers while giving up two specific, identifiable risks: **ELM** (which lands on expiry day and T−1, both after E2 has closed) and **the final weekend gap** immediately before expiry week, when the position is at its most gamma-loaded and a Monday open can move straight through a stop with no intervening tape.

**E2 has two readings, and they diverge across most of the sample.** Pari gave two worked cases: Tuesday expiry → exit the preceding **Friday**; expiry shifted to Monday → exit the preceding **Thursday**. Both are satisfied by "exit exactly 2 trading days before expiry" (E2a) — but that is an induction from two examples in the current Tuesday-expiry regime, and the backtest is mostly not in that regime:

| Expiry weekday | Contracts | E2a (T−2 trading days) | E2b (composite, below) | Diverge? |
|---|---|---|---|---|
| **Thursday** | **72** | Tuesday of expiry week — **holds the final weekend** | preceding Friday (T−4) | yes, 71 of 72 |
| Wednesday | 4 | Monday of expiry week — **holds the final weekend** | preceding Friday (T−3) | yes, 4 of 4 |
| Tuesday | 10 | Friday ✓ | Friday ✓ | no |
| Monday | 1 | Thursday ✓ | Thursday ✓ | no |

Measured in Phase 0: the two readings **disagree on 75 of 87 contracts (86%)**. (An earlier revision said 72/83% — it had the Wednesday row wrong, assuming E2a lands on Friday there when T−2 trading days from a Wednesday is the Monday of expiry week. The single Thursday exception is 2022-10-27, where the Diwali cluster collapses both readings onto Friday 21 October.) Wherever they disagree, E2a contradicts the stated rationale — it holds short through exactly the weekend the rule exists to avoid. E2b matches the rationale everywhere but costs two to four extra days of decay. They coincide for every Tuesday and Monday expiry, so under the current Tuesday-expiry regime **live behaviour is identical either way** — this is purely a backtest-interpretation issue.

**E2b is a composite**, not simply "the last trading day before the final weekend". Read literally, that phrase gives Friday for a Monday expiry, which would leave the position open on T−1 and therefore exposed to ELM — the other half of Pari's rationale. E2b is implemented as **the earlier of (last trading day before the weekend immediately preceding expiry) and (T−2 trading days)**. The weekend term binds for Thursday and Wednesday expiries; the T−2 term binds for the Monday expiry, producing Thursday as Pari's second worked case requires.

**Resolved: run both as sub-arms.** Implemented as a `configs.py` parameter, `EXIT_OFFSET_MODE` ∈ {`trading_days` (E2a), `avoid_final_weekend` (E2b)} — a date function, not a second code path, so the cost is negligible. Both are compared in Phase 4.

Phases 1–3 still need a single baseline: **use E2b**, because it is the reading consistent with the stated rationale in every regime, so the earlier sweeps are conditioned on an exit rule that means what it says. If Phase 4 shows E2a wins, re-run the Phase 2–3 winners under it before concluding — cheap, and the alternative is a silently confounded result.

Either reading resolves against `data_pipeline/config/holidays.csv` (117 holidays, 2019 onward, covering the full backtest range) rather than by weekday arithmetic. Weekday logic would silently mishandle mid-week holidays.

### Decision D — single slot, and liquidity-aware strike selection (Pari, 2026-08-17)

**Kronos never holds two positions at once.** The previous trade must be closed before the next is opened. Capital efficiency: the strategy is sized against one pool, and a second concurrent condor would double the margin call on it for a return the backtest has no way to attribute.

This is not a neutral constraint layered on top of the exit comparison — **it entangles with it.** Measured at a 35 DTE entry, the next contract's scheduled entry falls on or before the previous position's scheduled exit for:

| Exit policy | Collisions | Median overlap | Scheduled hold-days as a share of the calendar |
|---|---:|---:|---:|
| E1 (21 DTE) | 0 / 86 | — | 46% (54% of the calendar idle between trades) |
| E2b | 56 / 86 (65%) | 1 day | 96% |
| E2a | 56 / 86 (65%) | 5 days | 107% — oversubscribed |
| E3 (hold to expiry) | 86 / 86 (100%) | 7 days | 115% — oversubscribed |

E1 leaves the slot empty for two to three weeks of every cycle; E3 cannot enter at its natural point at all. **So the exit policy no longer just sets P&L per trade — it sets how much of the year the capital is deployed.** §6's Phase 4 metric changes accordingly.

**On collision, defer rather than skip**: enter on the first trading day after the previous position actually closes, provided at least `DEFERRED_ENTRY_MIN_DTE` (21) of runway remains, else skip that cycle. Deferral redeploys capital the moment it frees, which is the point of the rule; skipping would drop roughly half the E3 sample and answer a different question. Deferral resolves against the **actual** exit, not the scheduled one — a profit-target exit frees the slot early — so it is engine state and lives in the Phase 1 engine, not in `expiry_rules.py`. Phase 0 reports the scheduled collisions; it cannot enforce anything.

Consequence for Phase 2: under deferral the realised entry DTE drifts below the swept value (median 28 under E3, 34 under E2b). Every trade records `entry_dte_target` and `entry_dte_realised`, and Phase 2 reports the realised distribution alongside each swept point — otherwise the sweep silently measures something other than its own axis.

**Strike selection must clear a liquidity bar, with a one-strike fallback.** A print inside the staleness bound says a strike traded once; it does not say an order would fill. Liquidity is measured over a 30-minute window ending at the decision time — total volume, and the number of distinct minutes traded, which is unit-free and does not depend on whether the feed counts contracts or lots. If the target-delta strike does not clear the bar, substitute a neighbour one strike away before abandoning the trade.

Substitution direction errs toward safety on both legs: **the short moves outward** (further OTM — less risk, less credit), **the wing moves inward** (closer to the money — more protection, more cost). Nearest-first alternation was rejected because it can widen the spread on both legs at once, which is the one substitution that increases max loss. Every substitution is recorded per trade so Phase 1 can report how often the fallback fires and whether results depend on it.

### Decision B — build in isolation; live routing deferred

Kronos is developed standalone with no visibility into any other strategy. Live routing is a later conversation.

Recorded so it isn't re-derived: **a routed Leto slot is verified impossible.** `leto.py:344-367` resumes any strategy with an open state file **unconditionally and first**, and `leto.py:624-630` runs exactly one strategy per session, blocking until it returns. A Kronos state file in that chain would pin the router for three to four weeks per cycle and silence Artemis, Athena and Iris entirely.

When live integration is picked up, the realistic options are a background thread inside the Leto process (sharing the single AngelOne session and `websocket_feed.py`), or a pre-routing management step — **not** a routing slot. A separate cron with its own login is likely disqualified: a second AngelOne login on the same client code would probably invalidate Leto's session token, and it doubles consumption of shared rate limits.

### Decision C — no gating; must work across all VIX regimes

No VIX filter, no capital allocation, no regime conditioning. Kronos has to earn its keep unconditionally.

VIX **is still recorded per trade** as instrumentation — that is measurement, not gating, and it is what makes any later regime analysis possible without a re-run. Nothing in the entry or exit path may read it.

---

## 5. Core design

- **Instrument**: Nifty monthly expiry, NFO. Lot size 65. Fixed notional sizing for the backtest — real allocation is deferred with Decision B.
- **Structure**: **defined-risk iron condor** — short CE and PE with long wings — not a naked strangle. An undefined-risk short strangle carried for three weeks produces a P&L distribution whose tail an 87-contract sample cannot characterise; defined risk keeps the backtest's worst case bounded and knowable, which matters more than the extra credit. It also keeps margin efficient for whenever allocation is revisited.
- **Entry**: DTE-based, not day-of-week — enter when the front monthly reaches DTE ∈ [30, 35]. Time-of-day fixed at 10:30, matching Athena's convention and avoiding the open's noise. No VIX condition (Decision C). *Revised from [40, 50] by the Phase 0 feasibility measurement in §3 — at 45 DTE the chain will not supply a wing on both sides for two-fifths of the sample.*
- **Strike selection**: target delta, computed with `mibian` as the rest of the repo does. Starting point ~0.15 delta short, ~0.075 long wings — both are Phase 3 sweep parameters, not settled values. *Wing revised from 0.05 by §3; the short is unconstrained by liquidity and is the lever for widening the band if credit proves thin.*
- **Management** — all parameters, all swept in §6, none of them folklore to be trusted:
  - Profit target as a fraction of credit received (start 50%).
  - Loss exit at a multiple of credit received (start 2×).
  - Time exit per `EXIT_POLICY` (Decision A).
- **ELM**: does not arise under E1 or E2 — both close before expiry day and before T−1. Under E3 it becomes mandatory and non-negotiable, and is never a tuning lever. This asymmetry is itself part of what Phase 4 measures: E3 must out-earn E1/E2 by enough to justify taking on a regulatory handling path the other two avoid entirely.
- **Concurrency**: single slot, never two positions open (Decision D). On collision, defer to the first trading day after the previous exit if at least 21 DTE of runway remains, else skip the cycle.
- **Liquidity**: a leg is only taken at a strike that traded at least `MIN_LIQUIDITY_VOLUME` over a 30-minute window ending at the decision time, across at least `MIN_LIQUIDITY_BARS` distinct minutes. If the target-delta strike fails, substitute one strike outward (short) or inward (wing). No substitution, no trade.
- **CAS hard rules, under every exit policy**:
  - Never mark, value, or exit off option LTP between **15:16 and 15:29** (§5.3 — the Sensex 78,800 CE spiked to ~2× its 15:15 reference at 15:23 and crashed back by 15:29; that is derivatives-layer instability, not a real price).
  - No entries or exits inside the auction window at all.
  - Any stop referencing the index must use a continuously-traded reference, never a post-15:15 print.

---

## 6. Backtest design

Repo conventions are binding: every parameter in `kronos_backtest/configs.py`, function-based (no classes) in the backtest layer, **one variable changed per experiment**. Reuse `athena_backtest`'s loader pattern — it already handles monthly-expiry legs and the same `data_pipeline/data/nifty/options/` layout — rather than writing a new loader.

Universe: 87 Nifty monthly contracts, 2019-05-30 → 2026-07-28; 80 fillable at the chosen 35-DTE / 0.15Δ / 0.075Δ settings with the liquidity filter on, and a 55-contract fixed set for the Phase 2 sweep (§3). This spans COVID (2020), the 2021–22 rate cycle, and the 2024–25 regime. A short-vega strategy will look very different across those, so year-by-year breakdown is mandatory, not optional — headline aggregates over a 2019-start sample would flatter or damn the strategy for regime reasons alone.

| Phase | Question | Variable |
|---|---|---|
| 0 | Loader, delta computation, monthly identification, and both E2 date rules correct? | — (validation) |
| 1 | Does a naive baseline clear costs at all? **Kill gate.** | fixed 35 DTE, 0.15Δ / 0.075Δ, 50% target, E2b |
| 1b | Does daily-close management lose to intraday? | management cadence |
| 2 | Best entry point on the curve — **fixed contract set** of 60 (§3) | entry DTE, 30–45 |
| 3 | Best strike distance, and whether a wider band fixes thin credit | sold delta (wing is liquidity-capped) |
| 4 | **Which exit policy wins**, on return per unit of deployed capital-time | `EXIT_POLICY` ∈ {E1, E2a, E2b, E3}, then E1's DTE sub-sweep |
| 5 | Where to take profit | profit target % |
| 6 | Where to cut | loss multiple |

**Phase 0 is built and passing** (`kronos_backtest/`, 25 checks, run 2026-08-17). It verifies the things that fail silently: holidays load as dates and the calendar helpers actually skip them; monthlies are identified by calendar rule rather than lead time; entry and all four exit dates are trading days, correctly ordered, with E2b flat over the final weekend and never inside ELM on T−1 across all 19 holiday-shifted windows; and the chain can actually supply the legs at entry. It found the §3 lead-time artifact, the §4 Wednesday-row error, and a price-staleness bug — the standard repo lookup falls back to the last print before the timestamp with no age bound, which for a far-OTM wing manufactures both an impossible fill and an IV backed out of a dead quote. `kronos_backtest/loader.py` bounds it; other backtests in the repo do not.

Phase 4 is the decision phase. E2b is the baseline throughout Phases 1–3 so the earlier sweeps aren't conditioned on an exit policy that later loses; E1, E2a and E3 are then run against the Phase 2–3 winners.

**The Phase 4 metric is return on deployed capital per unit time, not P&L per trade.** Decision D's single-slot rule makes this mandatory rather than a refinement: E1 exits three weeks early and leaves the slot idle for 46% of the calendar, while E3 is oversubscribed and can only run at all by deferring every entry. A per-trade comparison would flatter E3 — which holds longest and captures most decay — while hiding that it forecloses the next cycle's entry. Report per-trade P&L alongside, but decide on the capital-time metric.

Two further asymmetries stand outside any metric: E3 carries an unquantifiable CAS tail (§5.2, no CAS-era monthly data) and a mandatory ELM handling path that E1 and E2 avoid entirely. It needs a clear margin, not a nominal one, to be worth choosing.

Phase 1 must therefore implement, from the start: the single-slot deferral against actual exits, `entry_dte_target` vs `entry_dte_realised` per trade, the liquidity filter with its substitution counter, and an explicit skip path for the five contracts the chain cannot fill (§3).

Deferred with Decision B: portfolio-interaction analysis via `leto_backtest/`. Worth noting for whenever that happens — `leto_backtest/` currently simulates a *routed* portfolio and would need extending to model a concurrently-held book.

---

## 9. Phase 1 result — 2026-08-17

Run at the settled baseline: 35 DTE entry, 0.15Δ short, 0.075Δ wing, 50%-of-credit profit target, 2× loss exit, E2b time exit, single slot, liquidity filter on, one lot. Kill-gate thresholds were written into `configs.py` before the run.

**77 of 87 contracts traded.** Ten skipped, all for want of a liquid leg — five CE wings, two PE wings, two CE shorts, one PE short. All seven contracts Phase 0 predicted unfillable were among them, plus three more: 2020-05-28, 2022-04-28 and 2025-04-30.

**Phase 0's feasibility figure is an upper bound, and now we know by how much.** It asked whether *any* liquid strike at or below the target delta exists in the chain; the engine asks whether the *first* strike at or below the target delta — or its one-step neighbour — is liquid. The second is the real test, because that is the strike the strategy actually wants. So the honest fillable count at 35 DTE is 77/87 (89%), not 80/87 (92%). Still above the 90% gate on Phase 0's measure and just below it on the engine's; either way it is not what killed the strategy. Worth carrying forward: any future feasibility scan should use the engine's selection path rather than a min-delta scan.

| | |
|---|---|
| Total P&L, 1 lot | **Rs 12,084** over seven years |
| Median trade | Rs 1,534 |
| Mean trade | Rs 157 |
| Win rate | 61/77 = 79% |
| Best / worst trade | Rs 4,732 / **Rs −12,564** |
| Median credit | 61 points on a 500-point width |
| Median capital at risk | Rs 28,096 |
| Deployment | 49% of the calendar |
| Capital reserved (one position's defined risk) | Rs 56,810 |
| Annualised return **while deployed** | 12.5% |
| Annualised return **on committed capital** | **3.0%** |

**Two returns, and the second is the one that counts.** 12.5% is what the structure earns during the 49% of the calendar it is actually on. But margin is reserved for the whole span whether or not a position is open — single slot means one position's defined risk, Rs 56,810, sits committed throughout. On that basis the strategy returns **3.0% annualised**, below a fixed deposit. Decision D's single-slot rule is a statement about idle time, so this is the number Phase 4 must compare policies on; `while_deployed` would reward a policy for sitting out.

**The cost structure is the story.** Gross P&L before slippage is Rs 52,124 — Rs 677 a trade. At the configured one point per leg, the eight applications of a four-leg round trip take Rs 520 of that, leaving Rs 157. The strategy breaks even at **1.30 points of per-leg slippage**:

| Slippage | Total P&L | Per trade |
|---:|---:|---:|
| 0.00 | Rs 52,124 | Rs 677 |
| 0.50 | Rs 32,104 | Rs 417 |
| **1.00 (configured)** | **Rs 12,084** | **Rs 157** |
| 1.50 | Rs −7,936 | Rs −103 |
| 2.00 | Rs −27,956 | Rs −363 |

The result's sign is decided by execution quality, not by the decay curve. That is the finding.

**Kill gate:**

| Criterion | Result | |
|---|---|---|
| Median trade P&L positive | Rs 1,534 | PASS |
| Gross P&L ≥ 2× the slippage bill | 1.30× | **FAIL** |
| Annualised return on committed capital positive | 3.0% | PASS (barely) |
| No year contributes more than 60% of P&L | 150% | **FAIL** |

The year concentration is not a rounding artifact: 2025 alone made Rs 18,128, more than the entire cumulative total, because 2020 (−12,324) and 2023 (−8,733) were negative. Seven of eight years are individually small relative to the noise.

**Three observations that should inform whatever comes next**, none of them acted on:

1. **The losses are not a volatility story.** Median entry VIX at the thirteen loss exits was 13.9 against 15.2 across all trades — the stops fired in *calm* markets, on directional drift, not on vol spikes. The usual short-premium intuition does not describe this failure mode, and a VIX gate would not have helped (which is also Decision C working as intended).

2. **The engine behaved correctly.** No trade exceeded its defined risk; profit exits landed at the target less the exit slippage (Rs 1,740 realised against a Rs 2,009 trigger); loss exits landed just past the stop. Only 1.6% of minutes were excluded as stale marks, so the loss exit was genuinely tested.

3. **The gate it passed, it barely passed.** A positive return on committed capital was set as the bar, and 3.0% clears it arithmetically. It should not be read as a pass in substance — that is below cash, for a strategy carrying a Rs 12,564 worst trade against Rs 56,810 of reserved margin. The threshold was set at zero before the run and it stays at zero; the honest reading is that three of four criteria are unsatisfying and two are outright failures.

4. **Deferral cost almost nothing.** One entry of 77 was deferred, against the 65% scheduled collision rate — because 60 of 77 trades exited early on the profit target and freed the slot. The single-slot rule is close to free under E2b, which is worth knowing before Phase 4 compares it against E1 and E3.

**The one pre-registered remedy.** §3 recorded, before any P&L existed, that a 0.15Δ/0.075Δ band would be thin on credit and that widening it via a **closer short (0.20–0.25Δ)** is the lever to try before abandoning the structure — the liquidity constraint binds on the outer leg alone, so the short can move without costing availability. A 61-point credit against a 500-point width is exactly the thinness that was predicted. Running that test is not tuning a failed result; it was named in advance. But it is the *only* pre-registered move, and if it does not clear the gate the honest answer is that monthly short premium on Nifty does not survive four-leg execution costs.

---

## 7. Risks and failure modes

1. **Pre-CAS calibration, permanently.** §3. E1 and E2 mitigate it structurally by never reaching the auction; E3 does not, and cannot be measured for it.

2. **Small sample — but no coverage hole.** 87 contracts sounds like a lot; it is ~12 observations a year over ~7 years, which is *small* for characterising tail behaviour in a short-premium strategy. Report year-by-year and treat any full-span Sharpe or Calmar with suspicion. The stronger version of this risk in the earlier revision — that 2020, the regime a short-vega drawdown would be dominated by, was nearly absent from the evidence — **is withdrawn**: it rested on the lead-time artifact corrected in §3. All twelve 2020 monthlies are in the sample with 87–90 days of coverage each. Only 2025-04-30 (40 days) is coverage-limited, and only above 40 DTE.

3. **The whole thing may not clear costs.** Real possibility that the slower part of the decay curve, after slippage on four legs, is not worth trading at all. Phase 1 is a kill gate, not a formality — be willing to stop there.

4. **Four-leg slippage on monthly strikes.** Monthly-expiry OTM strikes are thinner than weekly ATM. Model slippage explicitly from `volume`/`open_interest` in the data rather than assuming a flat per-leg cost. With four legs and a 50%-of-credit profit target, slippage assumptions can flip the sign of the result.

5. **No VIX gate means 2020 and 2022 sit in the sample at full weight** (Decision C, deliberate). A short-premium strategy that survives them unconditioned is genuinely robust; one that only works with a VIX filter bolted on afterwards is curve-fitted. Resist adding the filter to rescue a bad result.

6. **The single-slot rule and the exit policy are entangled, and the entanglement is not a free parameter.** Decision D was taken for capital efficiency, but it silently converts Phase 4 from a P&L question into a scheduling one — E1's 46% deployment and E3's 115% oversubscription are properties of the calendar, not of the strategy's edge. A policy could win on capital-time purely by occupying the slot more, while earning less per unit of risk. Report per-trade P&L, deployment share and capital-time return together, and do not let one of the three carry the decision alone.

7. **Deferred, not solved**: margin contention with the live book, and concurrency degrading live reliability (shared AngelOne rate limits are an existing known hazard). Neither is a backtest risk, but neither disappears — both return with Decision B.

---

## 8. When to call back

- ~~**After Phase 0**~~ — **done 2026-08-17.** Identification and both E2 rules verified across all 87 contracts. Three corrections resulted: no 2020 coverage hole (§3), E2 divergence is 86% not 83% (§4), and entry DTE / wing delta moved to 35 / 0.075Δ on feasibility grounds (§3). Confirm those two default changes are acceptable before Phase 1 runs.
- ~~**After Phase 1**~~ — **run 2026-08-17. VERDICT: FAIL.** See §9. The baseline earns Rs 12,084 over 77 trades on one lot, breaks even at 1.30 points of per-leg slippage, and 2025 alone out-earns the whole seven-year sample. Two of the four pre-registered kill-gate criteria are not met. Pari's call whether the one pre-registered remedy in §3 (a wider band via a closer short) is worth running before stopping.
- **After Phase 4**: exit-policy verdict. This is the substantive strategy decision.
- **After Phase 6**: go/no-go on a production build, which reopens Decision B.
- **Before any live wiring**: confirm the routing and override state at that time. Per §1 of the feeder, the current Iris force-route is a Slack override rather than a code change, and CLAUDE.md's "Artemis DISABLED" is documentation rather than a code state.

Next concrete step: Pari's call — run the §3 pre-registered wider-band test (0.20–0.25Δ short), or stop.

---

*2026-08-10, revised 2026-08-17 by Phase 0 and by Decision D. Instrument choice (§3) settled by audited data. Scope and exit policy (§4) settled by Pari. Structure and management parameters (§5) are starting points for the §6 sweeps; entry DTE and wing delta have already moved once, on measurement rather than preference.*
