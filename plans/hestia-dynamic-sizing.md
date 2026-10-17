# Hestia dynamic sizing: per-engine share of total account margin

Status: design only, written 2026-10-17. Nothing is built. Dynamic sizing is off for all four engines (`static_units` in `hestia_config.py`, `TRADING_HOSTS['delos']`); Monday 19 Oct runs 2 units each, cap 100.

## 1. The rule (owner, 2026-10-17)

For each engine on its own: (1) read the account's TOTAL margin, not the available margin; (2) divide it by 4, which is that engine's share; (3) divide the share by the engine's own margin per unit and round down. Units = floor((total margin / 4) / margin_per_unit), then limited by the unit cap.

The reason for the redesign: the old dynamic rule was written for Prometheus alone (`max(1, available cash // margin per unit)`, optionally limited by `allocation_rs`). Four engines now share one Angel One account. Every engine reads the same available cash, nothing divides it between them, and available cash shrinks whenever another engine holds a position, so an engine's size would depend on what the others happen to be doing.

## 2. What exists today

Each engine's `_current_units()` (`prometheus_engine/engine.py:193` and the same method in the other three) returns `static_units` when dynamic is off. When dynamic is on it returns `max(1, min(cash // margin_per_unit, unit_cap))`, where cash is `ctx.margin().available_cash` limited by `allocation_rs` if one is set, and it falls back to the static figure with a warning if anything fails. The cash comes from `AngelBrokerPort.free_cash()` (`hestia_core/angel_broker.py:99`), which reads only `availablecash` from `rmsLimit()` every `cash_refresh_s` and fails closed (returns 0) when the reading is older than `cash_max_age_s` (120 s). Hestia's core separately refuses an entry that exceeds `unit_cap x lots_per_unit` (`hestia_core/core.py:605`) or whose margin exceeds available cash; that second check uses the generic `price x lot_size / 2` per lot because `CORE` in `hestia_config.py` sets no `margin_per_lot`. Each engine also has its own affordability check, `_margin_sufficient`, which uses the engine's own `margin_per_unit`.

## 3. The margin call

`rmsLimit()` (the `getRMS` endpoint, `SmartApi/smartConnect.py`) is the only margin call in the SDK, and Hestia already uses it through `gateway.rms()`. The response has no field called "total margin". A minimal login probe on Saturday 2026-10-17 15:20 (credentials file, one `generateSession`, one `rmsLimit()`, `terminateSession`; Hestia was not running) returned:

| Field | Value (Rs) |
|---|---|
| availablecash | 50,89,326.23 |
| net | 50,89,326.23 |
| collateral | 51,44,319.20 |
| utiliseddebits | 1,09,751.50 |
| utilisedpayout | 66,160.61 |
| availableintradaypayin, availablelimitmargin, m2mrealized, m2munrealized | 0 |
| utilisedspan, utilisedexposure, utilisedoptionpremium, utilisedholdingsales, utilisedturnover | None |

The four live engines each held one unit at the time, which is consistent with `utiliseddebits` being the margin in use (about Rs 1.1 lakh).

## 4. What "total margin" is, and the settlement

Definition (owner, 2026-10-17): **total margin = `availablecash` (the same as `net`) + `availableintradaypayin` + `utiliseddebits`**. `availablecash` is the free cash; `availableintradaypayin` is money that has been paid in but is not yet in `availablecash` (the owner expects it to be 0 on most days, because it is added to `availablecash` within a day); `utiliseddebits` is the margin actually in use by the open positions. So the total is the account's funds with no margin in use, and it should not change as positions open and close, since opening a position moves money from `availablecash` into `utiliseddebits`.

It matches the owner's total account value on the reading taken after the quarterly-settlement money (Rs 1,21,153.58, bank SMS the same day) was transferred back, 2026-10-17 15:37: 52,10,479.81 + 1,21,153.58 + 1,09,751.50 = **Rs 54,41,384.89**. The same afternoon's earlier reading (before the transfer) gave 50,89,326.23 + 0 + 1,09,751.50 = Rs 51,99,077.73; that is lower because the settlement amount was still outside the account (in the bank, not in any API field), which the rule rightly cannot see. The Rs 53,20,231.31 the owner expected then was the account plus that money in transit.

Fields that are NOT part of the total: `collateral` (51,44,319.20, unchanged by the transfer) and `utilisedpayout` (66,160.61 before the transfer, 1,87,314.19 after; a settlement item that rose by exactly the transferred amount). Two earlier versions of this plan took `availablecash` + `utiliseddebits` and then `collateral` + `utiliseddebits` + `utilisedpayout` as the total; both were fitted to single readings and both are wrong or coincidental, corrected the same day.

