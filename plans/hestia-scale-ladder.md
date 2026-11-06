# Hestia scale ladder: gated, one-unit-at-a-time sizing

Status: design only, drafted 2026-11-06 for the owner's review. Nothing is built, no config or code is touched. Sizing today is static (`static_units` in the `delos` block of `TRADING_HOSTS` in `hestia_config.py`), changed by hand under the rule written down on 2026-11-03 in the scaling-plan memory. This plan turns that hand rule into code. It sits beside `plans/hestia-dynamic-sizing.md` (the margin rule, built but off) and does not replace it.

## 1. Why

The step to 5 units on 26 Oct met four losing days at once (about -1.6 lakh actual, 169,662 closed-trade drawdown from the 21 Oct peak of +48,955). At 1 unit the same stretch was about -29,500, half the backtest's combined max drawdown (Rs 58,566): the engines behaved normally, the size made it large. The owner's response (3 Nov) was to scale in single units and only when earned. That rule has been applied by hand since: on 4 Nov (QC clean, gates failed, change 4+1 to 3+2 for crude), and on 5 Nov at 23:59 (gate 1 pass, gate 2 fail by about Rs 40,500, nothing changed). Doing it by hand costs a dashboard pull, a manual calculation, a commit, a push, and a Delos pull before 08:55, and it depends on someone remembering to run it.

## 2. Two rules that must not be confused

- The **margin rule** (`hestia_core.sizing.dynamic_units`) is a LEVEL rule: units = floor((total margin / engines sharing) / margin per unit), recomputed at every entry, so it follows account value up and down. Left to itself it compounds and would have taken Helios and Typhon to about 12 units at once.
- The **scale ladder** is a RATCHET: an engine's units rise by exactly one only after it has earned the step.

They answer different questions (what the account can carry, what the engine has earned). So the ladder governs and the margin rule becomes a ceiling:

`units = min(earned_units, margin_ceiling, unit_cap)`

`margin_ceiling` is the existing `dynamic_units` result (or, with dynamic off, a margin-only calculation that never raises anything). The ceiling can only lower the ladder's figure. Precedence over both: a manual Slack sizing override (`<engine>_sizing.json`), which exists today and stays.

## 3. The gates (the owner's rule of 3 Nov, made precise)

Evaluated per engine, once per night (section 5). All must pass for a one-unit step.

1. **Equity near its peak.** The engine's closed-trade equity at actual units (sum of recorded rupees over its live trades) is within a tolerance of its running high-water mark. Open positions are excluded, by design: equity is what has been banked. Proposed tolerance: 10% of the engine's one-unit backtest drawdown times its current units, to be set from the replay (section 8). Reference: 5 Nov 23:59, whole-account closed equity +60,882 against a new peak +61,196, gap 314.
2. **Cushion.** Realised profit since the last step, at actual units, is at least one more unit's backtest drawdown for that engine. Per-engine constants at 1 unit: Prometheus 31,670, Selene 45,917, Helios 26,300, Typhon 39,165, Oleus 53,472 (Janus plain thirds). Sum for the four older engines is 58,566 at the account level, which is the figure the hand rule used. "Realised since the last step" is defined by EXIT date (the day the money is realised). On 5 Nov this was +18,050 by exit date against +9,312 by entry date; the definition has to be fixed once so the two cannot be chosen after the fact. The constants are refreshed whenever `hestia-backtest` is re-run and live in config, not code.
3. **Time floor.** At least 5 trading sessions at the current size, counted from the step date (the 26 Oct step is the origin for the current level). Optionally also a minimum number of closed trades since the step; the replay decides whether that adds anything.
4. **Margin.** The proposed level is at or below `margin_ceiling`, and the total-margin rule holds: with every engine at its proposed level, summed units x margin per unit stays within the account's total margin (about 10 to 12 units each on Rs 53.2 lakh at the 17 Oct per-unit figures).
5. **Roll eve.** No step on a roll-eve session or within a configurable number of sessions before one (crude 18 to 19 Nov, natural gas 24 Nov, silver and gold 30 Nov), using `hestia_core/roll_policy.py` and the trading calendar, per instrument.

