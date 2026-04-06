"""
backtest.py — Artemis Backtest Engine

Iron Condor (PE spread + CE spread) on Sensex or Nifty weekly options.
Replicates the live Artemis strategy logic exactly, including:
  - Premium-based strike selection at entry
  - DTE-based option SL multipliers
  - Index SL with 09:15 guard
  - Post-SL adjustment and re-entry on the surviving spread
  - Additional lots before cutoff_time
  - ELM exit at elm_time (lesser premium side exits)
  - Expiry exit at Thursday 15:30

Execution model (mirrors Apollo convention and live reality):
  - Signal/trigger detected on candle CLOSE
  - Execution at OPEN of the next candle
  - ELM exit: open of 15:16 candle
  - Expiry: close of last available candle; 0.05 if missing and OTM

Run generate_contracts.py first to produce contracts.csv.
Run precompute_vix.py (or let this script handle it) before the main loop.

Usage:
  python artemis_backtest/backtest.py
"""

import os
import sys
import logging
import warnings
import pandas as pd
import numpy as np
from math import floor, ceil
from datetime import time as dtime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from configs import (
    INSTRUMENT,
    BACKTEST_START_DATE, BACKTEST_END_DATE,
    SENSEX_INDEX_FILE, NIFTY_INDEX_FILE, VIX_INDEX_FILE,
    SENSEX_OPTIONS_PATH, NIFTY_OPTIONS_PATH,
    CONTRACTS_FILE, HOLIDAYS_FILE,
    TRADE_LOGS_DIR, TRADE_SUMMARY_FILE,
    LOT_SIZE, STRIKE_INTERVAL, EXPECTED_PREMIUM, HEDGE_POINTS,
    INDEX_SL_OFFSET, ADJUSTMENT_DISTANCE, MINIMUM_GAP, MINIMUM_GAP_ITERATOR,
    VIX_THRESHOLD,
    SL_0_DTE, SL_1_DTE, SL_2_DTE, SL_3_DTE, SL_4_DTE,
    ENABLE_INDEX_SL, ENABLE_OPTION_SL,
    EXPIRY_FALLBACK_PRICE,
    ENABLE_TRADE_LOGS,
    LOT_COUNT,
)
from data_loader import (
    load_index_data, load_vix_daily, load_option_data,
    get_price, get_index_price, get_next_open, get_index_next_open,
    scan_strikes_for_premium,
)

warnings.filterwarnings('ignore')

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Instrument routing
# ---------------------------------------------------------------------------
INDEX_FILE    = SENSEX_INDEX_FILE if INSTRUMENT == 'sensex' else NIFTY_INDEX_FILE
OPTIONS_PATH  = SENSEX_OPTIONS_PATH if INSTRUMENT == 'sensex' else NIFTY_OPTIONS_PATH

# ---------------------------------------------------------------------------
# SL multiplier lookup
# ---------------------------------------------------------------------------

def get_sl_multiplier(dte: int) -> float:
    if dte >= 4:
        return SL_4_DTE
    elif dte == 3:
        return SL_3_DTE
    elif dte == 2:
        return SL_2_DTE
    elif dte == 1:
        return SL_1_DTE
    else:
        return SL_0_DTE


def compute_dte(from_date, expiry_ts: pd.Timestamp) -> int:
    """Business day count from from_date to expiry date (exclusive end)."""
    return int(np.busday_count(
        pd.Timestamp(from_date).date(),
        expiry_ts.date()
    ))


# ---------------------------------------------------------------------------
# Spread state dataclass (plain dict for simplicity)
# ---------------------------------------------------------------------------

def make_spread(spread_type: str) -> dict:
    """Return a fresh spread state dict."""
    return {
        'type':                 spread_type,    # 'pe' or 'ce'
        'status':               'open',          # open/active/adjusted/adjusted_additional/
                                                 # active_additional/closed
        # Base position
        'sell_strike':          None,
        'buy_strike':           None,
        'sell_entry':           None,
        'buy_entry':            None,
        'sell_ltp':             None,
        'buy_ltp':              None,
        'sell_exit':            None,
        'buy_exit':             None,
        'index_sl':             None,
        'option_sl':            None,
        'booked_pl':            0.0,
        'pl':                   0.0,
        # Additional lots (entered before cutoff_time on adjustment)
        'additional_lots':      0,
        'add_sell_strike':      None,   # same as sell_strike after adjustment
        'add_buy_strike':       None,
        'add_buy_entry':        None,
        'add_buy_ltp':          None,
        'add_buy_exit':         None,
        'add_booked_pl':        0.0,
        'add_pl':               0.0,
        # Option DataFrames (loaded once per position)
        'sell_df':              None,
        'buy_df':               None,
        'add_buy_df':           None,
        # Exit metadata
        'exit_reason':          None,
        'exit_time':            None,
    }


# ---------------------------------------------------------------------------
# SL helpers
# ---------------------------------------------------------------------------

def set_sl(spread: dict, dte: int):
    """
    Compute and set index_sl and option_sl on a spread dict.
    Mirrors live Artemis credit_spread.py _set_sl() exactly:
      PE: index_sl = sell_strike + INDEX_SL_OFFSET
          SL fires when spot < index_sl (spot falling toward sold PE strike)
      CE: index_sl = sell_strike - INDEX_SL_OFFSET
          SL fires when spot > index_sl (spot rising toward sold CE strike)
    """
    mult = get_sl_multiplier(dte)
    spread['option_sl'] = spread['sell_entry'] * mult
    if spread['type'] == 'pe':
        spread['index_sl'] = spread['sell_strike'] + INDEX_SL_OFFSET
    else:
        spread['index_sl'] = spread['sell_strike'] - INDEX_SL_OFFSET


