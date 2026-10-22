"""
Camarilla pivot intraday research (plans/janus-camarilla-research.md). Every parameter lives here: no magic numbers in the other
scripts. Research only: nothing in this directory touches live code, Hestia, Delos or the engines' configs.
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMETHEUS_DIR = os.path.join(REPO_ROOT, 'prometheus_backtest')            # data_loader_p3's rollover machinery is reused, not copied
FYERS_DATA_DIR = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx_fyers')
HOLIDAYS_FILE = os.path.join(REPO_ROOT, 'data', 'mcx_holidays_2022_2026.csv')
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outputs')

# The four instruments the existing engines trade (liquidity already confirmed); any of them could later be hosted by Hestia.
SYMBOLS = ['CRUDEOILM', 'SILVERMIC', 'GOLDPETAL', 'NATGASMINI']

# First date of usable Fyers history per instrument (the earliest contract file's start; same values the engines' own loaders use).
DATA_START = {'CRUDEOILM': '2023-04-01', 'SILVERMIC': '2021-04-01', 'GOLDPETAL': '2021-10-01', 'NATGASMINI': '2023-04-01'}

# Systemic Fyers data void shared by all instruments (found by the Selene/Helios/Typhon work): the April and May 2026 contract files
# effectively ended 2026-03-31 and the next real data started 2026-06-30. FILLED on 2026-10-05 (Fyers expired-contract pull): all four
# instruments now have 63 of the 64 weekdays in the window (the one gap is Good Friday 2026-04-03), so nothing is skipped. Set to a
# ('YYYY-MM-DD', 'YYYY-MM-DD') pair to exclude a window again, e.g. if a future gap is found; None means no exclusion.
FYERS_VOID = None
# Angel One per-contract files hold their OWN contract only from this date (earlier rows are a relabelled front month); Phase 0 uses
# Fyers only, so the study ends where Fyers history ends and the Angel One tail is a later extension.
DATA_END = None            # None: whatever the Fyers files hold
# Angel One tail (2026-10-15, opt-in per call: load_sessions(..., live_tail=True)): a contract with no Fyers file yet (it has not expired) is read from the live tree,
# keeping only rows from LIVE_OWN_CONTRACT_FROM, the date the live files hold that contract's own prices (earlier rows are a relabelled front month).
LIVE_DATA_DIR = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'mcx')
LIVE_OWN_CONTRACT_FROM = '2026-09-02'

# Camarilla levels: previous session high H, low L, close C, range R = H - L.
#   R1..R4 = C + R * FACTOR * (1/12, 1/6, 1/4, 1/2);  S1..S4 mirror them below C.
#   R5 = (H / L) * C;  S5 = C - (R5 - C).
#   R6 = R5 + R6_FACTOR * (R5 - R4);  S6 mirrors it about C.
#   R7 = R6 + R7_FACTOR * (R6 - R5);  S7 mirrors it about C (added 2026-10-06 as the breakout target beyond R6; no standard definition exists,
#   this continues the same geometric ladder, each gap 1.168 times the one below, and is a convention to be revisited, not a published formula).
CAMARILLA_FACTOR = 1.1
LEVEL_DIVISORS = {1: 12.0, 2: 6.0, 3: 4.0, 4: 2.0}
R6_FACTOR = 1.168          # the common convention for the sixth level (second breakout target)
R7_FACTOR = 1.168          # same ratio applied once more for the seventh level (third breakout target)

# The previous session counts as "the previous trading day" only if it is no more than this many calendar days back (weekends and one
# holiday pass; a longer hole in the contract's own data means the levels would be stale, so that session is skipped).
MAX_PREV_SESSION_GAP_DAYS = 5

# A session needs at least this many 1-minute bars (after dropping placeholders) to be used, previous session included.
MIN_BARS_PER_SESSION = 120

# Open-location buckets in units of R from the previous close are defined by the levels themselves (see janus_events.open_zone).
# Touch events within this many minutes of the session's first bar are reported separately as "at open" (a gap beyond a level is not a touch).
AT_OPEN_MINUTES = 1

# Levels whose first touch is studied (level numbers; both sides, R and S). R7/S7 have no further outward level, so no first-passage pair.
TOUCH_LEVELS = [3, 4, 5, 6, 7]
# First-passage pairs (outward level, inward level) measured after the first touch of the keyed level, in the frame where the touched level
# is an upper one (lower-side touches are mirrored). 'C' is the previous close. The outward level acts as the stop of a fade or the
# continuation target of a breakout, the inward one as the fade target.
FIRST_PASSAGE = {3: [(4, 2), (4, 'C')], 4: [(5, 3), (5, 2)], 5: [(6, 4)], 6: [(7, 5)], 7: []}
# Open-location zones, from the levels themselves (see janus_events.open_zone).

# --- Thirds / trail / re-entry feasibility (2026-10-11, plans/janus-camarilla-research.md items 3 and 8) ---
# Setups in the LONG frame (a short is the same logic on negated prices, where S and R swap). The entry is a touch (item 1 grid arm A);
# the stop is the level named; targets T1..T3 in order. 'fade' is the reclaim of S3 (scenarios 1 and 3), 'breakout' is the break of R4 (scenario 2).
SETUPS = {'fade': {'entry': 'S3', 'stop': 'S4', 'targets': ('R1', 'R2', 'R3')},
          'breakout': {'entry': 'R4', 'stop': 'R3', 'targets': ('R5', 'R6', 'R7')}}
# After a target is hit in a bar, is the moved stop also tested against that same bar's low? 'test' is the pessimistic reading (the order inside
# the bar is unknown), 'skip' tests it from the next bar. The trailing study used the pessimistic reading.
TRAIL_SAME_BAR = 'test'
# Year groups reported separately (the sample is small per cell, so years are shown pooled over instruments as well).

# --- Day simulation: thirds, original stop, re-buy of booked lots, counter trade at T3 (2026-10-11, plan section 9a / items 3 and 8) ---
# The four setups in absolute prices. side +1 long, -1 short. The entry is a touch of the level approached from the side named by the setup
# (a long enters as price rises through its level, a short as price falls through it). Lot 1 targets T1, lot 2 T2, lot 3 T3; one stop for all.
# 'counter' is the sister setup entered on arrival when this setup's T3 is hit (fades only: a mean-reversion trade ends where its sister begins).
ABS_SETUPS = {
    'F_L': {'side': 1, 'family': 'fade', 'entry': 'S3', 'stop': 'S4', 'targets': ('R1', 'R2', 'R3'), 'counter': 'F_S'},
    'F_S': {'side': -1, 'family': 'fade', 'entry': 'R3', 'stop': 'R4', 'targets': ('S1', 'S2', 'S3'), 'counter': 'F_L'},
    'B_L': {'side': 1, 'family': 'breakout', 'entry': 'R4', 'stop': 'R3', 'targets': ('R5', 'R6', 'R7'), 'counter': None},
    'B_S': {'side': -1, 'family': 'breakout', 'entry': 'S4', 'stop': 'S3', 'targets': ('S5', 'S6', 'S7'), 'counter': None},
}
# One change at a time from A0. 'rebuy': booked lots are bought back at the original entry level when price returns to it, for their own targets.
# 'counter': a fade's T3 opens the sister setup at that level.
ARMS = {'A0': {'rebuy': False, 'counter': False}, 'A1': {'rebuy': True, 'counter': False},
        'A2': {'rebuy': False, 'counter': True}, 'A3': {'rebuy': True, 'counter': True}}
# Families whose trades are re-bought (user text covered the fade; breakout is an assumption to be confirmed, so results are reported by family).
REENTRY_FAMILIES = ('fade', 'breakout')
# Path a 1-minute bar is assumed to take (the order of high and low inside a bar is unknown):
#   'neutral': open, low, high, close on a bullish bar and open, high, low, close on a bearish one;
#   'adverse': against the net position open at the start of the bar (low first for a net long), neutral when flat.
BAR_ORDERS = ('neutral', 'adverse')
# PROVISIONAL (plan item 9): no new entry, re-buy or counter trade in the last ENTRY_CUTOFF_MIN minutes of the session; everything still open
# exits at the open of the first bar at or after HARD_EXIT_MIN minutes before the last bar. Measured from the session's last bar (it varies with US DST).
ENTRY_CUTOFF_MIN = 60
HARD_EXIT_MIN = 15
# Safety net on the event loop inside one bar: exceeding it raises instead of silently stopping.
MAX_EVENTS_PER_BAR = 200
# Round-trip cost per leg (entry plus exit of one lot), percent of price; every re-buy and every counter trade pays its own.
COSTS_PCT = (0.0, 0.02, 0.05, 0.10)
# Limit-type fills (targets, re-buys) need price to trade this many ticks THROUGH the level; 0 is a plain touch. Tick sizes are checked against the data.
TICK_THROUGH = 0
TICK_SIZE = {'CRUDEOILM': 1.0, 'SILVERMIC': 1.0, 'GOLDPETAL': 1.0, 'NATGASMINI': 0.1}

# --- Entry modes (user design 2026-10-11): close-armed watcher with a limit order at the level ---
# 'touch': the entry fires when price rises (falls) through the level (the first design, kept for reference).
# 'close_limit': per setup a watcher. IDLE until a bar of `minutes` minutes CLOSES on the arming side of the level (long: below it; short: above
#   it) -> ARMED; the next close on the entry side -> PENDING, a limit order at exactly the level working from the next 1-minute bar. It fills when
#   price trades back to the level (a touch; TICK_THROUGH applies to limit fills of targets and re-buys only). If price reaches T1 first the order is
#   cancelled and the watcher goes back to IDLE (the move was missed; CANCEL_AT_T1). A trade ending (stop, T3, hard exit) puts the watcher back to IDLE.
#   fill 'retest' is the realistic limit; 'at_signal' is the LITERAL reading (bought at the level on the signal close although price is already above
#   it), an optimistic upper bound kept as a reference only.
ENTRY_SCENARIOS = {
    'touch': {'mode': 'touch'},
    'c1': {'mode': 'close_limit', 'minutes': 1, 'fill': 'retest'},
    'c5': {'mode': 'close_limit', 'minutes': 5, 'fill': 'retest'},
    'c15': {'mode': 'close_limit', 'minutes': 15, 'fill': 'retest'},
    'l1': {'mode': 'close_limit', 'minutes': 1, 'fill': 'at_signal'},
    'l5': {'mode': 'close_limit', 'minutes': 5, 'fill': 'at_signal'},
    'l15': {'mode': 'close_limit', 'minutes': 15, 'fill': 'at_signal'},
}
CANCEL_AT_T1 = True
# What the runner simulates: scenario -> arms. 'A0x' is A0 with TICK_THROUGH=1 (limit fills of targets and re-buys need one tick through the level).
RUN_PLAN = {'touch': ['A0', 'A3'], 'c1': ['A0', 'A1', 'A2', 'A3', 'A0x'], 'c5': ['A0', 'A1', 'A2', 'A3', 'A0x'],
            'c15': ['A0', 'A1', 'A2', 'A3', 'A0x'], 'l1': ['A0'], 'l5': ['A0'], 'l15': ['A0']}

# --- Range-based levels (2026-10-12, plans/janus-range-levels.md) ---
# Where the High and Low that feed the Camarilla formulas come from. 'previous_session' (the control, and the default so nothing existing changes): the previous
# session's own High and Low. 'range_setter': the bounds of a price range tracked on the traded contract's own daily bars with setter-candle logic (a close strictly
# outside the range starts a new range from that day's bar; otherwise the range widens to include the day's High and Low). The Close is always the previous session's.
LEVEL_SOURCES = ('previous_session', 'range_setter')
LEVEL_SOURCE = 'previous_session'
# A day takes part in a contract's range history only if it has at least this many 1-minute bars and some traded volume (Fyers delivers a bar for every minute,
# zero-volume placeholders carrying the previous close, so a no-trade day is dropped, never turned into a flat bar).
RANGE_MIN_BARS = MIN_BARS_PER_SESSION

# --- State machine (2026-10-20, plans/janus-state-machine.md): janus_machine.py + machine_run.py ---
# Touch entries only (close triggers come later). Plain equal thirds with the original stop is the base case; every extra is a toggle, OFF by default.
MACHINE_SYMBOL = 'CRUDEOILM'
MACHINE_LEVEL_SOURCE = 'range_setter'          # one of LEVEL_SOURCES
MACHINE_REBUY = False                          # booked lots are bought back at the original entry level when price returns to it
MACHINE_COUNTER = False                        # a fade's T3 opens its sister setup at that level
MACHINE_TSL = False                            # trailing stop on the trade (the stop is then a property of the trade, not of the setup)
# When the TSL toggle is on: after target k is hit the trade's stop moves to the place named (never backwards). 'entry' = the trade's first fill,
# 'T1' / 'T2' = that target's level, None = leave the stop where it is. A moved stop is a plain stop: hitting it closes the trade and does NOT
# open the sister setup (the stop is no longer the sister's entry level). With re-buy on, a stop at the entry level is hit first (stops before re-buys at the
# same price), so the booked lots are not bought back.
MAX_ENTRIES_PER_SETUP = 0   # entries one setup may take per session (trade entries and counter entries; re-buys do not count); 0 = no limit
STOP_SCALE = 1.0   # share of the original stop distance (entry level to stop level) the trade's stop sits at; 1.0 = the Camarilla stop level itself
TSL_RULES = {1: 'entry', 2: 'T1'}
# With the trailing stop on: may a setup enter again in the same session after a TRAILED stop closed its trade? True is the plain event loop (price crosses the entry
# level again and the setup fires again; 65 such trades appeared in the first full run, -17.9 R); False blocks that setup for the rest of the session. A normal stop
# at the original level, a target, the hard exit and the session end never block anything. Has no effect with the trailing stop off.
TSL_REENTRY = True
MACHINE_LIVE_TAIL = True                       # sessions of a contract with no Fyers file yet (not expired) come from the live Angel One tree; they carry data_source 'angelone_tail'
MACHINE_BAR_ORDER = 'neutral'                  # the order written to the logs; the other order is run alongside as the sensitivity
MACHINE_OUTPUT_DIR = os.path.join(OUTPUT_DIR, 'machine')
# Levels whose first touch of the session is logged as an event (the level state of the day).
MACHINE_LEVELS = ['S7', 'S6', 'S5', 'S4', 'S3', 'S2', 'S1', 'C', 'R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7']
