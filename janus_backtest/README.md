# Janus: Camarilla pivot intraday research (MCX)

Research only: nothing here touches live code, Hestia, Delos or the engines' configs. Plan and phases: `plans/janus-camarilla-research.md`. Instruments: CRUDEOILM, SILVERMIC, GOLDPETAL, NATGASMINI (the four the engines trade, liquidity already confirmed; any of them could later be hosted by Hestia).

## Files

- `janus_configs.py`: every parameter (symbols, data start per instrument, the shared Fyers data void, level factors, first-passage pairs). No magic numbers elsewhere.
- `janus_levels.py`: pure function, previous session high/low/close to R1..R5 and S1..S5 (R1-R4 = C + R x 1.1/{12,6,4,2}, R5 = (H/L) x C, S mirrored).
- `janus_data.py`: one record per session with that session's 1-minute bars of the effective (early-roll) contract and the levels from the SAME contract's own previous session, so a roll day never mixes two contracts. Fyers history only, real un-adjusted prices; sessions in the shared Fyers void (2026-04-01 to 2026-06-29) are skipped, never filled. The Angel One tail (2026-09-02 onward) is not included yet.
- `janus_events.py`: per session and level, the first touch and what follows (first passage between an outward and an inward level, excursions, session close versus the level), in an upper frame so one piece of logic serves both sides. A bar that reaches both levels is reported as ambiguous, never guessed; a gap beyond a level at the open is flagged `at_open`, not counted as a touch.
- `phase0_descriptive.py`: runs all four instruments, writes `outputs/phase0_events_<SYMBOL>.csv` (gitignored) and `outputs/phase0_summary.txt`.
- Tests: `tests/test_janus_levels.py`, `tests/test_janus_events.py` (hand-computed levels, constructed price paths, mirror symmetry, benchmark arithmetic).

## How to read a first-passage result

After the first intraday touch of a level, the table gives the share of touches where the inward level came first, among touches where either was reached, next to the driftless benchmark (gambler's ruin from the level distances: for R3 with outward R4 and inward R2 the benchmark is 75% because R2 is three times closer). Excess over the benchmark is what matters, not the raw share. Negative excess means price continued outward more than a driftless walk would, positive means it reverted more.

## Phase 0 findings (2026-10-05, 790 to 1,319 sessions per instrument)

See `outputs/phase0_summary.txt` for every table. Headline: on all four instruments, after a touch of R3/S3 the inward level (R2/S2) is reached first less often than the driftless benchmark (excess -5 to -12 points), and after R4/S4 the continuation to R5/S5 beats the benchmark by a similar margin. The sign is the same in every instrument and almost every year, and it is largest in 2026. That leans toward continuation rather than reversion at these levels, but it is not yet an edge: it is not net of costs, and touching an extreme level selects trending days, so a plain "a day that reached an extreme tends to keep going" effect would show the same sign without any Camarilla-specific information. The shuffled-levels null in Phase 2 is what separates those.