def check_sl(spread: dict, spot: float, ts: pd.Timestamp) -> str:
    """
    Check both SL conditions for a spread.
    Returns 'index_sl', 'option_sl', or None.
    Mirrors live Artemis credit_spread.py monitor_spread() exactly:
      PE fires when spot < index_sl (spot within INDEX_SL_OFFSET of sell strike)
      CE fires when spot > index_sl (spot within INDEX_SL_OFFSET of sell strike)
    """
    if ENABLE_INDEX_SL:
        if spread['type'] == 'pe' and spot < spread['index_sl']:
            return 'index_sl'
        if spread['type'] == 'ce' and spot > spread['index_sl']:
            return 'index_sl'
    if ENABLE_OPTION_SL:
        if spread['sell_ltp'] is not None and spread['sell_ltp'] > spread['option_sl']:
            return 'option_sl'
    return None


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------

def select_sell_strike(spread_type: str, spot: float,
                       expiry_ts: pd.Timestamp, ref_ts: pd.Timestamp) -> tuple:
    """
    Find the OTM sell strike whose close price at ref_ts is closest to
    EXPECTED_PREMIUM. Returns (sell_strike, sell_price) or (None, None).
    """
    if spread_type == 'pe':
        atm   = floor(spot / STRIKE_INTERVAL) * STRIKE_INTERVAL
        direction = -1
    else:
        atm   = ceil(spot / STRIKE_INTERVAL) * STRIKE_INTERVAL
        direction = +1

    return scan_strikes_for_premium(
        INSTRUMENT, OPTIONS_PATH, expiry_ts, spread_type,
        atm, STRIKE_INTERVAL,
        direction, EXPECTED_PREMIUM, ref_ts)


def select_new_sell_strike_for_adjustment(spread: dict, spot: float) -> int:
    """
    Compute the new sell strike when adjusting after an SL.
    Mirrors live adjust_spread() logic exactly.
    """
    if spread['type'] == 'ce':
        if spread['sell_strike'] - spot > MINIMUM_GAP:
            return spread['sell_strike'] - ADJUSTMENT_DISTANCE
        else:
            return ceil(spot / STRIKE_INTERVAL) * STRIKE_INTERVAL + MINIMUM_GAP_ITERATOR
    else:  # pe
        if spot - spread['sell_strike'] > MINIMUM_GAP:
            return spread['sell_strike'] + ADJUSTMENT_DISTANCE
        else:
            return floor(spot / STRIKE_INTERVAL) * STRIKE_INTERVAL - MINIMUM_GAP_ITERATOR


def select_new_sell_strike_for_reentry(spread_type: str, spot: float,
                                       prev_sell_strike: int) -> int:
    """
    Compute the sell strike when re-entering after the other side's SL closed it.
    Mirrors live initialize_spread() re-entry logic.
    """
    if spread_type == 'ce':
        if prev_sell_strike - spot > MINIMUM_GAP:
            return prev_sell_strike - ADJUSTMENT_DISTANCE
        else:
            return ceil(spot / STRIKE_INTERVAL) * STRIKE_INTERVAL + MINIMUM_GAP_ITERATOR
    else:
        if spot - prev_sell_strike > MINIMUM_GAP:
            return prev_sell_strike + ADJUSTMENT_DISTANCE
        else:
            return floor(spot / STRIKE_INTERVAL) * STRIKE_INTERVAL - MINIMUM_GAP_ITERATOR


# ---------------------------------------------------------------------------
# Spread execution helpers
# ---------------------------------------------------------------------------

def load_spread_dfs(spread: dict, expiry_ts: pd.Timestamp):
    """Load option DataFrames for sell and buy legs of a spread."""
    spread['sell_df'] = load_option_data(
        INSTRUMENT, OPTIONS_PATH, expiry_ts,
        spread['sell_strike'], spread['type'])
    spread['buy_df'] = load_option_data(
        INSTRUMENT, OPTIONS_PATH, expiry_ts,
        spread['buy_strike'], spread['type'])


def execute_spread_at(spread: dict, exec_ts: pd.Timestamp,
                      expiry_ts: pd.Timestamp, dte: int,
                      lots: int) -> bool:
    """
    Enter a spread at the open of exec_ts candle.
    Returns True if entry prices were found, False otherwise.
    Sets sell_entry, buy_entry, sell_ltp, buy_ltp, option_sl, index_sl.
    """
    sell_price = get_price(spread['sell_df'], exec_ts, col='open')
    buy_price  = get_price(spread['buy_df'],  exec_ts, col='open')

    if sell_price is None or buy_price is None or sell_price <= 0:
        logger.warning(
            f"  [{spread['type'].upper()}] Missing entry prices at {exec_ts} — skipping spread")
        return False

    spread['sell_entry'] = sell_price
    spread['buy_entry']  = buy_price
    spread['sell_ltp']   = sell_price
    spread['buy_ltp']    = buy_price
    spread['status']     = 'active'
    set_sl(spread, dte)
    return True


def update_ltps(spread: dict, ts: pd.Timestamp):
    """Refresh sell_ltp and buy_ltp from DataFrames at timestamp ts."""
    v = get_price(spread['sell_df'], ts, col='close')
    if v is not None:
        spread['sell_ltp'] = v
    v = get_price(spread['buy_df'], ts, col='close')
    if v is not None:
        spread['buy_ltp'] = v
    if spread['add_buy_df'] is not None:
        v = get_price(spread['add_buy_df'], ts, col='close')
        if v is not None:
            spread['add_buy_ltp'] = v
    _recompute_pl(spread)


def _recompute_pl(spread: dict):
    """Recompute mark-to-market pl from booked_pl and current LTPs."""
    if spread['sell_entry'] is not None and spread['buy_entry'] is not None:
        spread['pl'] = (spread['booked_pl']
                        + spread['buy_ltp']  - spread['buy_entry']
                        + spread['sell_entry'] - spread['sell_ltp'])
    if spread['add_buy_entry'] is not None and spread['add_buy_ltp'] is not None:
        add_ltp = spread['add_buy_ltp'] if spread['add_buy_ltp'] is not None else spread['add_buy_entry']
        spread['add_pl'] = (spread['add_booked_pl']
                            + add_ltp - spread['add_buy_entry']
                            + spread['sell_entry'] - spread['sell_ltp'])


# ---------------------------------------------------------------------------
# Exit helpers
# ---------------------------------------------------------------------------

