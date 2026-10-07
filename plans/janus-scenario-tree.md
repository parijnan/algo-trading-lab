# Janus: scenario tree (every way the day can play out)

Companion to `plans/janus-camarilla-research.md` section 9 (the four-scenario plan and the ten open items). This document only enumerates how price action can branch at each decision point; it does not choose any rule. Where a branch depends on an open item it says which one. Written 2026-10-07 at the user's request. Documentation only: nothing here has been backtested.

## 0. How to read this

**Ladder.** Every level sits on one line, low to high: S7 < S6 < S5 < S4 < S3 < S2 < S1 < C < R1 < R2 < R3 < R4 < R5 < R6 < R7. A node is a pair: the position state (flat, long full, long after T1, and so on) and where price sits on that ladder. A branch is the next level price reaches, so the tree is a state machine on the ladder, not a list of prose scenarios.

**Wording.** Every branch reads "price reaches or crosses level X", written X↑ (reaches it from below) or X↓ (from above). Whether that means a touch, a 1-minute close or a 15-minute close is open item 1; settling it changes where a branch splits, not the shape of the tree.

**Branches every node has** (written once here, not repeated at each node):
- **CUT:** the entry cut-off (open item 9) arrives first. With no position open, no new entry is allowed, so the day ends flat (leaf NE). With a position open, only new entries are cut; the open position keeps being managed until its own exit or HX. A stop-out after the cut-off therefore does not reverse into the opposite entry.
- **HX:** the hard exit time (open item 9) arrives. Anything open is closed at market, whatever the P&L (leaf HX). With nothing open, the day ends flat.
- **AMB:** one 1-minute bar reaches two levels, so the order is unknown. Phase 0 already scores this as `ambiguous` and never guesses. Typical cases are a gap through a stop and a target, and the R3-R4 whipsaw bar (the two levels are only 0.275 R apart, so one volatile bar can span both).

**Leaf codes** (every path ends in one of these, so every day ends flat, which is the pure-intraday rule): NT no trade because there are no valid levels; NE no entry (flat all day); SL stopped out; TF final target hit and flat; HX forced flat at the hard exit; NR no rule: price moves to somewhere none of the user's scenarios trades, the tree ends flat there and the leaf names the open item it depends on.

**Scale legend** (distance from the previous close in units of the previous range R, approximate because R5 depends on H/L): R1 0.09, R2 0.18, R3 0.275, R4 0.55, R5 about 1.0, R6 about 1.5, R7 about 2.1; S mirrored.

**Phase 0 tags.** `[P0: R4→R5|R3]` marks a branch that matches a first-passage pair Phase 0 measured (R3: R4 versus R2; R4: R5 versus R3; R5: R6 versus R4; R6: R7 versus R5; S mirrored). Phase 0 measured these from the first touch of the level in any session, pooled over sides and not conditioned on the open zone, so a tag means "related", and frequencies can be attached later. Not computed here.

## M. Reusable management subtrees

