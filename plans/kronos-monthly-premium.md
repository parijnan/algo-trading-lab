# Plan: Kronos — Monthly Short-Premium, Expiry-Avoidant

**Codename: Kronos** (Titan of time — the strategy's entire thesis is which slice of the decay curve it harvests, and which one it deliberately refuses).

**Status: Design settled for a standalone backtest.** Scope decisions taken by Pari on 2026-08-10 (§4): build in isolation, no VIX gating, exit policy compared across arms rather than chosen. Live routing and capital allocation are explicitly deferred, not pending. No code exists yet — next step is `kronos_backtest/`.

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
| **Nifty** (`nifty/options/`) | **87** | 2019-05-30 → 2026-07-28 | ~89 days (but see below) | 1-min OHLC + `volume` + `open_interest` |
| **Sensex** (`sensex/`) | 23 | 2024-10-31 → 2026-08-06 | **14–27 days** | 1-min OHLC + `volume` only |

Supporting series: `indices/nifty.csv` and `indices/india_vix.csv`, 1-min, 696k rows each, 2019-01-28 → 2026-08-07. Both cover the full Nifty option history with room to spare.

**This settles the instrument question on its own, before CAS enters the argument.** Sensex monthly contracts carry only 14–27 days of data before expiry — *less than the DTE at which this strategy would enter*. A 45-DTE Sensex entry cannot be backtested because the data does not exist at entry time; a 30-DTE entry barely can, on some contracts. Sensex option files also lack `open_interest`, ruling out OI-based strike or liquidity filters.

CAS reinforces the choice rather than driving it: §5.1's overnight-gap-through-stop mechanism (BSE's closing auction producing **zero trades** for major constituents across 18 of 18 stock-days tested, leaving Sensex's close stale and dumping the information into the next open) is a Sensex-specific live-hold risk, and §6 of the feeder confirms there is no BSE constituent-level auction feed to monitor it with. A strategy that holds for three weeks is maximally exposed to exactly that.

**Nifty only. Not close.**

### Usable universe shrinks with entry DTE — and 2020 is the hole

Lead time is *not* uniform at ~89 days. Measured per contract, the universe available for a given entry DTE is:

| Entry DTE | Contracts with enough history |
|---|---|
| 60 | 57 / 87 |
| 50 | 73 / 87 |
| **45** | **74 / 87** |
| 40 | 78 / 87 |
| 30 | 81 / 87 |

Six contracts are unusable even at 30 DTE — **2020-03-26** (lead −1 day), **2020-05-28**, **2020-07-30** (lead 2), **2020-08-27**, **2020-09-24**, **2020-11-26**. Every one is in 2020. The COVID crash and its aftermath — precisely the regime that would dominate a short-vega strategy's drawdown, and the one Decision C's no-gating stance most needs tested — is the worst-covered part of the sample. This materially weakens any claim of regime robustness and is carried as risk §7.2.

**Methodological consequence for Phase 2**: the entry-DTE sweep cannot simply run each DTE over whatever contracts support it. Comparing 60 DTE on 57 contracts against 30 DTE on 81 is confounded by sample composition, not a controlled test. The sweep must run over a **fixed contract set** — the intersection across all DTEs tested — with the full-universe result reported separately. This follows directly from the repo's one-variable-per-experiment rule.

Monthly contracts must be identified by **calendar rule** (last expiry of the month, trailing incomplete month excluded), *not* by a lead-time threshold. A lead-time filter would silently discard ~30 genuine monthlies that merely have short data history, including most of 2020 — the exact contracts whose absence matters most.

Even so, Nifty's lead time supports a real 30–60 DTE sweep. Sensex could not support that sweep even if everything else were equal.

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

| Expiry weekday | Contracts | E2a (T−2 trading days) | E2b (last trading day before the final weekend) |
|---|---|---|---|
| **Thursday** | **72 / 87 (83%)** | Tuesday of expiry week — **holds the final weekend** | preceding Friday (T−4) |
| Tuesday | 10 | Friday ✓ | Friday ✓ |
| Wednesday | 4 | Friday ✓ | Friday ✓ |
| Monday | 1 | Thursday ✓ | Thursday ✓ |

For 83% of the sample the two readings **disagree**, and E2a contradicts the stated rationale there — it would hold short through exactly the weekend the rule exists to avoid. E2b matches the rationale everywhere, but costs two extra days of decay in the Thursday era. They coincide for every Tuesday and Monday expiry, so **live behaviour is identical either way** — this is purely a backtest-interpretation issue.

**Resolved: run both as sub-arms.** Implemented as a `configs.py` parameter, `EXIT_OFFSET_MODE` ∈ {`trading_days` (E2a), `avoid_final_weekend` (E2b)} — a date function, not a second code path, so the cost is negligible. Both are compared in Phase 4.

Phases 1–3 still need a single baseline: **use E2b**, because it is the reading consistent with the stated rationale in every regime, so the earlier sweeps are conditioned on an exit rule that means what it says. If Phase 4 shows E2a wins, re-run the Phase 2–3 winners under it before concluding — cheap, and the alternative is a silently confounded result.

Either reading resolves against `data_pipeline/config/holidays.csv` (117 holidays, 2019 onward, covering the full backtest range) rather than by weekday arithmetic. Weekday logic would silently mishandle mid-week holidays.

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
- **Entry**: DTE-based, not day-of-week — enter when the front monthly reaches DTE ∈ [40, 50]. Time-of-day fixed at 10:30, matching Athena's convention and avoiding the open's noise. No VIX condition (Decision C).
- **Strike selection**: target delta, computed with `mibian` as the rest of the repo does. Starting point ~0.15 delta short, ~0.05 long wings — both are Phase 3 sweep parameters, not settled values.
- **Management** — all parameters, all swept in §6, none of them folklore to be trusted:
  - Profit target as a fraction of credit received (start 50%).
  - Loss exit at a multiple of credit received (start 2×).
  - Time exit per `EXIT_POLICY` (Decision A).
- **ELM**: does not arise under E1 or E2 — both close before expiry day and before T−1. Under E3 it becomes mandatory and non-negotiable, and is never a tuning lever. This asymmetry is itself part of what Phase 4 measures: E3 must out-earn E1/E2 by enough to justify taking on a regulatory handling path the other two avoid entirely.
- **CAS hard rules, under every exit policy**:
  - Never mark, value, or exit off option LTP between **15:16 and 15:29** (§5.3 — the Sensex 78,800 CE spiked to ~2× its 15:15 reference at 15:23 and crashed back by 15:29; that is derivatives-layer instability, not a real price).
  - No entries or exits inside the auction window at all.
  - Any stop referencing the index must use a continuously-traded reference, never a post-15:15 print.

---

## 6. Backtest design

Repo conventions are binding: every parameter in `kronos_backtest/configs.py`, function-based (no classes) in the backtest layer, **one variable changed per experiment**. Reuse `athena_backtest`'s loader pattern — it already handles monthly-expiry legs and the same `data_pipeline/data/nifty/options/` layout — rather than writing a new loader.

Universe: 87 Nifty monthly contracts, 2019-05-30 → 2026-07-28; 74 usable at a 45-DTE entry (§3). This spans COVID (2020), the 2021–22 rate cycle, and the 2024–25 regime. A short-vega strategy will look very different across those, so year-by-year breakdown is mandatory, not optional — headline aggregates over a 2019-start sample would flatter or damn the strategy for regime reasons alone.

| Phase | Question | Variable |
|---|---|---|
| 0 | Loader, delta computation, monthly identification, and both E2 date rules correct? | — (validation) |
| 1 | Does a naive baseline clear costs at all? **Kill gate.** | fixed 45 DTE, 0.15Δ, 50% target, E2b |
| 1b | Does daily-close management lose to intraday? | management cadence |
| 2 | Best entry point on the curve — **fixed contract set** (§3) | entry DTE, 30–60 |
| 3 | Best strike distance | sold delta |
| 4 | **Which exit policy wins** | `EXIT_POLICY` ∈ {E1, E2a, E2b, E3}, then E1's DTE sub-sweep |
| 5 | Where to take profit | profit target % |
| 6 | Where to cut | loss multiple |

Phase 0 must explicitly verify two things that fail silently if wrong: monthly contracts identified by calendar rule rather than lead time (§3), and both `EXIT_OFFSET_MODE` readings producing the correct date for every expiry weekday present in the sample, including holiday-shifted ones.

Phase 4 is the decision phase. E2b is the baseline throughout Phases 1–3 so the earlier sweeps aren't conditioned on an exit policy that later loses; E1, E2a and E3 are then run against the Phase 2–3 winners. The comparison is not raw P&L — E3 carries an unquantifiable CAS tail (§5.2, no CAS-era monthly data) and an ELM handling requirement, so it needs a clear margin, not a nominal one, to be worth choosing.

Deferred with Decision B: portfolio-interaction analysis via `leto_backtest/`. Worth noting for whenever that happens — `leto_backtest/` currently simulates a *routed* portfolio and would need extending to model a concurrently-held book.

---

## 7. Risks and failure modes

1. **Pre-CAS calibration, permanently.** §3. E1 and E2 mitigate it structurally by never reaching the auction; E3 does not, and cannot be measured for it.

2. **Small sample, and the worst-covered year is the one that matters most.** 87 contracts sounds like a lot; it is ~12 observations a year over ~7 years, which is *small* for characterising tail behaviour in a short-premium strategy — and only 74 are usable at a 45-DTE entry. Worse, **all six contracts with too little history to use at any entry DTE are in 2020** (§3), including the COVID-crash monthly itself (2020-03-26, lead −1 day, no usable pre-expiry history at all). The regime that would dominate a short-vega drawdown is close to absent from the evidence. Report year-by-year, treat any full-span Sharpe or Calmar with suspicion, and do not claim regime robustness on this sample — the evidence for the case that matters most simply isn't there.

3. **The whole thing may not clear costs.** Real possibility that the slower part of the decay curve, after slippage on four legs, is not worth trading at all. Phase 1 is a kill gate, not a formality — be willing to stop there.

4. **Four-leg slippage on monthly strikes.** Monthly-expiry OTM strikes are thinner than weekly ATM. Model slippage explicitly from `volume`/`open_interest` in the data rather than assuming a flat per-leg cost. With four legs and a 50%-of-credit profit target, slippage assumptions can flip the sign of the result.

5. **No VIX gate means 2020 and 2022 sit in the sample at full weight** (Decision C, deliberate). A short-premium strategy that survives them unconditioned is genuinely robust; one that only works with a VIX filter bolted on afterwards is curve-fitted. Resist adding the filter to rescue a bad result.

6. **Deferred, not solved**: margin contention with the live book, and concurrency degrading live reliability (shared AngelOne rate limits are an existing known hazard). Neither is a backtest risk, but neither disappears — both return with Decision B.

---

## 8. When to call back

- **After Phase 0**: confirm monthly identification and both E2 date rules resolve correctly for all 87 contracts, including holiday-shifted and non-Thursday expiries. Cheap to check, expensive to get wrong silently.
- **After Phase 1**: kill-gate review — does the baseline clear costs? Be willing to stop.
- **After Phase 4**: exit-policy verdict. This is the substantive strategy decision.
- **After Phase 6**: go/no-go on a production build, which reopens Decision B.
- **Before any live wiring**: confirm the routing and override state at that time. Per §1 of the feeder, the current Iris force-route is a Slack override rather than a code change, and CLAUDE.md's "Artemis DISABLED" is documentation rather than a code state.

Next concrete step: build `kronos_backtest/` — `configs.py`, loader adapted from `athena_backtest`'s monthly-leg handling, and Phase 0 validation.

---

*2026-08-10. Instrument choice (§3) settled by audited data. Scope and exit policy (§4) settled by Pari. Structure and management parameters (§5) are starting points for the §6 sweeps.*