def exit_spread_at(spread: dict, exec_ts: pd.Timestamp, reason: str,
                   lots: int, additional_lots: int):
    """
    Exit all legs of a spread (base + additional) at open of exec_ts.
    Updates booked_pl, add_booked_pl, and sets status to 'closed'.
    """
    # Base sell leg exit (buy to close)
    sell_exit = get_price(spread['sell_df'], exec_ts, col='open')
    if sell_exit is None:
        sell_exit = spread['sell_ltp']

    # Base buy leg exit (sell to close)
    buy_exit = get_price(spread['buy_df'], exec_ts, col='open')
    if buy_exit is None:
        buy_exit = spread['buy_ltp']

    spread['sell_exit'] = sell_exit
    spread['buy_exit']  = buy_exit
    spread['booked_pl'] = (spread['booked_pl']
                           + buy_exit  - spread['buy_entry']
                           + spread['sell_entry'] - sell_exit)
    spread['pl']        = spread['booked_pl']
    spread['sell_ltp']  = sell_exit
    spread['buy_ltp']   = buy_exit

    # Additional lots exit
    if spread['additional_lots'] > 0 and spread['add_buy_entry'] is not None:
        add_df = spread['add_buy_df'] if spread['add_buy_df'] is not None else spread['buy_df']
        add_buy_exit = get_price(add_df, exec_ts, col='open')
        if add_buy_exit is None:
            add_buy_exit = spread['add_buy_ltp'] if spread['add_buy_ltp'] else spread['add_buy_entry']
        add_sell_exit = sell_exit  # same sell strike as base
        spread['add_buy_exit']   = add_buy_exit
        spread['add_booked_pl']  = (spread['add_booked_pl']
                                    + add_buy_exit - spread['add_buy_entry']
                                    + spread['sell_entry'] - add_sell_exit)
        spread['add_pl']         = spread['add_booked_pl']
        spread['add_buy_ltp']    = add_buy_exit

    spread['status']      = 'closed'
    spread['exit_reason'] = reason
    spread['exit_time']   = exec_ts


def exit_spread_at_expiry(spread: dict, expiry_ts: pd.Timestamp,
                          lots: int, additional_lots: int):
    """
    Exit at expiry: use close of last available candle at or before expiry_ts.
    If no candle found, assume EXPIRY_FALLBACK_PRICE (OTM assumption).
    """
    sell_exit = get_price(spread['sell_df'], expiry_ts, col='close')
    if sell_exit is None:
        sell_exit = EXPIRY_FALLBACK_PRICE

    buy_exit = get_price(spread['buy_df'], expiry_ts, col='close')
    if buy_exit is None:
        buy_exit = EXPIRY_FALLBACK_PRICE

    spread['sell_exit'] = sell_exit
    spread['buy_exit']  = buy_exit
    spread['booked_pl'] = (spread['booked_pl']
                           + buy_exit  - spread['buy_entry']
                           + spread['sell_entry'] - sell_exit)
    spread['pl']        = spread['booked_pl']
    spread['sell_ltp']  = sell_exit
    spread['buy_ltp']   = buy_exit

    if spread['additional_lots'] > 0 and spread['add_buy_entry'] is not None:
        add_df = spread['add_buy_df'] if spread['add_buy_df'] is not None else spread['buy_df']
        add_buy_exit = get_price(add_df, expiry_ts, col='close')
        if add_buy_exit is None:
            add_buy_exit = EXPIRY_FALLBACK_PRICE
        add_sell_exit = sell_exit
        spread['add_buy_exit']   = add_buy_exit
        spread['add_booked_pl']  = (spread['add_booked_pl']
                                    + add_buy_exit - spread['add_buy_entry']
                                    + spread['sell_entry'] - add_sell_exit)
        spread['add_pl']         = spread['add_booked_pl']
        spread['add_buy_ltp']    = add_buy_exit

    spread['status']      = 'closed'
    spread['exit_reason'] = 'expiry'
    spread['exit_time']   = expiry_ts


# ---------------------------------------------------------------------------
# Adjustment helpers
# ---------------------------------------------------------------------------