Used by every entry below, so the long-side tree is written once. Parameters: entry E, stop SL, targets T1, T2, T3 (the user's levels per scenario, section 9 table), and the levels between E and T1 listed as optional trail points (not branch points). The short-side subtree MS is the exact mirror: replace every level and direction (↑ with ↓, higher with lower).

**ML.0, long full position, stop at SL.** The next event is one of:
- **ML.0.1** SL↓: full stop-out, leaf SL (all lots, loss). Follow-on node set by the calling scenario.
- **ML.0.2** T1↑: partial exit at T1 (the share is open item 3), then ML.1.
- **ML.0.3** HX: leaf HX, all lots closed at market.
- **ML.0.4** AMB: a bar reaches both SL and T1 (a gap); the order is unknown.
- Optional trail points before T1: each level between E and T1 reached and then retraced to E. Not branches; a trail rule (open item 8) would act here.

**ML.1, T1 booked, remainder open, price between T1 and T2** (state names the zone price is in; zones from the top down are ML.1a between T1 and T2, ML.1b between E and T1, ML.1c between SL and E):
- **ML.1a:** T2↑ books T2 and goes to ML.2a; T1↓ (back through the last target) goes to ML.1b; HX is a leaf; AMB if one bar reaches T2 and T1↓.
- **ML.1b:** T1↑ returns to ML.1a (loop); E↓ (back through the entry) goes to ML.1c; HX.
- **ML.1c:** E↑ returns to ML.1b (loop); SL↓ is leaf SL on the remainder; HX.
- Trail points here: T1↓ (trail to the last target), E↓ (trail to entry). With a fixed stop the retraces only matter when they reach SL; with a trail rule the remainder exits at T1 or E and the branch becomes a leaf there.

**ML.2, T1 and T2 booked, price above T2:**
- **ML.2a:** T3↑ books T3, leaf TF (all flat); T2↓ goes to ML.2b; HX.
- **ML.2b** (between T1 and T2): T2↑ back to ML.2a; T1↓ to ML.2c; HX.
- **ML.2c** (between E and T1): T1↑ back to ML.2b; E↓ to ML.2d; HX.
- **ML.2d** (between SL and E): E↑ back to ML.2c; SL↓ is leaf SL on the remainder; HX.
- Trail points: T2↓, T1↓, E↓.

The only recursion in the tree is the loops back to a higher zone (recoveries), which are shown as "back to", not unrolled.

## R. Root: day open

**How often each zone is the open** (Phase 0 sessions, 2026-10-07 re-run; `phase0_events_<SYMBOL>.csv`): between S3 and R3 (scenario 1) 77% CRUDEOILM, 91% SILVERMIC, 79% GOLDPETAL, 82% NATGASMINI; between R3 and R4 (scenario 2) 11%, 3%, 10%, 8%; between S3 and S4 (scenario 3) 8%, 4%, 6%, 6%; beyond R4 or S4 (scenario 4) 4%, 1%, 5%, 4%. Scenario 1 is where almost all the days are; scenarios 2 to 4 together are 9% to 23% of days.

- **R.0 no valid levels** (previous session missing, thin or stale): leaf NT. On an evening-only day the root time is 17:00, not 09:00.
- **R.1** open between S3 and R3: scenario 1 (section 1).
- **R.2** open between R3 and R4: scenario 2 (section 2).
- **R.3** open between S4 and S3: scenario 3 (section 3, the exact mirror of 2).
- **R.4** open at or beyond R4 or S4: scenario 4 (section 4).
- **R.5** open exactly on a level: on R3 or S3 the zone is either side, and on R4 or S4 either scenario 4 or 2/3; open item 4 decides, and the tree for each side applies to that choice.

## 1. Scenario 1: open between S3 and R3

Long arm: S3↓ then S3↑ (entry E=S3, SL=S4, T1=R1, T2=R2, T3=R3). Short arm: R3↑ then R3↓ (E=R3, SL=R4, T1=S1, T2=S2, T3=S3). Both arms are live at the open; mid-levels between S3 and R3 (S2, S1, C, R1, R2) are crossed on the way and are not decision points.

**1.0 flat, price between S3 and R3.** The next event is one of: 1.1 S3↓, 1.2 R3↑, 1.3 CUT (leaf NE), HX (leaf, nothing open), AMB (one bar spans S3 and R3, a gap).

**1.1 S3↓ first: long arm primed** (price below S3). Next event:
- **1.1.1** S3↑ (reclaims S3): enter long, ML with E=S3, SL=S4, T R1, R2, R3. Trail points before T1: S2, S1, C. Outcome mapping:
  - SL leaf (S4↓): node **1.1.1.s**, flat below S4. Next: S3↑ again (re-entry question, open items 6 and 7: if allowed, loop to 1.1.1; if not, leaf NR); S5↓ (leaf NR: S5, S6 and S7 are below the arm and no scenario-1 rule trades them, see N1); CUT or HX leaf.
  - TF leaf (R3 reached as T3): node **1.1.1.t**, flat at R3. Because R3↑ is also the short arm's priming event, this leads to **1.2** with the short arm now primed (both trades on one day, open items 6 and 7 decide whether the second may happen).
  - Retrace nodes ML.1b, ML.1c, ML.2b to ML.2d as defined in M, with the targets above.
- **1.1.2** S4↓ before S3↑: the long arm is broken before any entry (the stop level was reached first). Next event:
  - **1.1.2.1** S3↑ later (price comes back above S3 with no entry yet): late reclaim; whether an entry is still allowed is open item 7, and the stop level S4 is already behind price. If allowed, enters long (ML as in 1.1.1); if not, flat and continue to R3↑ (1.2).
  - **1.1.2.2** S5↓, then S6↓, then S7↓: leaf NR at each depth (a breakout through S4 that no scenario-1 rule trades; Phase 0 found continuation beyond S4 more often than chance, see N1). At each of S5, S6, S7 price can also reverse back up through the level before ending; those reversals loop to 1.1.2 and add nothing new.
- **1.1.3** CUT while primed: leaf NE. **1.1.4** HX: leaf.

**1.2 R3↑ first: short arm primed** (price above R3). Mirror of 1.1: **1.2.1** R3↓ enters short (MS, E=R3, SL=R4, T S1, S2, S3), with SL at R4↑ going to node 1.2.1.s (re-entry question; R5↑ is leaf NR) and TF at S3↓ going to 1.2.1.t, which primes the long arm (node 1.1, cross-arming). **1.2.2** R4↑ before R3↓ breaks the arm (late recross is item 7; R5, R6, R7 are leaf NR). **1.2.3** CUT, **1.2.4** HX.

**1.3 neither arm ever primed:** price stays between S3 and R3 to the cut-off: leaf NE ("quiet day"). In Phase 0 only 2% to 6% of sessions that open between S3 and R3 never touch either R3 or S3, so this branch is rare.

## 2. Scenario 2: open between R3 and R4

Long: R4↑ (E=R4, SL=R3, T1=R5, T2=R6, T3=R7). Short: R3↓ (E=R3, SL=R4, T1=S1, T2=S2, T3=S3). Both triggers are pending at the open.

**2.0 flat, price between R3 and R4.** The next event is one of: 2.1 R4↑, 2.2 R3↓, CUT (leaf NE), HX (nothing open), AMB (one bar spans R3 and R4, only 0.275 R apart, so this is the likeliest ambiguity in the whole tree; open item 1 matters most here).

**2.1 R4↑: enter long** (ML, E=R4, SL=R3, T R5, R6, R7). No levels lie between R4 and R5. `[P0: R4→R5|R3]` is the ML.0 split (T1 before SL), `[P0: R5→R6|R4]` is ML.1a (T2 before E), `[P0: R6→R7|R5]` is ML.2a (T3 before T1). Outcome mapping:
- **SL leaf (R3↓):** node **2.1.s**. This is the same event as the short trigger in 2.2, so the stop-out and the opposite entry coincide (a stop-and-reverse). Branch by whether the entry is still allowed (before the cut-off and open item 6 permits reversal): **2.1.s.1** enters short (2.2, E=R3), **2.1.s.2** no reversal (CUT passed or not permitted): flat; price moves on below R3 (R2, R1 and lower) with no position and no rule, leaf NR (N1).
- **TF leaf (R7↑):** node **2.1.t**, flat at R7. Beyond R7 no level exists, so nothing further is traded; price can reverse down but no rule applies (leaf NR, N1).
- **Retrace nodes** ML.1b, ML.1c (E=R4, SL=R3: the remainder's stop is also the short trigger, so a stop on the remainder is again a stop-and-reverse, node 2.1.s) and ML.2b to ML.2d.

**2.2 R3↓: enter short** (MS, E=R3, SL=R4, T S1, S2, S3). Trail points before T1: R2, R1, C. Outcome mapping:
- **SL leaf (R4↑):** node **2.2.s**. This is the long trigger in 2.1, so again a stop-and-reverse: **2.2.s.1** enters long (back to 2.1), **2.2.s.2** no reversal: flat (leaf NR for R5 and beyond, N1).
- **TF leaf (S3↓):** node **2.2.t**, flat at S3, price now in the S3 to S4 zone, which is scenario 3's geometry. Whether scenario 3's triggers become live (open item 5 and N1) decides between joining node 3.0 and leaf NR.
- Retrace nodes as in M.

**2.3 The whipsaw cycle.** R4↑ (long) then R3↓ (stop and short) then R4↑ (stop and long) and so on, each leg a full stop-out and re-entry, can repeat many times between R3 and R4. The tree shows it as the loop 2.1 to 2.1.s.1 to 2.2 to 2.2.s.1 to 2.1; it does not unroll. The loop ends only by: CUT (the next reversal is no longer allowed, so the stop-out is the last trade, leaf SL), HX (flat), a run out to R5 (the long reaches T1, ML.1) or a drop to S1 (the short reaches T1, MS.1). Counting the number of laps before the cut-off is a Phase 1 measurement, not a tree decision.

## 3. Scenario 3: open between S4 and S3

An exact mirror of scenario 2, because S_k = 2C - R_k for every level including S5 to S7. Write it by level substitution (no new tree):

| Scenario 2 level | Scenario 3 level | Scenario 2 meaning | Scenario 3 meaning |
|---|---|---|---|
| R4 (long entry) | S4 (short entry) | long on R4↑ | short on S4↓ |
| R3 (long stop, short entry) | S3 (short stop, long entry) | stop and reverse | stop and reverse |
| R5, R6, R7 (long targets) | S5, S6, S7 (short targets) | T1 to T3 | T1 to T3 |
| S1, S2, S3 (short targets) | R1, R2, R3 (long targets) | T1 to T3 | T1 to T3 |
| long / short, ↑ / ↓ | short / long, ↓ / ↑ | | |

So scenario 3's nodes are 3.x = 2.x with this substitution (3.1 S4↓ enters short with SL=S3 and targets S5, S6, S7; 3.2 S3↑ enters long with SL=S4 and targets R1, R2, R3; 3.1.s and 3.2.s are the stop-and-reverse nodes; 3.3 is the whipsaw cycle S3 to S4).

Note that the scenario-3 long (E=S3, SL=S4, T R1, R2, R3) uses the same levels as the scenario-1 long, but scenario 1 requires the break below S3 first, while scenario 3 enters on the first S3↑ from an open already below S3.

## 4. Scenario 4: open beyond R4 or S4

"Wait for price to come back in range, then trade accordingly." The branches are where price re-enters and which scenario then applies (open item 5; N1). Write the R side; the S side mirrors (4.5 to 4.8 below S4, S5 to S7).

**Open zones on the R side:** 4.1 R4 to R5, 4.2 R5 to R6, 4.3 R6 to R7, 4.4 beyond R7.

**Excursion ladder (no position, nothing traded, only where price goes):** from 4.1: R5↑ goes to 4.2; from 4.2: R6↑ goes to 4.3 and R5↓ returns to 4.1; from 4.3: R7↑ goes to 4.4 and R6↓ returns to 4.2; from 4.4: R7↓ returns to 4.3. If price never returns below R4 before the cut-off the day is leaf NR (a breakout day that no rule trades, N1). Phase 0's R4 to R5 continuation lives here and in 4.2 to 4.4.

**Re-entry from 4.1 (price crosses R4↓)**, or from a higher zone after passing the ladder down. "In range" means the point P (open item 5: re-entry at R4, or at R3). After the re-entry:
- **4.1.1** price holds between R3 and R4: now the geometry of scenario 2; node 2.0 applies, except the re-entry itself just crossed R4↓. Because scenario 2's long trigger is R4↑, a quick bounce back through R4 would be an immediate long entry on a failed re-entry: branch **4.1.1.1** R4↑ again (a false re-entry, enters long per 2.1, stop R3) versus **4.1.1.2** R3↓ (enters short per 2.2).
- **4.1.2** price crosses both R4↓ and R3↓ quickly (one bar or a few minutes): the short that results is the same trade under scenario 1 (above R3, then back below it) and scenario 2 (R3↓): entry R3, stop R4, targets S1 to S3, so the short itself is not ambiguous. What is open is the other side: whether the scenario-2 long (R4↑) is live after the re-entry (4.1.1.1), and whether price had to be seen above R3 first (it was, coming from above R4). Open item 5 and N1.
- **4.1.3** price crosses R4↓ and keeps going to S3↓ and below without pausing in the band: from the re-entry point it passes through scenario 1's whole zone; the matching scenario is again item 5 and N1; leaf NR until decided.
- **4.1.4** price returns only as far as the R4 level and turns back up (touch without a cross): remains 4.1.
- **4.1.5** CUT, HX: leaf NE.

**4.2 to 4.4:** to come back into range price must pass 4.1, so their re-entry branches are the same as 4.1.1 to 4.1.5 once price crosses R5↓ and R4↓ in turn.

## 5. Structural coincidences (the tree's central cases, collected)

- **C1 Stop-and-reverse (scenarios 2 and 3):** the long's stop is the short's entry and vice versa. A stop-out lands directly in the opposite entry. Nodes 2.1.s, 2.2.s, 3.1.s, 3.2.s, and the loop 2.3. Open item 6.
- **C2 Cross-arming (scenario 1):** the long's T3 is R3, the short's arming level, and the short's T3 is S3, the long's arming level. A full winning trade primes the opposite arm, so both trades can happen on one day (nodes 1.1.1.t, 1.2.1.t). Open items 6 and 7.
- **C3 Broken arm (scenario 1):** the arm's stop level is reached before the entry, so the setup is dead before it starts (1.1.2, 1.2.2). Late reclaim is open item 7; continuation beyond is leaf NR.
- **C4 Scenario 4 re-entry ambiguity:** scenario 1's short and scenario 2's short are the same trade (E=R3, SL=R4, T S1 to S3), differing only in whether an earlier R3↑ is required, so a cross down through R3 after re-entry from above gives one short either way (4.1.2). The open question is the long side: whether scenario 2's R4↑ long is live after the re-entry (4.1.1.1). Open item 5 and N1.
- **C5 Concurrency:** one side open while the opposite trigger fires. In scenarios 2 and 3 the long's remainder stop equals the short's trigger, so the two events coincide (C1); in scenario 1 the arms are separate and the gap between S3 and R3 (0.55 R) makes simultaneity possible only through a partial position plus the cross-armed trade (C2). Open item 6.
- **C6 Re-arming:** after any stop, price returns to the trigger level (1.1.1.s, 1.2.1.s). Open items 6 and 7.
- **C7 Ambiguous bars:** AMB leaves at 1.0, 2.0 (the R3 to R4 bar), ML.0.4 and ML.1a. Open item 1.

## 6. Leaf ledger and agenda

Every leaf is flat. The leaves that end with "no rule" and the open item each depends on are the agenda for the next discussion.

| Leaf | Where | Meaning | Depends on open item |
|---|---|---|---|
| NT | R.0 | no valid levels | none (data rule) |
| NE | 1.0, 1.1.3, 1.3, 2.0, scenario 3 mirrors, 4.1.5 | cut-off reached with nothing entered | 9 |
| SL | ML.0.1, ML.1c, ML.2d (remainder) | stopped out | 1, 2 |
| TF | ML.2a (T3) | final target, flat | 3 |
| HX | every node | hard exit | 9 |
| NR | 1.1.1.s (S5 and beyond), 1.1.2.2, 1.2.1.s (R5 and beyond), 1.2.2, 2.1.t, 2.1.s.2, 2.2.s.2, 2.2.t (if scenario 3 does not apply), 4.1 ladder (no return), 4.1.3 | no scenario trades this | 5, N1 |
| AMB | 1.0, 2.0, ML.0.4, ML.1a, 4.1.2 | same-bar order unknown | 1 |

**N1 is open item 11 in the plan (surfaced by the tree, added 2026-10-07):** is the scenario fixed by the open for the whole day, or re-evaluated as price moves into another zone? Every NR leaf above exists because the scenario was fixed at the open and price left that zone (a scenario-1 day that breaks R4, a scenario-2 short that reaches S3, a scenario-4 breakout). If the scenario is re-evaluated by current zone instead, most NR leaves become entries into another scenario's tree. Phase 0 found continuation beyond R4 and S4 more often than a driftless walk, so these leaves are the most likely place for a missed move.

**Self-check performed on this document.** Every level, stop and target in sections 1 to 3 matches the table in section 9 of the plan for all eight setups (the four scenario-1 and scenario-2 setups plus scenario 3's two). Every path ends in one of the leaf codes above. CUT, HX and AMB apply to every node by section 0 rather than being repeated.
