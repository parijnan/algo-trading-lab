# Janus: other pivot families (regular, Fibonacci, and others)

**Status: notes only, written 2026-10-29. Nothing designed, nothing built. To be discussed 2026-10-30, with the advisor involved before any work starts.** Runs in parallel with monitoring the Oleus paper trades (`plans/oleus-janus-engine.md`). Method and code to reuse: `plans/janus-camarilla-research.md` (Phase 0 descriptive study, then Phase 1 rules), `janus_backtest/janus_levels.py` (level builders), `janus_events.py` (first-passage statistics, pairs set in `janus_configs.FIRST_PASSAGE`), `janus_machine.py` (the state machine over setups given as entry, stop and three targets).

## 1. The owner's point (2026-10-29)

The rules cannot simply be carried over from Camarilla, because regular and Fibonacci pivots give three levels of support, three of resistance and one central pivot P, and the structure is different. The Camarilla set-ups lean on specific roles: S3/R3 fade, S4/R4 breakout, the next level out as the stop, and a ladder out to R7/S7 for three targets.

## 2. What breaks when the Camarilla rules are moved over

- **The outer levels have no partner.** A fade from S2 has its stop at S3 but nothing beyond S3. A breakout from R1 or R2 runs out of levels after R3. The plain-thirds structure needs a stop and three targets per setup, so the outer ones need a derived rule: extensions of the previous range (Fibonacci extensions), a multiple of the stop distance, or fewer lots/targets.
- **The central pivot P is new.** It could be a fade target, a trend-bias filter (price above or below P), the stop of a breakout, or all of these as separate experiments.
- **The spacing differs.** Fibonacci levels sit at fixed 0.382, 0.618 and 1.0 of the previous range (H - L) from P. Classic levels depend on where the close sits in the range: R1 = 2P - L, S1 = 2P - H, R2 = P + (H - L), S2 = P - (H - L), R3 = H + 2(P - L), S3 = L - 2(H - P), with P = (H + L + C) / 3. Neither lines up with Camarilla's distances (R3/S3 about 0.275 and R4/S4 about 0.55 of the range from the close), so stop distances, win rates and the number of touches per day will differ.
- **Stop-and-reverse coupling.** In Camarilla one setup's stop is its sister's entry level (S4 stops the fade long and enters the breakout short). The pivot ladder can keep this if the setups are chosen so that a stop sits on the next level out, but the outermost levels cannot.

## 3. Proposed method (the same order as Camarilla)

1. **Phase 0, descriptive, no rules.** Add the pivot families to the level builder: classic and Fibonacci first, Woodie or DeMark later if wanted, Camarilla kept as the control. Run the first-passage tables the Camarilla work used: after a touch of each level, which is reached first, the next level out or the next one in, against the driftless benchmark. Crude first (Oleus's instrument), then silver, gold and natural gas. The numbers should say which level deserves which role (fade or breakout) before any rule is written.
2. **Phase 1, rules designed from that evidence**, one variable changed per experiment, parameters only in `janus_configs.py`, costs in from the first run.

## 4. Decisions for the owner at the start

1. **Families.** Classic and Fibonacci first, Woodie or DeMark later? Proposed: classic, Fibonacci and the Camarilla control.
2. **The missing outer targets and stops.** Fibonacci extensions beyond R3/S3 in the first experiments, or only the levels that exist with fewer than three targets? Proposed: decide after Phase 0 shows how far moves usually run past R3/S3.
3. **The role of P.** Fade target, bias filter, or both, as separate experiments? Proposed: separate experiments, P as a target first (it needs no new rule), the bias filter second.
4. **Who is consulted first.** The owner will involve the advisor before work starts.

## 5. Constraints carried over

Backtests must stay at production parity where an engine could follow (touch entries at the market, hard exit 15 minutes before the last bar, flat overnight, no entry in the first minute or the last hour). Any candidate becomes an Oleus-style engine only after the same gates: a pure machine pinned leg for leg to the research machine, a replay check, then paper. The cost and slippage study (still open for Oleus) applies equally.