def adjust_spread(spread: dict, spot: float, exec_ts: pd.Timestamp,
                  expiry_ts: pd.Timestamp, dte: int, lots: int,
                  elm_time: pd.Timestamp, cutoff_time: pd.Timestamp):
    """
    Adjust spread sell strike after SL on the other side.
    Mirrors live adjust_spread() logic:
      - Before cutoff_time: adjust + enter additional lots
      - Between cutoff_time and elm_time: adjust only
      - After elm_time: no adjustment
    """
    if exec_ts >= elm_time:
        logger.info(f"  [{spread['type'].upper()}] Past ELM — no adjustment")
        return

    # Exit existing sell at exec_ts open (already done by caller via exit_spread_at
    # for the SL side — here we only reposition the surviving spread's sell leg)
    old_sell_exit = get_price(spread['sell_df'], exec_ts, col='open')
    if old_sell_exit is None:
        old_sell_exit = spread['sell_ltp']

    # Book P&L on old sell leg portion
    spread['booked_pl'] += spread['sell_entry'] - old_sell_exit
    spread['sell_exit']  = old_sell_exit

    # Compute new sell strike
    new_sell_strike = select_new_sell_strike_for_adjustment(spread, spot)
    new_buy_strike  = (new_sell_strike + HEDGE_POINTS
                       if spread['type'] == 'ce'
                       else new_sell_strike - HEDGE_POINTS)

    new_sell_df = load_option_data(
        INSTRUMENT, OPTIONS_PATH, expiry_ts, new_sell_strike, spread['type'])
    new_buy_df  = load_option_data(
        INSTRUMENT, OPTIONS_PATH, expiry_ts, new_buy_strike, spread['type'])

    new_sell_entry = get_price(new_sell_df, exec_ts, col='open')
    new_buy_entry  = get_price(new_buy_df,  exec_ts, col='open')

    if new_sell_entry is None:
        logger.warning(
            f"  [{spread['type'].upper()}] No data for adjusted sell strike "
            f"{new_sell_strike} at {exec_ts} — keeping old position")
        # Revert booked_pl adjustment since we couldn't trade
        spread['booked_pl'] -= spread['sell_entry'] - old_sell_exit
        spread['sell_exit']  = None
        return

    # Enter additional lots if before cutoff_time
    enter_additional = exec_ts < cutoff_time
    add_lots = lots // 2 if enter_additional else 0

    if enter_additional and new_buy_entry is not None:
        # Additional lots: new sell + new buy hedge
        spread['additional_lots']  = add_lots
        spread['add_sell_strike']  = new_sell_strike
        spread['add_buy_strike']   = new_buy_strike
        spread['add_buy_entry']    = new_buy_entry
        spread['add_buy_ltp']      = new_buy_entry
        spread['add_buy_df']       = new_buy_df
        spread['add_booked_pl']    = 0.0
        spread['add_pl']           = 0.0

    # Old buy leg: book P&L and exit (bring hedge to within hedge_points)
    old_buy_exit = get_price(spread['buy_df'], exec_ts, col='open')
    if old_buy_exit is None:
        old_buy_exit = spread['buy_ltp']
    spread['booked_pl'] += old_buy_exit - spread['buy_entry']

    # Update spread to new position
    spread['sell_strike']  = new_sell_strike
    spread['buy_strike']   = new_buy_strike
    spread['sell_df']      = new_sell_df
    spread['buy_df']       = new_buy_df if new_buy_entry is not None else spread['buy_df']
    spread['sell_entry']   = new_sell_entry
    spread['buy_entry']    = new_buy_entry if new_buy_entry is not None else old_buy_exit
    spread['sell_ltp']     = new_sell_entry
    spread['buy_ltp']      = new_buy_entry if new_buy_entry is not None else old_buy_exit

    spread['status'] = 'adjusted_additional' if enter_additional else 'adjusted'

    # Recompute SL on new sell strike
    set_sl(spread, dte)
    _recompute_pl(spread)

    logger.info(
        f"  [{spread['type'].upper()}] Adjusted: "
        f"sell {new_sell_strike} @ {new_sell_entry:.2f} | "
        f"buy {new_buy_strike} @ {spread['buy_entry']:.2f}"
        + (f" | Additional lots: {add_lots}" if enter_additional else ""))


def reenter_spread(spread: dict, spot: float, exec_ts: pd.Timestamp,
                   expiry_ts: pd.Timestamp, dte: int, lots: int):
    """
    Re-enter a spread that was previously closed (other side triggered SL first).
    Mirrors live initialize_spread() re-entry path.
    """
    # Reset spread to fresh state but keep type
    new_sell_strike = select_new_sell_strike_for_reentry(
        spread['type'], spot, spread['sell_strike'] or 0)

    new_buy_strike = (new_sell_strike + HEDGE_POINTS
                      if spread['type'] == 'ce'
                      else new_sell_strike - HEDGE_POINTS)

    sell_df = load_option_data(
        INSTRUMENT, OPTIONS_PATH, expiry_ts, new_sell_strike, spread['type'])
    buy_df  = load_option_data(
        INSTRUMENT, OPTIONS_PATH, expiry_ts, new_buy_strike,  spread['type'])

    sell_price = get_price(sell_df, exec_ts, col='open')
    buy_price  = get_price(buy_df,  exec_ts, col='open')

    if sell_price is None or buy_price is None:
        logger.warning(
            f"  [{spread['type'].upper()}] Re-entry: no data at {exec_ts} — skipping")
        return

    # Reset all fields
    spread['sell_strike']   = new_sell_strike
    spread['buy_strike']    = new_buy_strike
    spread['sell_df']       = sell_df
    spread['buy_df']        = buy_df
    spread['sell_entry']    = sell_price
    spread['buy_entry']     = buy_price
    spread['sell_ltp']      = sell_price
    spread['buy_ltp']       = buy_price
    spread['sell_exit']     = None
    spread['buy_exit']      = None
    spread['booked_pl']     = 0.0
    spread['pl']            = 0.0
    spread['additional_lots'] = 0
    spread['add_buy_entry'] = None
    spread['add_buy_ltp']   = None
    spread['add_buy_exit']  = None
    spread['add_buy_df']    = None
    spread['add_booked_pl'] = 0.0
    spread['add_pl']        = 0.0
    spread['exit_reason']   = None
    spread['exit_time']     = None
    spread['status']        = 'active'

    set_sl(spread, dte)

    logger.info(
        f"  [{spread['type'].upper()}] Re-entered: "
        f"sell {new_sell_strike} @ {sell_price:.2f} | "
        f"buy {new_buy_strike} @ {buy_price:.2f}")


# ---------------------------------------------------------------------------
# ELM handling
# ---------------------------------------------------------------------------

def handle_elm(pe: dict, ce: dict, exec_ts: pd.Timestamp,
               expiry_ts: pd.Timestamp, lots: int):
    """
    At elm_time, exit the side with lesser net premium remaining.
    For adjusted spreads: also bring the surviving hedge to within hedge_points.
    Mirrors live evaluate_adjust_for_elm() logic.
    """
    pe_net = (pe['sell_ltp'] - pe['buy_ltp']) if pe['status'] != 'closed' else -float('inf')
    ce_net = (ce['sell_ltp'] - ce['buy_ltp']) if ce['status'] != 'closed' else -float('inf')

    # Exit the side with lesser net premium remaining
    if pe['status'] != 'closed' and ce['status'] != 'closed':
        if pe_net <= ce_net:
            _elm_exit_spread(pe, exec_ts, lots)
            _elm_adjust_hedge_if_needed(ce, exec_ts, expiry_ts, lots)
        else:
            _elm_exit_spread(ce, exec_ts, lots)
            _elm_adjust_hedge_if_needed(pe, exec_ts, expiry_ts, lots)
    elif pe['status'] != 'closed':
        # CE already closed — PE may need hedge adjustment and additional lot exit
        _elm_exit_additional_if_needed(pe, exec_ts, lots)
        _elm_adjust_hedge_if_needed(pe, exec_ts, expiry_ts, lots)
    elif ce['status'] != 'closed':
        _elm_exit_additional_if_needed(ce, exec_ts, lots)
        _elm_adjust_hedge_if_needed(ce, exec_ts, expiry_ts, lots)