Still to confirm before code reads it: that the sum stays constant between a reading with the engines flat and one with positions open (`availablecash` down by what `utiliseddebits` goes up by); and that on an ordinary day `availableintradaypayin` is 0 and the sum still equals the owner's total on the broker's side. A reading before the 08:55 start and one with positions open settles both; a once-per-session log of the whole response inside the existing cash refresh would do it with no extra login. Note for the sizing rule: while money is in transit (a settlement or withdrawal in flight) the total, and so each engine's share, is temporarily lower.

## 5. Margin per unit: the engines are on different bases

| Engine | Formula (per unit) | Basis | Rs per unit at Friday's close |
|---|---|---|---|
| Prometheus | price x lot size / 3 x 4 | house formula, includes the x4 | 1,25,173 |
| Selene | price x lot size / 8 x 4 | house formula, includes the x4 | 1,07,000 |
| Helios | price x lot size x 0.0927 x 20 lots | one observed 1-lot quote (Rs 1,380 at 14,893), NO x4 | 27,344 |
| Typhon | price x lot size x 0.1869 x 2 lots | one observed quote (Rs 14,000 at 299.7), NO x4 | 27,897 |

Finding (owner, 2026-10-17): the x4 safety factor is missing from Helios and Typhon. The Helios plan (`plans/helios-goldpetal-st-strategy.md` section 4h area, line "Margin divisor, derived from one real observed point") recorded the ratio directly "rather than forced into" the divide-and-multiply shape and flagged it as a single-point derivation to re-check before real sizing; Typhon's config comment says the same. The x4 was never added. Applied to the rule, the missing factor is what makes Helios and Typhon come out at 48 and 47 units against 10 and 12 for the other two.

With x4 on Helios and Typhon (Rs 1,09,376 and Rs 1,11,588 per unit), a share of Rs 13,60,346.22 (Saturday's Rs 54,41,384.89 / 4) gives Prometheus 10, Selene 12, Helios 12, Typhon 12 units (without the x4: Helios 49, Typhon 48). Indicative only: prices move, and the total will move with the account.

## 6. Proposed work (nothing started; needs the owner's go-ahead)

1. DONE 2026-10-17 (uncommitted at the time of writing): the x4 is now inside `margin_sizing_multiplier` for Helios and Typhon (`4 * 1380 / 14893` and `4 * 14000 / (299.7 * 250)`), in the engine configs and in the matching backtest configs (recorded values, no script reads them), with the Helios pin test updated and a Typhon pin added; the four engine test files pass (96). The separate safety-factor field described next was not done. Original item: add the x4 to Helios and Typhon. Preferred shape: a separate config field for the safety factor (4.0 for all four engines) rather than editing the multiplier, so the factor is visible and the same everywhere. Pin it in the engine config tests. Note the effect on today's static sizing: `margin_per_unit` also feeds each engine's entry affordability check (`units x margin_per_unit` against free cash). At 2, 5 and 10 units that is about Rs 2.2, 5.5 and 11 lakh per engine; all four engines in a trade at 10 units needs about Rs 44 lakh against a balance of about Rs 51 lakh.
2. Re-probe `rmsLimit()` on an ordinary weekday with Hestia stopped (before the 08:55 start), once the settlement has been transferred back; then again with positions open (a read-only logging of the whole response once per session inside the existing cash refresh is enough, no extra login). Confirm the definition in section 4.
3. Build `total_margin()` beside `free_cash()` in `AngelBrokerPort` (and the paper and replay brokers), failing closed on a stale reading, and set each engine's share to total / 4 (this feeds `allocation_rs`, or replaces the cash term in `_current_units`).
4. Keep the existing refusals: unit cap, roll window, available-cash admission, and each engine's `_margin_sufficient`.
5. Update `hestia_core/README.md` in the same commit; tests for the share, rounding down, the stale reading, and the zero-unit case.

## 7. Open decisions

- Zero units: if the share is below one unit's margin, skip the entry or take 1 unit (today's code forces a minimum of 1).
- Divisor: fixed at 4, or the number of enabled live engines (so a paused engine's quarter is not idle).
- Unit cap: keep it as a per-engine ceiling. At the owner's schedule the cap, not the margin rule, is what holds an engine to its planned units.
- Whether dynamic sizing replaces the 2 / 5 / 10 static schedule or only comes after it.

## 8. Context: the scaling schedule

Static units, set by the owner: 2 units the week of 19 Oct, 5 units the week of 26 Oct, 10 units the week of 2 Nov, then a decision on further scaling. See `plans/hestia-p6-cutover.md` section 6 for the original cap and the memory note on the scaling plan. Dynamic sizing is not part of this schedule until the owner decides.