A step raises `earned_units` by 1. It resets the cushion baseline (equity and date at the step) and the time clock. At most one step per engine per evaluation, and none on a day the engine is flagged killed or disabled.

## 4. Crude, where two engines share an instrument

Owner's matching rule (3 Nov): Prometheus + Oleus units equal the units Selene, Helios and Typhon run. Units are not lots (Prometheus 2 lots a unit, Oleus 3), so a unit moved between them changes lots by one.

Proposal: treat crude as a group. The pool target is the level the three non-crude engines hold. Config states the split as a fixed Prometheus holding (3 today) with Oleus taking the remainder (pool minus Prometheus). Oleus therefore earns the increases, and Prometheus holds or steps down, as the owner described on 3 Nov. Oleus's own cushion gate uses its Janus drawdown (Rs 53,472 per unit). Open decision D1 covers whether the group keys off the lowest, highest or median of the other three.

## 5. When it runs and where state lives

- **Evaluation:** once, at session end, after the host has flushed the trades CSVs and ledger and before teardown completes (the existing session-report moment). Trades CSVs are Hestia's own files, so no dashboard pull and no broker call are needed. A second evaluation at the start of the session is NOT done: the level must not change between the night's decision and the next morning without a record.
- **State:** `hestia_data/state/<engine>_ladder.json`: `earned_units`, `last_step_date`, `equity_at_step`, `peak_equity`, `peak_date`, `last_evaluated`, and the last gate results (so Slack and QC can show why). Written only by the nightly job, atomically, like the other state files.
- **Reading:** each engine's `_current_units()` (for Prometheus around `prometheus_engine/engine.py:193`, same method in the others and in Oleus; line numbers to be re-checked before work) takes `earned_units` from the ladder file at every new entry, the same live-read pattern as the Slack sizing override. A step therefore takes effect at the next entry with no restart, no commit and no pull before 08:55. Open positions keep their entry units; a carried roll keeps the trade's units, unchanged from today.
- **First value:** seeded from `static_units` in `hestia_config.py` the first time a ladder file does not exist, so turning the feature on changes nothing until a step is earned. `static_units` stays in config as that seed and as the fallback if the ladder file is missing or corrupt (fail to the lower of the two, never up).

## 6. Advise first, auto later

Two modes per engine, set in config:

- **advise:** the nightly job computes everything, writes the gate table to the ladder file and posts it to `#tradebot-updates`, with a proposed step if all gates pass. Nothing changes until the owner confirms with a Slack command (same listener as the sizing override). This is what was done by hand on 4 and 5 Nov.
- **auto:** a passing evaluation applies the step itself and posts what it did.

Default: advise for every engine. Switch to auto only after several weeks in which the nightly proposals matched the owner's own calls. This is the same paper-before-live discipline every engine went through.

## 7. Stepping down (open, the owner's call)

A ratchet that never falls is incomplete, and the 26 to 29 Oct episode is the case for a down rule. It is also a discretionary-feeling intervention, which the owner has ruled out for live trades. The distinction to put to him: this is not overriding a trade, it is a pre-committed size rule evaluated between trades on closed results only. Candidate: drop one unit when closed equity falls below peak by more than that engine's one-unit backtest drawdown (the same quantity that gates the step up). Default in this draft: no automatic step-down; the nightly post flags it ("drawdown from peak X, trigger Y") and the owner decides. D2 below.

## 8. Calibrating it before it touches anything: replay on history

The `hestia-simulation` machinery already sizes the integrated ledger (`hestia_backtest/outputs/integrated_trades.csv`, one unit each, from 3 April 2023) trade by trade with costs and slippage. Add the ladder as an alternative sizing mode there and compare it with the margin rule and with fixed schedules. Questions the replay answers:

- Would the ladder have stepped into the 26 to 29 Oct run, and if not, what would that stretch have cost?
- How fast does it climb? "One full extra-unit drawdown of realised profit" may be so strict that growth lags a fixed schedule badly; if so the cushion fraction, the time floor or the tolerance is mis-set, and the replay shows by how much.
- Sensitivity: tolerance (5%, 10%, 25%), time floor (3, 5, 10 sessions), cushion (0.5, 1.0, 1.5 times one-unit drawdown), exit-date versus entry-date accounting, per-engine versus whole-account evaluation.
- Max drawdown, Calmar, final value and number of steps up and down, against the same trades at fixed 5 units.

Caveat to state with any result: the live record is weeks old, the backtest is gross trades with modelled costs, and a rule tuned on the same history it is judged on will look better than it will be. Pick round values, not optimised ones.

## 9. Build plan (nothing started)

1. `hestia_core/scale_ladder.py`: pure `evaluate(stats, cfg, today) -> Decision` returning the gate results, the new level and the reasons. No file or broker access, so it tests like `dynamic_units`.
2. Stats builder: reads an engine's live trades from `hestia_data/trades/<engine>_trades.csv` from its first live trade id (as `hestia_performance/tracker_config.json` does; paper rows are excluded by the same rule), computes closed equity, peak and realised since the step. Reuse the dashboard's counting rules rather than a second copy.
3. State store `<engine>_ladder.json` and the nightly job at session end; Slack post (names capitalised in Slack, lower-case in files); advise-mode confirm command in `slack_listener.py`.
4. Wire `_current_units()` in the five engines to `min(earned, margin_ceiling, cap)`.
5. Crude group logic (section 4).
6. Config: a `ladder` block per engine in the `delos` entry (mode, one-unit drawdown, tolerance, time floor, enabled), plus the roll-eve lead, pinned by tests like the existing config pins.
7. Docs in the same commit: `hestia_core/README.md` sizing section, this plan's status line, the scaling-plan memory.
8. Replay (section 8) comes BEFORE steps 3 to 6; steps 1 and 2 can be built alongside it since the replay needs the same function.

Tests: each gate passing and failing on the boundary; tolerance and exit-date accounting using the real 5 Nov figures as a fixture (gate 2 fails by about 40,500, gate 1 passes at a gap of 314); a step resets baseline and clock; no step on or just before a roll eve; ceiling lowers but never raises; missing or corrupt ladder file falls to the lower of ladder and static; manual override wins; crude group totals; advise mode changes no units; down trigger flags only.

## 10. Risks and what this draft has not verified

- The nightly job must not run on a day the host crashed mid-session or before the CSVs are flushed; it should refuse on a missing or partial day and say so rather than evaluate on stale data.
- A ladder state file edited by hand or lost is a silent sizing change; mitigated by fail-to-lower and by posting the level at every start (the existing "Hestia running" line can carry it).
- Per-engine evaluation means the engines drift apart in size; the total-margin gate and the crude group are the only things binding them. Whether that is acceptable is D1.
- Closed-trade equity ignores open losses. A large unrealised loss at the evaluation can still pass gate 1; the replay should show whether adding an open-P&L floor matters.
- Not checked in this draft: the exact hook lines in the five engines, whether Oleus's `_current_units` mirrors the others, and the Slack listener's command registration. Check before building.
- The one-unit drawdown constants are from backtests (Oleus's from the Janus run, not the live engine) and move with every refresh.

## 11. Open decisions for the owner

- **D1** Per-engine ladders, or all engines step together (as on 26 Oct and in the hand rule). If separate, how the crude group keys off the other three.
- **D2** Step-down: none, alert only (draft default), or the rule in section 7.
- **D3** Advise first (draft default) or straight to auto; how many weeks of agreement before auto.
- **D4** Fixed definitions: realised by exit date; "near peak" tolerance; time floor of 5 sessions; roll-eve lead in sessions.
- **D5** Whether the nightly job may act on a day with an open-position unrealised loss above some size, or closed trades only.
- **D6** Whether the margin ceiling uses the dynamic rule's divisor (engines sharing) or a fixed 4, and what the unit cap becomes once the ladder, not the cap, is what holds units down.