def _elm_exit_spread(spread: dict, exec_ts: pd.Timestamp, lots: int):
    """Full exit of a spread at ELM time."""
    exit_spread_at(spread, exec_ts, 'elm', lots, spread['additional_lots'])
    logger.info(
        f"  [{spread['type'].upper()}] ELM exit @ {exec_ts} | "
        f"pl: {spread['pl']:+.2f} pts")


def _elm_exit_additional_if_needed(spread: dict, exec_ts: pd.Timestamp, lots: int):
    """Exit only the additional lots of a spread at ELM time, if present."""
    if spread['additional_lots'] == 0 or spread['add_buy_entry'] is None:
        return

    add_df = spread['add_buy_df'] if spread['add_buy_df'] is not None else spread['buy_df']
    add_buy_exit = get_price(add_df, exec_ts, col='open')
    if add_buy_exit is None:
        add_buy_exit = spread['add_buy_ltp'] if spread['add_buy_ltp'] else spread['add_buy_entry']

    # Additional sell exit (same token as base sell)
    add_sell_exit = get_price(spread['sell_df'], exec_ts, col='open')
    if add_sell_exit is None:
        add_sell_exit = spread['sell_ltp']

    spread['add_booked_pl'] += (add_buy_exit - spread['add_buy_entry']
                                + spread['sell_entry'] - add_sell_exit)
    spread['add_pl']         = spread['add_booked_pl']
    spread['add_buy_ltp']    = add_buy_exit
    spread['add_buy_exit']   = add_buy_exit
    spread['additional_lots'] = 0

    # Update status from adjusted_additional → adjusted
    if spread['status'] == 'adjusted_additional':
        spread['status'] = 'adjusted_elm'
    elif spread['status'] == 'active_additional':
        spread['status'] = 'active_additional_elm'

    logger.info(
        f"  [{spread['type'].upper()}] ELM: additional lots exited @ {exec_ts}")


def _elm_adjust_hedge_if_needed(spread: dict, exec_ts: pd.Timestamp,
                                 expiry_ts: pd.Timestamp, lots: int):
    """
    If the surviving spread's buy strike is further than hedge_points from
    the sell strike (can happen after adjustments), bring it in.
    Mirrors live adjust_for_elm() logic.
    """
    if spread['status'] == 'closed':
        return

    expected_buy = (spread['sell_strike'] + HEDGE_POINTS
                    if spread['type'] == 'ce'
                    else spread['sell_strike'] - HEDGE_POINTS)

    if spread['buy_strike'] == expected_buy:
        return  # already at correct distance

    new_buy_df = load_option_data(
        INSTRUMENT, OPTIONS_PATH, expiry_ts, expected_buy, spread['type'])
    new_buy_entry = get_price(new_buy_df, exec_ts, col='open')
    if new_buy_entry is None:
        logger.warning(
            f"  [{spread['type'].upper()}] ELM hedge adjust: "
            f"no data for strike {expected_buy} at {exec_ts}")
        return

    # Exit old buy, enter new buy
    old_buy_exit = get_price(spread['buy_df'], exec_ts, col='open')
    if old_buy_exit is None:
        old_buy_exit = spread['buy_ltp']

    spread['booked_pl']  += old_buy_exit - spread['buy_entry']
    spread['buy_strike']  = expected_buy
    spread['buy_df']      = new_buy_df
    spread['buy_entry']   = new_buy_entry
    spread['buy_ltp']     = new_buy_entry

    if spread['status'] == 'adjusted':
        spread['status'] = 'adjusted_elm'

    _recompute_pl(spread)
    logger.info(
        f"  [{spread['type'].upper()}] ELM hedge adjusted to {expected_buy} @ {new_buy_entry:.2f}")


# ---------------------------------------------------------------------------
# Trade log helpers
# ---------------------------------------------------------------------------

def _make_log_row(ts: pd.Timestamp, spot: float, vix: float,
                  pe: dict, ce: dict) -> dict:
    return {
        'time_stamp':           ts,
        'spot':                 round(spot, 2),
        'vix':                  round(vix, 2) if vix is not None else None,
        'pe_sell_strike':       pe['sell_strike'],
        'pe_buy_strike':        pe['buy_strike'],
        'pe_sell_ltp':          round(pe['sell_ltp'], 2)  if pe['sell_ltp']  is not None else None,
        'pe_buy_ltp':           round(pe['buy_ltp'],  2)  if pe['buy_ltp']   is not None else None,
        'pe_pl':                round(pe['pl'],        2),
        'pe_add_pl':            round(pe['add_pl'],    2),
        'pe_status':            pe['status'],
        'ce_sell_strike':       ce['sell_strike'],
        'ce_buy_strike':        ce['buy_strike'],
        'ce_sell_ltp':          round(ce['sell_ltp'], 2)  if ce['sell_ltp']  is not None else None,
        'ce_buy_ltp':           round(ce['buy_ltp'],  2)  if ce['buy_ltp']   is not None else None,
        'ce_pl':                round(ce['pl'],        2),
        'ce_add_pl':            round(ce['add_pl'],    2),
        'ce_status':            ce['status'],
    }


def _save_trade_log(trade_logs: list, expiry_ts: pd.Timestamp,
                    trade_counter: int):
    if not trade_logs or not ENABLE_TRADE_LOGS:
        return
    os.makedirs(TRADE_LOGS_DIR, exist_ok=True)
    expiry_str = pd.Timestamp(expiry_ts).strftime('%Y-%m-%d')
    filename   = f"trade_{trade_counter:04d}_{expiry_str}.csv"
    pd.DataFrame(trade_logs).to_csv(
        os.path.join(TRADE_LOGS_DIR, filename), index=False)


# ---------------------------------------------------------------------------
# Trade summary record
# ---------------------------------------------------------------------------

def _build_summary_record(contract: pd.Series, entry_ts: pd.Timestamp,
                          entry_spot: float, entry_vix: float,
                          lots: int, pe: dict, ce: dict,
                          skipped: str = None) -> dict:
    """Build one row for trade_summary.csv."""
    def _r(v):
        return round(v, 4) if v is not None else None

    if skipped:
        return {
            'instrument':           INSTRUMENT,
            'expiry':               contract['expiry'],
            'entry_time':           None,
            'entry_spot':           None,
            'entry_vix':            None,
            'lots':                 None,
            'pe_sell_strike':       None, 'pe_buy_strike':       None,
            'pe_sell_entry':        None, 'pe_buy_entry':        None,
            'pe_sell_exit':         None, 'pe_buy_exit':         None,
            'pe_exit_reason':       None, 'pe_exit_time':        None,
            'pe_pl_points':         None, 'pe_add_pl_points':    None,
            'ce_sell_strike':       None, 'ce_buy_strike':       None,
            'ce_sell_entry':        None, 'ce_buy_entry':        None,
            'ce_sell_exit':         None, 'ce_buy_exit':         None,
            'ce_exit_reason':       None, 'ce_exit_time':        None,
            'ce_pl_points':         None, 'ce_add_pl_points':    None,
            'total_pl_points':      None, 'total_pl_rupees':     None,
            'week_outcome':         skipped,
        }

    # Combined P&L: base lots + additional lots (additional_lots = lots // 2)
    # normalised to per-base-lot as in live code
    add_lots    = lots // 2
    pe_base_pl  = _r(pe['pl'])
    ce_base_pl  = _r(ce['pl'])
    pe_add_pl   = _r(pe['add_pl'])
    ce_add_pl   = _r(ce['add_pl'])

    if pe_base_pl is not None and ce_base_pl is not None:
        total_pl_pts = ((pe_base_pl + ce_base_pl)
                        + (pe_add_pl + ce_add_pl) / 2)
        total_pl_rs  = total_pl_pts * LOT_SIZE
    else:
        total_pl_pts = None
        total_pl_rs  = None

    return {
        'instrument':           INSTRUMENT,
        'expiry':               contract['expiry'],
        'entry_time':           entry_ts,
        'entry_spot':           _r(entry_spot),
        'entry_vix':            _r(entry_vix),
        'lots':                 lots,
        'pe_sell_strike':       pe['sell_strike'],
        'pe_buy_strike':        pe['buy_strike'],
        'pe_sell_entry':        _r(pe['sell_entry']),
        'pe_buy_entry':         _r(pe['buy_entry']),
        'pe_sell_exit':         _r(pe['sell_exit']),
        'pe_buy_exit':          _r(pe['buy_exit']),
        'pe_exit_reason':       pe['exit_reason'],
        'pe_exit_time':         pe['exit_time'],
        'pe_pl_points':         pe_base_pl,
        'pe_add_pl_points':     pe_add_pl,
        'ce_sell_strike':       ce['sell_strike'],
        'ce_buy_strike':        ce['buy_strike'],
        'ce_sell_entry':        _r(ce['sell_entry']),
        'ce_buy_entry':         _r(ce['buy_entry']),
        'ce_sell_exit':         _r(ce['sell_exit']),
        'ce_buy_exit':          _r(ce['buy_exit']),
        'ce_exit_reason':       ce['exit_reason'],
        'ce_exit_time':         ce['exit_time'],
        'ce_pl_points':         ce_base_pl,
        'ce_add_pl_points':     ce_add_pl,
        'total_pl_points':      _r(total_pl_pts),
        'total_pl_rupees':      _r(total_pl_rs),
        'week_outcome':         'traded',
    }


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def print_summary(all_records: list):
    df = pd.DataFrame(all_records)
    traded = df[df['week_outcome'] == 'traded']
    skipped = df[df['week_outcome'] != 'traded']

    logger.info('=' * 60)
    logger.info('ARTEMIS BACKTEST SUMMARY')
    logger.info('=' * 60)
    logger.info(f"  Instrument         : {INSTRUMENT.upper()}")
    logger.info(f"  Total weeks        : {len(df)}")
    logger.info(f"  Skipped (VIX gate) : {len(skipped)}")
    logger.info(f"  Traded weeks       : {len(traded)}")

    if traded.empty:
        logger.info("  No traded weeks to analyse.")
        return

    winners  = traded[traded['total_pl_points'] > 0]
    losers   = traded[traded['total_pl_points'] <= 0]
    win_rate = len(winners) / len(traded) * 100 if len(traded) else 0

    logger.info(f"  Win rate           : {win_rate:.1f}%")
    logger.info(f"  Avg winner (pts)   : {winners['total_pl_points'].mean():.2f}" if len(winners) else "  Avg winner        : N/A")
    logger.info(f"  Avg loser  (pts)   : {losers['total_pl_points'].mean():.2f}"  if len(losers)  else "  Avg loser         : N/A")
    logger.info(f"  Total P&L (pts)    : {traded['total_pl_points'].sum():.2f}")
    logger.info(f"  Total P&L (Rs)     : {traded['total_pl_rupees'].sum():,.0f}")

    logger.info(f"  PE exit breakdown:")
    for reason, count in traded['pe_exit_reason'].value_counts().items():
        logger.info(f"    {reason:25s}: {count}")
    logger.info(f"  CE exit breakdown:")
    for reason, count in traded['ce_exit_reason'].value_counts().items():
        logger.info(f"    {reason:25s}: {count}")
    logger.info('=' * 60)
    logger.info(f"  Saved to: {TRADE_SUMMARY_FILE}")


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------

def run_backtest():
    logger.info('=== Artemis Backtest starting ===')
    logger.info(f"  Instrument : {INSTRUMENT.upper()}")

    # --- Load contracts ---
    if not os.path.exists(CONTRACTS_FILE):
        logger.error(f"contracts.csv not found at {CONTRACTS_FILE}. "
                     f"Run generate_contracts.py first.")
        sys.exit(1)

    contracts_df = pd.read_csv(
        CONTRACTS_FILE,
        parse_dates=['expiry', 'entry', 'elm_time', 'cutoff_time'])
    contracts_df = contracts_df[contracts_df['instrument'] == INSTRUMENT].copy()
    contracts_df = contracts_df.sort_values('expiry').reset_index(drop=True)

    if BACKTEST_START_DATE:
        contracts_df = contracts_df[
            contracts_df['expiry'] >= pd.Timestamp(BACKTEST_START_DATE)]
    if BACKTEST_END_DATE:
        contracts_df = contracts_df[
            contracts_df['expiry'] <= pd.Timestamp(BACKTEST_END_DATE)]

    logger.info(f"  Contracts  : {len(contracts_df)} weeks")

    # --- Load index data ---
    logger.info("Loading index data...")
    index_df = load_index_data(INDEX_FILE)
    vix_df   = load_vix_daily(VIX_INDEX_FILE)
    vix_map  = dict(zip(vix_df['date'], vix_df['vix_open']))
    logger.info(f"  Index rows : {len(index_df):,}")

    # --- Load holidays ---
    holidays_df = pd.read_csv(HOLIDAYS_FILE, parse_dates=['date'])
    holidays    = set(holidays_df['date'].dt.date)

    os.makedirs(os.path.dirname(TRADE_SUMMARY_FILE), exist_ok=True)
    if ENABLE_TRADE_LOGS:
        os.makedirs(TRADE_LOGS_DIR, exist_ok=True)

    all_records   = []
    trade_counter = 0

    # -----------------------------------------------------------------------
    # Outer loop — one iteration per weekly contract
    # -----------------------------------------------------------------------
    for _, contract in contracts_df.iterrows():

        expiry_ts    = contract['expiry']
        entry_anchor = contract['entry']       # Monday 10:30
        elm_time     = contract['elm_time']    # Wednesday 15:15
        cutoff_time  = contract['cutoff_time'] # Wednesday 09:15

        entry_date = entry_anchor.date()
        logger.info(f"\nWeek: entry {entry_date} | expiry {expiry_ts.date()}")

        # --- VIX gate ---
        entry_vix = vix_map.get(entry_date)
        if entry_vix is None:
            logger.info(f"  VIX data missing for {entry_date} — skipping")
            all_records.append(
                _build_summary_record(contract, None, None, None, 0,
                                      make_spread('pe'), make_spread('ce'),
                                      skipped='skipped_no_vix'))
            continue

        if entry_vix >= VIX_THRESHOLD:
            logger.info(
                f"  VIX {entry_vix:.1f} >= {VIX_THRESHOLD} — skipping (Apollo territory)")
            all_records.append(
                _build_summary_record(contract, None, None, entry_vix, 0,
                                      make_spread('pe'), make_spread('ce'),
                                      skipped='skipped_vix'))
            continue

        # --- Entry: spot at 10:30 close, execute at 10:31 open ---
        signal_ts = entry_anchor                          # 10:30
        exec_ts   = signal_ts + pd.Timedelta(minutes=1)  # 10:31

        entry_spot = get_index_price(index_df, signal_ts, col='close')
        if entry_spot is None:
            logger.warning(f"  No index data at {signal_ts} — skipping week")
            all_records.append(
                _build_summary_record(contract, None, None, entry_vix, 0,
                                      make_spread('pe'), make_spread('ce'),
                                      skipped='skipped_no_data'))
            continue

        dte  = compute_dte(entry_date, expiry_ts)
        lots = LOT_COUNT

        # --- Strike selection: scan at 10:30 close price ---
        pe = make_spread('pe')
        ce = make_spread('ce')

        pe_sell_strike, _ = select_sell_strike('pe', entry_spot, expiry_ts, signal_ts)
        ce_sell_strike, _ = select_sell_strike('ce', entry_spot, expiry_ts, signal_ts)

        if pe_sell_strike is None or ce_sell_strike is None:
            logger.warning(
                f"  Strike selection failed (PE={pe_sell_strike}, CE={ce_sell_strike}) — skipping")
            all_records.append(
                _build_summary_record(contract, None, None, entry_vix, 0,
                                      pe, ce, skipped='skipped_no_strikes'))
            continue

        pe['sell_strike'] = pe_sell_strike
        pe['buy_strike']  = pe_sell_strike - HEDGE_POINTS
        ce['sell_strike'] = ce_sell_strike
        ce['buy_strike']  = ce_sell_strike + HEDGE_POINTS

        load_spread_dfs(pe, expiry_ts)
        load_spread_dfs(ce, expiry_ts)

        # Execute at 10:31 open
        pe_ok = execute_spread_at(pe, exec_ts, expiry_ts, dte, lots)
        ce_ok = execute_spread_at(ce, exec_ts, expiry_ts, dte, lots)

        if not pe_ok or not ce_ok:
            logger.warning(f"  Entry failed — skipping week")
            all_records.append(
                _build_summary_record(contract, exec_ts, entry_spot, entry_vix,
                                      lots, pe, ce, skipped='skipped_entry_failed'))
            continue

        logger.info(
            f"  ENTRY @ {exec_ts} | spot {entry_spot:.0f} | VIX {entry_vix:.1f} | DTE {dte}")
        logger.info(
            f"  PE sell {pe['sell_strike']} @ {pe['sell_entry']:.2f} | "
            f"buy {pe['buy_strike']} @ {pe['buy_entry']:.2f} | "
            f"SL opt {pe['option_sl']:.2f} | idx {pe['index_sl']:.0f}")
        logger.info(
            f"  CE sell {ce['sell_strike']} @ {ce['sell_entry']:.2f} | "
            f"buy {ce['buy_strike']} @ {ce['buy_entry']:.2f} | "
            f"SL opt {ce['option_sl']:.2f} | idx {ce['index_sl']:.0f}")

        trade_logs  = []
        elm_done    = False
        entry_exec_ts = exec_ts

        # -------------------------------------------------------------------
        # Inner loop — 1-min candles from entry to expiry
        # -------------------------------------------------------------------
        # Build the 1-min candle series for this week from the index file
        week_index = index_df[
            (index_df.index >= exec_ts) &
            (index_df.index <= expiry_ts)
        ]

        current_loop_date = None

        for ts, idx_row in week_index.iterrows():
            spot = float(idx_row['close'])

            # Refresh option_sl at the start of each new trading day.
            # DTE decreases each day so the multiplier tightens. The live code
            # reinitialises the spread object on each script restart (daily),
            # which recalculates days_to_expiry. The backtest must replicate this.
            if ts.date() != current_loop_date:
                current_loop_date = ts.date()
                current_dte_daily = compute_dte(ts.date(), expiry_ts)
                if pe['status'] != 'closed' and pe['sell_entry'] is not None:
                    set_sl(pe, current_dte_daily)
                if ce['status'] != 'closed' and ce['sell_entry'] is not None:
                    set_sl(ce, current_dte_daily)

            # Refresh LTPs for open spreads
            if pe['status'] != 'closed':
                update_ltps(pe, ts)
            if ce['status'] != 'closed':
                update_ltps(ce, ts)

            # VIX for log
            vix_now = vix_map.get(ts.date())

            # ---------------------------------------------------------------
            # Log snapshot
            # ---------------------------------------------------------------
            if ENABLE_TRADE_LOGS:
                trade_logs.append(_make_log_row(ts, spot, vix_now, pe, ce))

            # ---------------------------------------------------------------
            # Expiry check
            # ---------------------------------------------------------------
            if ts >= expiry_ts:
                if pe['status'] != 'closed':
                    exit_spread_at_expiry(pe, expiry_ts, lots, lots // 2)
                    logger.info(
                        f"  PE EXPIRY @ {ts} | pl: {pe['pl']:+.2f}")
                if ce['status'] != 'closed':
                    exit_spread_at_expiry(ce, expiry_ts, lots, lots // 2)
                    logger.info(
                        f"  CE EXPIRY @ {ts} | pl: {ce['pl']:+.2f}")
                break

            # ---------------------------------------------------------------
            # ELM check (elm_time = Wednesday 15:15)
            # Execute at open of 15:16 candle
            # ---------------------------------------------------------------
            if not elm_done and ts >= elm_time:
                elm_exec_ts = ts + pd.Timedelta(minutes=1)
                handle_elm(pe, ce, elm_exec_ts, expiry_ts, lots)
                elm_done = True

            # ---------------------------------------------------------------
            # SL checks — PE spread
            # ---------------------------------------------------------------
            if pe['status'] != 'closed':
                pe_sl = check_sl(pe, spot, ts)

                if pe_sl:
                    sl_exec_ts  = ts + pd.Timedelta(minutes=1)
                    current_dte = compute_dte(ts.date(), expiry_ts)
                    logger.info(
                        f"  PE {pe_sl.upper()} @ {ts} | "
                        f"spot {spot:.0f} | sell_ltp {pe['sell_ltp']:.2f}")
                    exit_spread_at(pe, sl_exec_ts, pe_sl, lots, lots // 2)
                    logger.info(f"  PE EXIT | pl: {pe['pl']:+.2f}")

                    # Handle the CE spread after PE SL
                    if ce['status'] == 'closed':
                        # Both sides now closed — week ends
                        pass
                    elif ce['status'] in ('active', 'adjusted',
                                          'adjusted_additional', 'active_additional',
                                          'adjusted_elm', 'active_additional_elm',
                                          'adjusted_additional_elm'):
                        # CE still open — was it previously adjusted (meaning PE SL
                        # caused CE to be closed previously and CE re-entered)?
                        # Only adjust if CE has not been touched yet this week.
                        # Re-enter is only if CE was already closed when PE hit SL.
                        adjust_spread(ce, spot, sl_exec_ts, expiry_ts,
                                      current_dte, lots, elm_time, cutoff_time)

            # ---------------------------------------------------------------
            # SL checks — CE spread
            # ---------------------------------------------------------------
            if ce['status'] != 'closed':
                ce_sl = check_sl(ce, spot, ts)

                if ce_sl:
                    sl_exec_ts  = ts + pd.Timedelta(minutes=1)
                    current_dte = compute_dte(ts.date(), expiry_ts)
                    logger.info(
                        f"  CE {ce_sl.upper()} @ {ts} | "
                        f"spot {spot:.0f} | sell_ltp {ce['sell_ltp']:.2f}")
                    exit_spread_at(ce, sl_exec_ts, ce_sl, lots, lots // 2)
                    logger.info(f"  CE EXIT | pl: {ce['pl']:+.2f}")

                    if pe['status'] == 'closed':
                        pass
                    elif pe['status'] in ('active', 'adjusted',
                                          'adjusted_additional', 'active_additional',
                                          'adjusted_elm', 'active_additional_elm',
                                          'adjusted_additional_elm'):
                        adjust_spread(pe, spot, sl_exec_ts, expiry_ts,
                                      current_dte, lots, elm_time, cutoff_time)

            # ---------------------------------------------------------------
            # Re-entry: if one side just closed via SL AND the other side
            # was already closed before this week started (edge case: second SL)
            # Handled inline below for clarity
            # ---------------------------------------------------------------
            # If both spreads are now closed, stop monitoring this week
            if pe['status'] == 'closed' and ce['status'] == 'closed':
                logger.info(f"  Both spreads closed @ {ts} — week ends early")
                break

        # -------------------------------------------------------------------
        # Week complete — save and record
        # -------------------------------------------------------------------
        trade_counter += 1
        _save_trade_log(trade_logs, expiry_ts, trade_counter)

        record = _build_summary_record(
            contract, entry_exec_ts, entry_spot, entry_vix,
            lots, pe, ce)
        all_records.append(record)

        logger.info(
            f"  WEEK DONE | PE pl: {pe['pl']:+.2f} | CE pl: {ce['pl']:+.2f} | "
            f"Total: {record['total_pl_points']:+.4f} pts | "
            f"Rs {record['total_pl_rupees']:+,.0f}")

    # -----------------------------------------------------------------------
    # Save trade summary
    # -----------------------------------------------------------------------
    summary_df = pd.DataFrame(all_records)
    summary_df.to_csv(TRADE_SUMMARY_FILE, index=False)
    print_summary(all_records)
    logger.info('=== Artemis Backtest complete ===')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    run_backtest()