"""
backtest_debit.py — Apollo Debit Spread Backtest Engine
Dual-timeframe Supertrend trend-following debit spread strategy.
Deployed only when India VIX > VIX_THRESHOLD.

Signal logic is identical to the credit spread (backtest.py).
Key structural differences:
  - Buy leg is ATM (or offset), sell leg is further OTM — net debit paid
  - Strike selection is offset-based, not delta-based (no mibian)
  - P&L = (buy_exit - buy_entry) - (sell_exit - sell_entry)
  - Max loss is structurally capped at net debit — no downside SL needed
  - Exit mechanisms: trend_flip (always), profit_target, time_gate,
    trailing_profit (all toggleable), expiry (always)

Run precompute.py first to generate intermediate files (shared with credit spread).

Execution model:
  - Signal fires on candle CLOSE
  - Entry/exit executes at OPEN of the next candle
  - Slippage applied per leg at entry and exit (not on expiry)
  - Per-trade 1-min log captures a snapshot every minute while in trade
"""

import os
import sys
import logging
import warnings
from datetime import timedelta
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from configs_debit import (
    NIFTY_INDEX_FILE, VIX_INDEX_FILE,
    NIFTY_OPTIONS_PATH, CONTRACT_LIST_FILE,
    NIFTY_15MIN_FILE, NIFTY_75MIN_FILE, VIX_DAILY_FILE,
    TRADE_LOGS_DIR, TRADE_SUMMARY_FILE,
    VIX_THRESHOLD,
    HEDGE_POINTS, STRIKE_STEP, MIN_DTE, BUY_LEG_OFFSET,
    NO_EXIT_BEFORE,
    ENABLE_PROFIT_TARGET, ENABLE_TIME_GATE, ENABLE_TRAILING_PROFIT,
    ENABLE_HARD_STOP, HARD_STOP_POINTS,
    ENABLE_DAY0_SPREAD_SL, DAY0_SPREAD_SL_PCT,
    PROFIT_TARGET_VIX_LOW, PROFIT_TARGET_VIX_HIGH,
    PROFIT_TARGET_PCT_LOW_VIX, PROFIT_TARGET_PCT_MID_VIX, PROFIT_TARGET_PCT_HIGH_VIX,
    TIME_GATE_DAYS, TIME_GATE_CHECK_TIME,
    TIME_GATE_VIX_THRESHOLD,
    TIME_GATE_MIN_PROFIT_PCT_LOW_VIX, TIME_GATE_MIN_PROFIT_PCT_HIGH_VIX,
    TRAIL_VIX_THRESHOLD,
    TRAIL_TRIGGER_1, TRAIL_FLOOR_1,
    TRAIL_TRIGGER_2, TRAIL_FLOOR_2,
    TRAIL_TRIGGER_3, TRAIL_FLOOR_3,
    ADDITIONAL_LOT_MULTIPLIER, ELM_SECONDS_BEFORE_EXPIRY, ENABLE_ADDITIONAL_LOTS,
    EXCLUDE_TRADE_DAYS, EXCLUDE_SIGNAL_CANDLES,
    PROFIT_TARGET_PCT_BULL, PROFIT_TARGET_PCT_BEAR,
    TIME_GATE_MIN_PROFIT_PCT_BULL, TIME_GATE_MIN_PROFIT_PCT_BEAR,
    TIME_GATE_DAYS_BULL, TIME_GATE_DAYS_BEAR,
    HARD_STOP_POINTS_BULL, HARD_STOP_POINTS_BEAR,
    EXCLUDE_BULLISH_VIX_ABOVE,
    SLIPPAGE_POINTS, LOT_SIZE,
    SPREAD_TYPE,
    BACKTEST_START_DATE, BACKTEST_END_DATE,
)

warnings.filterwarnings('ignore')

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data loading  (identical to credit spread — shared precomputed files)
# ---------------------------------------------------------------------------

def load_precomputed():
    """Load precomputed 15-min, 75-min Supertrend data and daily VIX."""
    logger.info("Loading precomputed data...")

    nifty_15  = pd.read_csv(NIFTY_15MIN_FILE, parse_dates=['time_stamp'])
    nifty_75  = pd.read_csv(NIFTY_75MIN_FILE, parse_dates=['time_stamp'])
    vix_daily = pd.read_csv(VIX_DAILY_FILE)
    vix_daily['date'] = pd.to_datetime(vix_daily['date']).dt.date

    for df in [nifty_15, nifty_75]:
        df['trend'] = df['trend'].map(
            {'True': True, 'False': False, True: True, False: False})
        df['trend_flip'] = df['trend_flip'].map(
            {'True': True, 'False': False, True: True, False: False})

    logger.info(f"  15-min : {len(nifty_15):,} candles")
    logger.info(f"  75-min : {len(nifty_75):,} candles")
    logger.info(f"  VIX    : {len(vix_daily):,} days")
    return nifty_15, nifty_75, vix_daily


def load_1min_data():
    """Load raw 1-min Nifty spot and VIX data."""
    logger.info("Loading 1-min index data...")

    nifty_1m = pd.read_csv(NIFTY_INDEX_FILE, parse_dates=['time_stamp'])
    nifty_1m['time_stamp'] = pd.to_datetime(
        nifty_1m['time_stamp'], utc=False).dt.tz_localize(None)

    vix_1m = pd.read_csv(VIX_INDEX_FILE, parse_dates=['time_stamp'])
    vix_1m['time_stamp'] = pd.to_datetime(
        vix_1m['time_stamp'], utc=False).dt.tz_localize(None)

    if BACKTEST_START_DATE:
        nifty_1m = nifty_1m[nifty_1m['time_stamp'] >= pd.Timestamp(BACKTEST_START_DATE)]
        vix_1m   = vix_1m[vix_1m['time_stamp']     >= pd.Timestamp(BACKTEST_START_DATE)]
    if BACKTEST_END_DATE:
        nifty_1m = nifty_1m[nifty_1m['time_stamp'] <= pd.Timestamp(BACKTEST_END_DATE)]
        vix_1m   = vix_1m[vix_1m['time_stamp']     <= pd.Timestamp(BACKTEST_END_DATE)]

    nifty_1m = nifty_1m.set_index('time_stamp').sort_index()
    vix_1m   = vix_1m.set_index('time_stamp').sort_index()

    logger.info(f"  1-min Nifty: {len(nifty_1m):,} rows")
    logger.info(f"  1-min VIX  : {len(vix_1m):,} rows")
    return nifty_1m, vix_1m


def load_contracts(holidays_df: pd.DataFrame = None):
    """Load Nifty weekly expiry contract list. Computes elm_time per expiry."""
    df = pd.read_csv(CONTRACT_LIST_FILE)
    df['expiry_date'] = pd.to_datetime(
        df['expiry_date'], utc=False).dt.tz_localize(None)
    df['end_date'] = pd.to_datetime(
        df['end_date'], utc=False).dt.tz_localize(None)
    elm_times = []
    for _, row in df.iterrows():
        elm = row['end_date'] - pd.Timedelta(seconds=ELM_SECONDS_BEFORE_EXPIRY)
        if holidays_df is not None:
            day_before = (row['end_date'] - pd.Timedelta(days=1)).date()
            if day_before in holidays_df['date'].values:
                elm -= pd.Timedelta(days=1)
                two_before = (row['end_date'] - pd.Timedelta(days=2)).date()
                if two_before in holidays_df['date'].values:
                    elm -= pd.Timedelta(days=1)
        elm_times.append(elm)
    df['elm_time'] = elm_times
    df = df.sort_values('expiry_date').reset_index(drop=True)
    return df


def load_option_data(expiry_date: pd.Timestamp, strike: int,
                     option_type: str) -> pd.DataFrame:
    """Load 1-min option data for a given expiry, strike and type."""
    expiry_str = expiry_date.strftime('%Y-%m-%d')
    filepath   = os.path.join(
        NIFTY_OPTIONS_PATH, expiry_str, f"{strike}{option_type}.csv")
    if not os.path.exists(filepath):
        return pd.DataFrame()
    df = pd.read_csv(filepath, parse_dates=['datetime'])
    df['datetime'] = pd.to_datetime(df['datetime'])
    return df


def get_option_price(option_df: pd.DataFrame, timestamp: pd.Timestamp,
                     price_col: str = 'open') -> float:
    """Get option price at a given timestamp, falling back to last known."""
    if option_df.empty:
        return None
    row = option_df[option_df['datetime'] == timestamp]
    if not row.empty:
        val = row[price_col].iloc[0]
        return float(val) if pd.notna(val) else None
    prior = option_df[option_df['datetime'] < timestamp]
    if not prior.empty:
        return float(prior['close'].iloc[-1])
    return None


def get_1min_value(indexed_df: pd.DataFrame, timestamp: pd.Timestamp,
                   col: str = 'close') -> float:
    """Get a value from a timestamp-indexed 1-min DataFrame."""
    if timestamp in indexed_df.index:
        val = indexed_df.loc[timestamp, col]
        return float(val) if pd.notna(val) else None
    prior = indexed_df[indexed_df.index < timestamp]
    if not prior.empty:
        return float(prior[col].iloc[-1])
    return None


# ---------------------------------------------------------------------------
# Expiry selection  (identical to credit spread)
# ---------------------------------------------------------------------------

def compute_elm_date(expiry_end_date: pd.Timestamp, holidays_set: set):
    """
    Return the last trading day before expiry (the ELM date).
    Steps back from expiry_end_date - ELM_SECONDS_BEFORE_EXPIRY, then
    walks back further if that day is a weekend or holiday.
    """
    elm_dt   = expiry_end_date - pd.Timedelta(seconds=ELM_SECONDS_BEFORE_EXPIRY)
    elm_date = elm_dt.date()
    while elm_date.weekday() >= 5 or elm_date in holidays_set:
        elm_date -= timedelta(days=1)
    return elm_date


def get_expiry(signal_time: pd.Timestamp,
               contracts_df: pd.DataFrame,
               holidays_set: set) -> pd.Timestamp:
    """
    Select the nearest expiry whose ELM date is strictly after signal_date.

    A calendar DTE check is insufficient — an expiry whose ELM date falls on
    signal_date would force a same-day pre-expiry exit, leaving no useful
    trade life. Iterating candidates and selecting the first where
    elm_date > signal_date ensures the trade has at least one full session
    before the pre-expiry exit fires.
    """
    signal_date = signal_time.date()
    future = contracts_df[contracts_df['expiry_date'].dt.date >= signal_date]
    if future.empty:
        return None
    for _, row in future.iterrows():
        elm_date = compute_elm_date(row['end_date'], holidays_set)
        if elm_date > signal_date:
            return row['end_date']
    return None


# ---------------------------------------------------------------------------
# Strike selection  (offset-based, no delta/mibian)
# ---------------------------------------------------------------------------

def select_strikes(spot: float, direction: str) -> tuple:
    """
    Select buy and sell strikes for a debit spread.

    Buy leg: ATM + BUY_LEG_OFFSET (0 = ATM, negative = OTM, positive = ITM)
    Sell leg: further OTM from buy leg by HEDGE_POINTS

    Bullish (buying CE to profit from upward move):
      buy_strike  = ATM + BUY_LEG_OFFSET  (lower strike = more ITM for CE)
      sell_strike = buy_strike + HEDGE_POINTS  (further OTM CE)

    Bearish (buying PE to profit from downward move):
      buy_strike  = ATM - BUY_LEG_OFFSET  (higher strike = more ITM for PE)
      sell_strike = buy_strike - HEDGE_POINTS  (further OTM PE)

    Returns (buy_strike, sell_strike, option_type)
    """
    atm = int(round(spot / STRIKE_STEP) * STRIKE_STEP)

    if direction == 'bullish':
        option_type = 'ce'
        buy_strike  = atm + BUY_LEG_OFFSET
        sell_strike = buy_strike + HEDGE_POINTS
    else:  # bearish
        option_type = 'pe'
        buy_strike  = atm - BUY_LEG_OFFSET
        sell_strike = buy_strike - HEDGE_POINTS

    return buy_strike, sell_strike, option_type


# ---------------------------------------------------------------------------
# P&L calculation
# ---------------------------------------------------------------------------

def _calc_pl(buy_entry: float, buy_exit: float,
             sell_entry: float, sell_exit: float) -> float:
    """
    Calculate P&L in points for a debit spread.
    Profit when bought option appreciates and/or sold option depreciates.
    P&L = (buy_exit - buy_entry) - (sell_exit - sell_entry)
    """
    return round((buy_exit - buy_entry) - (sell_exit - sell_entry), 2)


# ---------------------------------------------------------------------------
# Exit mechanism helpers
# ---------------------------------------------------------------------------

def update_trailing_profit(trailing_profit_floor: float,
                            unrealised_pl: float,
                            max_profit: float,
                            use_trailing: bool = False) -> float:
    """
    Update the trailing profit floor based on current unrealised P&L.
    Floor is a ratchet — only ever moves up, never reverts.
    Returns None if trailing profit has not yet been activated.
    Returns unchanged floor immediately if use_trailing is False.

    All thresholds and floors expressed as fraction of max_profit.
    """
    if not use_trailing:
        return trailing_profit_floor  # always None when trailing is gated off

    new_floor = trailing_profit_floor

    if unrealised_pl >= max_profit * TRAIL_TRIGGER_3:
        candidate = max_profit * TRAIL_FLOOR_3
    elif unrealised_pl >= max_profit * TRAIL_TRIGGER_2:
        candidate = max_profit * TRAIL_FLOOR_2
    elif unrealised_pl >= max_profit * TRAIL_TRIGGER_1:
        candidate = max_profit * TRAIL_FLOOR_1
    else:
        candidate = None

    if candidate is not None:
        if new_floor is None or candidate > new_floor:
            new_floor = candidate

    return new_floor


def get_gate_date(entry_time: pd.Timestamp, gate_days: int,
                  holidays_set: set):
    """
    Returns the gate activation date: the first trading day on or after
    entry_date + gate_days calendar days.
    Skips weekends (weekday >= 5) and market holidays.

    holidays_set: a set of datetime.date objects.
    """
    gate_date = entry_time.date() + timedelta(days=gate_days)
    while gate_date.weekday() >= 5 or gate_date in holidays_set:
        gate_date += timedelta(days=1)
    return gate_date


def check_exits(unrealised_pl: float, max_profit: float,
                trailing_profit_floor: float,
                ts: pd.Timestamp,
                gate_date,
                max_unrealised_pl_so_far: float,
                use_trailing: bool = False,
                days_in_trade: int = 0,
                net_debit: float = 0.0,
                active_pt_pct: float = None,
                active_gate_pct: float = None,
                active_hard_stop: float = None) -> str:
    """
    Check all toggleable exit conditions for a debit spread.
    Returns the triggered exit type string, or None.
    trend_flip and expiry are handled by the caller — not checked here.

    active_pt_pct, active_gate_pct, active_hard_stop are pre-resolved at
    entry time via resolve_param() — direction-specific overrides already
    applied. Falls back to base config values if None.

    Exit evaluation order (first to trigger wins):
    1. hard_stop        — unrealised_pl <= -active_hard_stop
    2. profit_target    — unrealised_pl >= max_profit * active_pt_pct
    3. day0_spread_sl   — Day 0 only: unrealised_pl < -net_debit * DAY0_SPREAD_SL_PCT
    4. time_gate        — past gate_date, past TIME_GATE_CHECK_TIME,
                          max_unrealised never reached active_gate_pct * max_profit
    5. trailing_profit  — unrealised_pl < trailing_profit_floor (VIX-gated)
    """
    # Use pre-resolved values; fall back to base config if not provided
    hard_stop_pts    = active_hard_stop if active_hard_stop is not None else HARD_STOP_POINTS
    pt_pct           = active_pt_pct    if active_pt_pct    is not None else PROFIT_TARGET_PCT_LOW_VIX
    gate_min_pct     = active_gate_pct  if active_gate_pct  is not None else TIME_GATE_MIN_PROFIT_PCT_HIGH_VIX

    # 1. Hard stop — unconditional floor, fires before all other exits
    if ENABLE_HARD_STOP:
        if unrealised_pl <= -hard_stop_pts:
            return 'hard_stop'

    # 2. Profit target
    if ENABLE_PROFIT_TARGET:
        if unrealised_pl >= max_profit * pt_pct:
            return 'profit_target'

    # 3. Day 0 spread SL — only active on entry day
    if ENABLE_DAY0_SPREAD_SL and days_in_trade == 0:
        if unrealised_pl < -net_debit * DAY0_SPREAD_SL_PCT:
            return 'day0_spread_sl'

    # 4. Time gate — three conditions must all be true simultaneously:
    #    a. current date is on or after gate_date (pre-computed at entry)
    #    b. current time is at or after TIME_GATE_CHECK_TIME (avoids 09:15 noise)
    #    c. trade has never reached active_gate_pct * max_profit
    if ENABLE_TIME_GATE and gate_date is not None:
        if (ts.date() >= gate_date and
                ts.time() >= pd.Timestamp(TIME_GATE_CHECK_TIME).time() and
                max_unrealised_pl_so_far < gate_min_pct * max_profit):
            return 'time_gate'

    # 5. Trailing profit lock — only active when use_trailing is True
    if use_trailing and trailing_profit_floor is not None:
        if unrealised_pl < trailing_profit_floor:
            return 'trailing_profit'

    return None


def resolve_param(direction: str, bull_val, bear_val, base_val):
    """
    Return the direction-specific parameter value if set, else the base value.
    Called once at entry time — result stored in trade state, not re-resolved
    on every candle.

    direction: 'bullish' or 'bearish'
    bull_val:  PARAM_BULL config value (None if not overriding)
    bear_val:  PARAM_BEAR config value (None if not overriding)
    base_val:  base PARAM config value (always set)
    """
    if direction == 'bullish' and bull_val is not None:
        return bull_val
    if direction == 'bearish' and bear_val is not None:
        return bear_val
    return base_val


# ---------------------------------------------------------------------------
# Slippage
# ---------------------------------------------------------------------------

def apply_slippage(price: float, is_buy: bool) -> float:
    """Add slippage to buys, subtract from sells. Floor at 0."""
    return (price + SLIPPAGE_POINTS) if is_buy else max(price - SLIPPAGE_POINTS, 0.0)


# ---------------------------------------------------------------------------
# Per-trade 1-min snapshot
# ---------------------------------------------------------------------------

def _build_snapshot(ts: pd.Timestamp, spot: float, vix: float,
                    buy_strike: int, sell_strike: int, option_type: str,
                    buy_ltp: float, sell_ltp: float,
                    buy_entry: float, sell_entry: float,
                    direction: str, trend_75, trend_15,
                    expiry: pd.Timestamp,
                    net_debit: float, max_profit: float,
                    days_in_trade: int = 0,
                    trailing_profit_floor: float = None,
                    max_unrealised_pl_so_far: float = 0.0,
                    entry_vix: float = None,
                    realised_pl_pts: float = None,
                    realised_pl_rs:  float = None) -> dict:
    """
    Build a single 1-min snapshot row for the per-trade log.

    unrealised_pl_pts: mark-to-market P&L without slippage.
    realised_pl_pts / realised_pl_rs: only populated on the final (exit) row.
    profit_target_level: absolute P&L level at which profit target fires (VIX-sensitive).
    trailing_profit_level: current ratchet floor in P&L points (None until active).
    """
    unrealised_pl = _calc_pl(buy_entry, buy_ltp, sell_entry, sell_ltp)

    # VIX-sensitive profit target level for log reference
    if entry_vix is not None and entry_vix >= PROFIT_TARGET_VIX_HIGH:
        _pt_pct = PROFIT_TARGET_PCT_HIGH_VIX
    elif entry_vix is not None and entry_vix >= PROFIT_TARGET_VIX_LOW:
        _pt_pct = PROFIT_TARGET_PCT_MID_VIX
    else:
        _pt_pct = PROFIT_TARGET_PCT_LOW_VIX

    profit_target_level = round(max_profit * _pt_pct, 2)
    trail_val = round(trailing_profit_floor, 2) if trailing_profit_floor is not None else None
    dte = (expiry.date() - ts.date()).days

    return {
        'time_stamp':            ts,
        'spot':                  round(spot, 2),
        'vix':                   round(vix, 2) if vix is not None else None,
        'buy_strike':            buy_strike,
        'sell_strike':           sell_strike,
        'option_type':           option_type,
        'buy_ltp':               round(buy_ltp,  2),
        'sell_ltp':              round(sell_ltp, 2),
        'buy_entry':             round(buy_entry,  2),
        'sell_entry':            round(sell_entry, 2),
        'net_debit':             round(net_debit,  2),
        'max_profit':            round(max_profit,  2),
        'unrealised_pl_pts':     round(unrealised_pl, 2),
        'unrealised_pl_rs':      round(unrealised_pl * LOT_SIZE, 2),
        'realised_pl_pts':       round(realised_pl_pts, 2) if realised_pl_pts is not None else None,
        'realised_pl_rs':        round(realised_pl_rs,  2) if realised_pl_rs  is not None else None,
        'profit_target_level':   profit_target_level,
        'trailing_profit_level': trail_val,
        'max_unrealised_so_far': round(max_unrealised_pl_so_far, 2),
        'trend_75':              trend_75,
        'trend_15':              trend_15,
        'dte':                   dte,
    }


# ---------------------------------------------------------------------------
# Trade stats (for trade summary)
# ---------------------------------------------------------------------------

def _compute_trade_stats(trade_log: list, direction: str) -> dict:
    """
    Scan the per-trade 1-min log and compute extremes across the full trade
    lifetime (including the exit candle).

    Returns a dict with 14 fields:
      max/min unrealised_pl_pts and their timestamps
      max/min buy_ltp and their timestamps
      max/min sell_ltp and their timestamps
      best_spot / best_spot_ts — most favourable spot reached:
        bullish (bought CE): max spot — spot rising is favourable
        bearish (bought PE): min spot — spot falling is favourable
    """
    empty = {
        'max_unrealised_pl_pts': None, 'max_unrealised_pl_ts': None,
        'min_unrealised_pl_pts': None, 'min_unrealised_pl_ts': None,
        'max_buy_ltp':           None, 'max_buy_ltp_ts':       None,
        'min_buy_ltp':           None, 'min_buy_ltp_ts':       None,
        'max_sell_ltp':          None, 'max_sell_ltp_ts':      None,
        'min_sell_ltp':          None, 'min_sell_ltp_ts':      None,
        'best_spot':             None, 'best_spot_ts':         None,
    }
    if not trade_log:
        return empty

    first = trade_log[0]
    max_pl = min_pl = first['unrealised_pl_pts']
    max_pl_ts = min_pl_ts = first['time_stamp']

    max_buy = min_buy = first['buy_ltp']
    max_buy_ts = min_buy_ts = first['time_stamp']

    max_sell = min_sell = first['sell_ltp']
    max_sell_ts = min_sell_ts = first['time_stamp']

    best_spot    = first['spot']
    best_spot_ts = first['time_stamp']

    for row in trade_log[1:]:
        ts  = row['time_stamp']
        pl  = row['unrealised_pl_pts']
        bl  = row['buy_ltp']
        sl  = row['sell_ltp']
        sp  = row['spot']

        if pl > max_pl:   max_pl,   max_pl_ts   = pl, ts
        if pl < min_pl:   min_pl,   min_pl_ts   = pl, ts
        if bl > max_buy:  max_buy,  max_buy_ts  = bl, ts
        if bl < min_buy:  min_buy,  min_buy_ts  = bl, ts
        if sl > max_sell: max_sell, max_sell_ts = sl, ts
        if sl < min_sell: min_sell, min_sell_ts = sl, ts

        if direction == 'bullish' and sp > best_spot:
            best_spot, best_spot_ts = sp, ts
        elif direction == 'bearish' and sp < best_spot:
            best_spot, best_spot_ts = sp, ts

    return {
        'max_unrealised_pl_pts': round(max_pl,   2),
        'max_unrealised_pl_ts':  max_pl_ts,
        'min_unrealised_pl_pts': round(min_pl,   2),
        'min_unrealised_pl_ts':  min_pl_ts,
        'max_buy_ltp':           round(max_buy,  2),
        'max_buy_ltp_ts':        max_buy_ts,
        'min_buy_ltp':           round(min_buy,  2),
        'min_buy_ltp_ts':        min_buy_ts,
        'max_sell_ltp':          round(max_sell, 2),
        'max_sell_ltp_ts':       max_sell_ts,
        'min_sell_ltp':          round(min_sell, 2),
        'min_sell_ltp_ts':       min_sell_ts,
        'best_spot':             round(best_spot, 2),
        'best_spot_ts':          best_spot_ts,
    }


def _build_trade_record(entry_time, exit_time, direction, expiry,
                         buy_strike, sell_strike, option_type,
                         entry_spot, exit_spot,
                         buy_entry, sell_entry,
                         buy_exit, sell_exit,
                         net_debit, max_profit,
                         pl_points, pl_rupees, exit_reason,
                         trade_stats: dict = None,
                         entry_vix=None, exit_vix=None,
                         trail_active: bool = False,
                         active_pt_pct: float = None,
                         active_gate_pct: float = None,
                         active_gate_days=None,
                         active_hard_stop: float = None) -> dict:
    record = {
        'entry_time':      entry_time,
        'exit_time':       exit_time,
        'direction':       direction,
        'expiry':          expiry,
        'buy_strike':      buy_strike,
        'sell_strike':     sell_strike,
        'option_type':     option_type,
        'spread_type':     SPREAD_TYPE,
        'entry_spot':      entry_spot,
        'exit_spot':       exit_spot,
        'buy_entry':       buy_entry,
        'sell_entry':      sell_entry,
        'buy_exit':        buy_exit,
        'sell_exit':       sell_exit,
        'net_debit':       round(net_debit,  2),
        'max_profit':      round(max_profit, 2),
        'pl_points':       pl_points,
        'pl_rupees':       pl_rupees,
        'exit_reason':     exit_reason,
        'trail_active':    trail_active,
        'active_pt_pct':   round(active_pt_pct,    4) if active_pt_pct    is not None else None,
        'active_gate_pct': round(active_gate_pct,  4) if active_gate_pct  is not None else None,
        'active_gate_days': active_gate_days,
        'active_hard_stop': round(active_hard_stop, 2) if active_hard_stop is not None else None,
        'entry_vix':       round(entry_vix, 2) if entry_vix is not None else None,
        'exit_vix':        round(exit_vix,  2) if exit_vix  is not None else None,
    }
    null_stats = {
        'max_unrealised_pl_pts': None, 'max_unrealised_pl_ts': None,
        'min_unrealised_pl_pts': None, 'min_unrealised_pl_ts': None,
        'max_buy_ltp':           None, 'max_buy_ltp_ts':       None,
        'min_buy_ltp':           None, 'min_buy_ltp_ts':       None,
        'max_sell_ltp':          None, 'max_sell_ltp_ts':      None,
        'min_sell_ltp':          None, 'min_sell_ltp_ts':      None,
        'best_spot':             None, 'best_spot_ts':         None,
    }
    record.update(trade_stats if trade_stats else null_stats)
    return record


def _log_exit(trade: dict):
    logger.info(
        f"  EXIT  {trade['exit_reason']:20s} | {trade['direction']:8s} | "
        f"Buy {trade['buy_strike']} | "
        f"P&L: {trade['pl_points']:+.1f} pts ({trade['pl_rupees']:+,.0f})"
    )


def _save_trade_log(trade_counter: int, entry_time: pd.Timestamp,
                    trade_log: list):
    """Save per-trade 1-min log."""
    if not trade_log:
        return
    entry_str = pd.Timestamp(entry_time).strftime('%Y-%m-%d_%H%M')
    filename  = f"trade_{trade_counter:04d}_{entry_str}.csv"
    filepath  = os.path.join(TRADE_LOGS_DIR, filename)
    pd.DataFrame(trade_log).to_csv(filepath, index=False)
    logger.debug(f"  Trade log saved: {filename} ({len(trade_log)} rows)")


# ---------------------------------------------------------------------------
# 1-min snapshot helpers
# ---------------------------------------------------------------------------

def _append_1min_snapshots_window(from_ts: pd.Timestamp, to_ts: pd.Timestamp,
                                   nifty_1m, vix_1m, nifty_75_indexed,
                                   nifty_15, buy_opt_df, sell_opt_df,
                                   buy_strike, sell_strike, option_type,
                                   buy_entry, sell_entry, direction, expiry,
                                   net_debit, max_profit,
                                   trade_log: list,
                                   last_buy_ltp:  float = None,
                                   last_sell_ltp: float = None,
                                   trailing_profit_floor: float = None,
                                   max_unrealised_pl_so_far: float = 0.0,
                                   trade_entry_date=None,
                                   gate_date=None,
                                   use_trailing: bool = False,
                                   entry_vix: float = None,
                                   active_pt_pct: float = None,
                                   active_gate_pct: float = None,
                                   active_hard_stop: float = None):
    """
    Append 1-min snapshots for every minute in (from_ts, to_ts] to trade_log.
    Checks all exit conditions on every 1-min candle.
    Updates trailing_profit_floor ratchet and max_unrealised_pl_so_far.

    active_pt_pct, active_gate_pct, active_hard_stop are pre-resolved at
    entry time and passed through to check_exits unchanged.

    Returns:
      (last_buy_ltp, last_sell_ltp, exit_ts, exit_type,
       trailing_profit_floor, max_unrealised_pl_so_far)
    """
    running_buy_ltp   = last_buy_ltp  if last_buy_ltp  is not None else buy_entry
    running_sell_ltp  = last_sell_ltp if last_sell_ltp is not None else sell_entry
    running_trail     = trailing_profit_floor
    running_max_pl    = max_unrealised_pl_so_far
    exit_hit_ts       = None
    exit_hit_type     = None

    window = nifty_1m[
        (nifty_1m.index > from_ts) & (nifty_1m.index <= to_ts)
    ]
    for ts, row in window.iterrows():
        spot = float(row['close'])
        vix  = get_1min_value(vix_1m, ts, 'close')

        fetched_buy  = get_option_price(buy_opt_df,  ts, 'close')
        fetched_sell = get_option_price(sell_opt_df, ts, 'close')
        if fetched_buy  is not None: running_buy_ltp  = fetched_buy
        if fetched_sell is not None: running_sell_ltp = fetched_sell

        prior_75 = nifty_75_indexed[nifty_75_indexed.index <= ts]
        trend_75 = prior_75.iloc[-1]['trend'] if not prior_75.empty else None

        prior_15 = nifty_15[nifty_15['time_stamp'] <= ts]
        trend_15 = prior_15.iloc[-1]['trend'] if not prior_15.empty else None

        unrealised    = _calc_pl(buy_entry, running_buy_ltp, sell_entry, running_sell_ltp)
        days_in_trade = (ts.date() - trade_entry_date).days if trade_entry_date else 0

        running_trail  = update_trailing_profit(running_trail, unrealised, max_profit, use_trailing)
        if unrealised > running_max_pl:
            running_max_pl = unrealised

        snapshot = _build_snapshot(
            ts, spot, vix,
            buy_strike, sell_strike, option_type,
            running_buy_ltp, running_sell_ltp,
            buy_entry, sell_entry,
            direction, trend_75, trend_15, expiry,
            net_debit=net_debit, max_profit=max_profit,
            days_in_trade=days_in_trade,
            trailing_profit_floor=running_trail,
            max_unrealised_pl_so_far=running_max_pl,
            entry_vix=entry_vix,
        )
        trade_log.append(snapshot)

        if exit_hit_ts is None:
            ex = check_exits(
                unrealised, max_profit, running_trail,
                ts, gate_date, running_max_pl, use_trailing,
                days_in_trade=days_in_trade, net_debit=net_debit,
                active_pt_pct=active_pt_pct,
                active_gate_pct=active_gate_pct,
                active_hard_stop=active_hard_stop)
            if ex:
                exit_hit_ts   = ts
                exit_hit_type = ex
                break

    return (running_buy_ltp, running_sell_ltp,
            exit_hit_ts, exit_hit_type,
            running_trail, running_max_pl)


def _append_1min_snapshots(day_date, nifty_1m, vix_1m,
                            nifty_75_indexed, nifty_15,
                            buy_opt_df, sell_opt_df,
                            buy_strike, sell_strike, option_type,
                            buy_entry, sell_entry, direction, expiry,
                            net_debit, max_profit,
                            trade_log: list,
                            last_buy_ltp: float = None,
                            last_sell_ltp: float = None,
                            trailing_profit_floor: float = None,
                            max_unrealised_pl_so_far: float = 0.0,
                            trade_entry_date=None,
                            gate_date=None,
                            use_trailing: bool = False,
                            entry_vix: float = None,
                            active_pt_pct: float = None,
                            active_gate_pct: float = None,
                            active_hard_stop: float = None):
    """Append all 1-min snapshots for a full day (overnight-held trades)."""
    day_start = pd.Timestamp(f"{day_date} 09:15:00")
    day_end   = pd.Timestamp(f"{day_date} 15:30:00")
    return _append_1min_snapshots_window(
        day_start - pd.Timedelta(minutes=1), day_end,
        nifty_1m, vix_1m, nifty_75_indexed, nifty_15,
        buy_opt_df, sell_opt_df,
        buy_strike, sell_strike, option_type,
        buy_entry, sell_entry, direction, expiry,
        net_debit, max_profit,
        trade_log,
        last_buy_ltp, last_sell_ltp,
        trailing_profit_floor, max_unrealised_pl_so_far,
        trade_entry_date, gate_date, use_trailing, entry_vix,
        active_pt_pct, active_gate_pct, active_hard_stop,
    )


# ---------------------------------------------------------------------------
# Entry filter
# ---------------------------------------------------------------------------

def entry_allowed(signal_ts: pd.Timestamp,
                  entry_direction: str = None,
                  entry_vix: float = None) -> bool:
    """
    Returns True if a new entry is permitted at this signal timestamp.

    signal_ts is the 15-min candle whose CLOSE triggered the Supertrend flip.
    Entry executes at the OPEN of the next candle. Filters are evaluated on
    the signal candle time — not the entry execution time.

    A Monday signal that enters at Tuesday open is a Monday signal and will
    NOT be blocked by a Tuesday day-of-week exclusion.

    entry_direction and entry_vix are used for the EXCLUDE_BULLISH_VIX_ABOVE
    filter. Does not affect in-trade management — only the entry decision.
    """
    # Filter 1: excluded day of week
    if EXCLUDE_TRADE_DAYS and signal_ts.dayofweek in EXCLUDE_TRADE_DAYS:
        logger.debug(
            f"  Entry blocked — excluded day "
            f"({signal_ts.strftime('%A')}): {signal_ts}")
        return False

    # Filter 2: excluded signal candle close times
    if EXCLUDE_SIGNAL_CANDLES:
        signal_hhmm = signal_ts.strftime('%H:%M')
        if signal_hhmm in EXCLUDE_SIGNAL_CANDLES:
            logger.debug(
                f"  Entry blocked — excluded signal candle "
                f"({signal_hhmm}): {signal_ts}")
            return False

    # Filter 3: bullish VIX exclusion
    if (EXCLUDE_BULLISH_VIX_ABOVE is not None
            and entry_direction == 'bullish'
            and entry_vix is not None
            and entry_vix > EXCLUDE_BULLISH_VIX_ABOVE):
        logger.debug(
            f"  Entry blocked — bullish signal above VIX threshold "
            f"(VIX {entry_vix:.1f} > {EXCLUDE_BULLISH_VIX_ABOVE}): {signal_ts}")
        return False

    return True


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------

def run_backtest(nifty_15: pd.DataFrame, nifty_75: pd.DataFrame,
                 vix_daily: pd.DataFrame, contracts_df: pd.DataFrame,
                 nifty_1m: pd.DataFrame, vix_1m: pd.DataFrame,
                 holidays_df: pd.DataFrame = None):
    """
    Main backtest loop — debit spread variant.
    Signal logic identical to credit spread. Strike selection, P&L, and
    exit mechanisms differ per the debit spread design specification.
    """
    os.makedirs(TRADE_LOGS_DIR, exist_ok=True)

    nifty_75_indexed = nifty_75.set_index('time_stamp').sort_index()

    high_vix_dates = set(vix_daily[vix_daily['vix_open'] > VIX_THRESHOLD]['date'])
    logger.info(f"High-VIX days (VIX > {VIX_THRESHOLD}): {len(high_vix_dates)}")

    # Pre-build holidays set for gate_date computation at entry
    holidays_set = set(holidays_df['date'].values) if holidays_df is not None else set()

    all_trades      = []
    option_df_cache = {}
    trade_counter   = 0

    nifty_15 = nifty_15.copy()
    nifty_15['date'] = nifty_15['time_stamp'].dt.date

    trading_days = sorted(nifty_15['date'].unique())
    if BACKTEST_START_DATE:
        trading_days = [d for d in trading_days
                        if d >= pd.Timestamp(BACKTEST_START_DATE).date()]
    if BACKTEST_END_DATE:
        trading_days = [d for d in trading_days
                        if d <= pd.Timestamp(BACKTEST_END_DATE).date()]

    logger.info(f"Trading days in scope : {len(trading_days)}")
    logger.info(f"Starting backtest...")

    sl_1min_ts   = None
    sl_1min_type = None

    # ------------------------------------------------------------------
    # Trade state — persists across days
    # ------------------------------------------------------------------
    in_trade      = False
    direction     = None
    buy_strike    = None
    sell_strike   = None
    option_type   = None
    expiry        = None
    elm_time      = None
    buy_entry     = None
    sell_entry    = None
    net_debit     = None
    max_profit    = None
    buy_ltp       = None
    sell_ltp      = None
    entry_time    = None
    entry_spot    = None
    entry_vix     = None
    buy_opt_df    = None
    sell_opt_df   = None
    trade_log     = []
    snap_buy_ltp  = None
    snap_sell_ltp = None
    entry_exec_ts = None
    # Additional lots state
    has_additional    = False
    add_buy_entry     = None
    add_sell_entry    = None
    add_buy_ltp       = None
    add_sell_ltp      = None
    add_booked_pl     = 0.0
    # Exit mechanism state
    trailing_profit_floor    = None   # ratchet — only moves up, never reverts
    max_unrealised_pl_so_far = 0.0    # tracks peak P&L for time_gate condition
    gate_date                = None   # pre-computed at entry, never changes during trade
    use_trailing             = False  # pre-computed at entry from entry_vix >= TRAIL_VIX_THRESHOLD
    trade_entry_date         = None
    # Direction-specific resolved parameters — computed once at entry via resolve_param()
    active_pt_pct            = None   # resolved profit target PCT for this trade
    active_gate_pct          = None   # resolved time gate min profit PCT for this trade
    active_gate_days         = None   # resolved time gate days for this trade
    active_hard_stop         = None   # resolved hard stop points for this trade

    for day_date in trading_days:

        # ------------------------------------------------------------------
        # Close any trade where expiry has passed without an explicit exit
        # ------------------------------------------------------------------
        if in_trade and expiry is not None and day_date > expiry.date():
            _buy_exp  = get_option_price(buy_opt_df,  expiry, 'close') or buy_ltp
            _sell_exp = get_option_price(sell_opt_df, expiry, 'close') or sell_ltp
            # No slippage on expiry exits
            pl_points = _calc_pl(buy_entry, _buy_exp, sell_entry, _sell_exp)
            pl_points = round(pl_points + add_booked_pl * ADDITIONAL_LOT_MULTIPLIER, 2)
            pl_rupees = pl_points * LOT_SIZE
            expiry_exit_vix  = get_1min_value(vix_1m,   expiry, 'close')
            expiry_exit_spot = get_1min_value(nifty_1m, expiry, 'close') or entry_spot
            trade_stats  = _compute_trade_stats(trade_log, direction)
            trade_record = _build_trade_record(
                entry_time, expiry, direction, expiry,
                buy_strike, sell_strike, option_type,
                entry_spot, expiry_exit_spot,
                buy_entry, sell_entry,
                _buy_exp, _sell_exp,
                net_debit, max_profit,
                pl_points, pl_rupees, 'expiry',
                trade_stats=trade_stats,
                entry_vix=entry_vix, exit_vix=expiry_exit_vix,
                trail_active=use_trailing,
                active_pt_pct=active_pt_pct,
                active_gate_pct=active_gate_pct,
                active_gate_days=active_gate_days,
                active_hard_stop=active_hard_stop,
            )
            trade_counter += 1
            all_trades.append(trade_record)
            _log_exit(trade_record)
            _save_trade_log(trade_counter, entry_time, trade_log)
            in_trade                  = False
            trade_log                 = []
            has_additional            = False
            add_booked_pl             = 0.0

        # ------------------------------------------------------------------
        # Overnight path: build 1-min log for active trade on non-high-VIX days
        # ------------------------------------------------------------------
        if in_trade and day_date not in high_vix_dates:
            (snap_buy_ltp, snap_sell_ltp,
             sl_1min_ts, sl_1min_type,
             trailing_profit_floor, max_unrealised_pl_so_far) = \
                _append_1min_snapshots(
                    day_date, nifty_1m, vix_1m,
                    nifty_75_indexed, nifty_15,
                    buy_opt_df, sell_opt_df,
                    buy_strike, sell_strike, option_type,
                    buy_entry, sell_entry, direction, expiry,
                    net_debit, max_profit,
                    trade_log,
                    snap_buy_ltp, snap_sell_ltp,
                    trailing_profit_floor, max_unrealised_pl_so_far,
                    trade_entry_date, gate_date, use_trailing, entry_vix,
                    active_pt_pct, active_gate_pct, active_hard_stop,
                )

            if sl_1min_ts is not None and in_trade:
                _exit_exec_ts  = sl_1min_ts + pd.Timedelta(minutes=1)
                _buy_raw  = get_option_price(buy_opt_df,  _exit_exec_ts, 'open') or snap_buy_ltp
                _sell_raw = get_option_price(sell_opt_df, _exit_exec_ts, 'open') or snap_sell_ltp
                _buy_net  = apply_slippage(_buy_raw,  is_buy=False)
                _sell_net = apply_slippage(_sell_raw, is_buy=True)
                _base_pl  = _calc_pl(buy_entry, _buy_net, sell_entry, _sell_net)
                if has_additional:
                    _add_buy_raw  = get_option_price(buy_opt_df,  _exit_exec_ts, 'open') or snap_buy_ltp
                    _add_sell_raw = get_option_price(sell_opt_df, _exit_exec_ts, 'open') or snap_sell_ltp
                    add_booked_pl = _calc_pl(
                        add_buy_entry,  apply_slippage(_add_buy_raw,  is_buy=False),
                        add_sell_entry, apply_slippage(_add_sell_raw, is_buy=True))
                    has_additional = False
                _pl_pts = round(_base_pl + add_booked_pl * ADDITIONAL_LOT_MULTIPLIER, 2)
                _pl_rs  = _pl_pts * LOT_SIZE
                _exit_spot = get_1min_value(nifty_1m, _exit_exec_ts, 'close') or entry_spot
                _exit_vix  = get_1min_value(vix_1m,   _exit_exec_ts, 'close')
                _prior_75  = nifty_75_indexed[nifty_75_indexed.index <= _exit_exec_ts]
                _t75 = _prior_75.iloc[-1]['trend'] if not _prior_75.empty else None
                _prior_15  = nifty_15[nifty_15['time_stamp'] <= _exit_exec_ts]
                _t15 = _prior_15.iloc[-1]['trend'] if not _prior_15.empty else None
                _exit_days = (_exit_exec_ts.date() - trade_entry_date).days if trade_entry_date else 0
                trade_log.append(_build_snapshot(
                    _exit_exec_ts, _exit_spot, _exit_vix,
                    buy_strike, sell_strike, option_type,
                    _buy_raw, _sell_raw, buy_entry, sell_entry,
                    direction, _t75, _t15, expiry,
                    net_debit=net_debit, max_profit=max_profit,
                    days_in_trade=_exit_days,
                    trailing_profit_floor=trailing_profit_floor,
                    max_unrealised_pl_so_far=max_unrealised_pl_so_far,
                    entry_vix=entry_vix,
                    realised_pl_pts=_pl_pts, realised_pl_rs=_pl_rs))
                trade_stats  = _compute_trade_stats(trade_log, direction)
                trade_record = _build_trade_record(
                    entry_time, _exit_exec_ts, direction, expiry,
                    buy_strike, sell_strike, option_type,
                    entry_spot, _exit_spot,
                    buy_entry, sell_entry,
                    _buy_net, _sell_net,
                    net_debit, max_profit,
                    _pl_pts, _pl_rs, sl_1min_type,
                    trade_stats=trade_stats,
                    entry_vix=entry_vix, exit_vix=_exit_vix,
                    trail_active=use_trailing,
                    active_pt_pct=active_pt_pct,
                    active_gate_pct=active_gate_pct,
                    active_gate_days=active_gate_days,
                    active_hard_stop=active_hard_stop,
                )
                trade_counter += 1
                all_trades.append(trade_record)
                _log_exit(trade_record)
                _save_trade_log(trade_counter, entry_time, trade_log)
                # Reset all trade state
                in_trade = False; trade_log = []
                direction = None; buy_strike = None; sell_strike = None
                option_type = None; expiry = None; elm_time = None
                buy_entry = None; sell_entry = None; net_debit = None; max_profit = None
                buy_opt_df = None; sell_opt_df = None
                snap_buy_ltp = None; snap_sell_ltp = None
                entry_exec_ts = None; entry_vix = None
                has_additional = False; add_buy_entry = None; add_sell_entry = None
                add_buy_ltp = None; add_sell_ltp = None; add_booked_pl = 0.0
                trailing_profit_floor = None; max_unrealised_pl_so_far = 0.0
                trade_entry_date = None; gate_date = None; use_trailing = False
                active_pt_pct = None; active_gate_pct = None; active_gate_days = None; active_hard_stop = None
                sl_1min_ts = None; sl_1min_type = None
            continue

        if day_date not in high_vix_dates:
            continue

        day_15 = nifty_15[nifty_15['date'] == day_date].reset_index(drop=True)
        if day_15.empty:
            continue

        day_open_anchor = pd.Timestamp(f"{day_date} 09:14:00")
        last_15min_ts   = None

        for idx in range(len(day_15)):
            row      = day_15.iloc[idx]
            ts       = row['time_stamp']
            spot     = row['close']
            trend_15 = row['trend']
            flip_15  = row['trend_flip']

            prior_75 = nifty_75_indexed[nifty_75_indexed.index <= ts]
            if prior_75.empty:
                continue
            trend_75 = prior_75.iloc[-1]['trend']

            if pd.isna(trend_15) or pd.isna(trend_75):
                last_15min_ts = ts
                continue

            has_next = (idx + 1) < len(day_15)
            next_row = day_15.iloc[idx + 1] if has_next else row
            exec_ts  = next_row['time_stamp'] if has_next else ts
            exec_col = 'open' if has_next else 'close'

            # --------------------------------------------------------------
            # Append 1-min snapshots for active trade up to this 15-min bar
            # --------------------------------------------------------------
            if in_trade:
                prev_ts = last_15min_ts if last_15min_ts is not None else day_open_anchor
                if entry_exec_ts is not None and prev_ts < entry_exec_ts - pd.Timedelta(minutes=1):
                    prev_ts = entry_exec_ts - pd.Timedelta(minutes=1)
                entry_exec_ts = None

                (snap_buy_ltp, snap_sell_ltp,
                 sl_1min_ts, sl_1min_type,
                 trailing_profit_floor, max_unrealised_pl_so_far) = \
                    _append_1min_snapshots_window(
                        prev_ts, ts, nifty_1m, vix_1m,
                        nifty_75_indexed, nifty_15,
                        buy_opt_df, sell_opt_df,
                        buy_strike, sell_strike, option_type,
                        buy_entry, sell_entry, direction, expiry,
                        net_debit, max_profit,
                        trade_log,
                        snap_buy_ltp, snap_sell_ltp,
                        trailing_profit_floor, max_unrealised_pl_so_far,
                        trade_entry_date, gate_date, use_trailing, entry_vix,
                    active_pt_pct, active_gate_pct, active_hard_stop,
                    )

            last_15min_ts = ts

            # --------------------------------------------------------------
            # Monitor open trade for exits
            # --------------------------------------------------------------
            if in_trade:
                buy_ltp  = get_option_price(buy_opt_df,  ts, 'close') or buy_ltp
                sell_ltp = get_option_price(sell_opt_df, ts, 'close') or sell_ltp
                if has_additional:
                    add_buy_ltp  = get_option_price(buy_opt_df,  ts, 'close') or add_buy_ltp
                    add_sell_ltp = get_option_price(sell_opt_df, ts, 'close') or add_sell_ltp

                # Pre-expiry full position exit at elm_time (15:15 day before expiry)
                # Fires after all other exit checks — prevents carrying into expiry day
                # and avoids ELM margin requirement. Executes at open of next 1-min candle.
                # Only check this if no other exit has already fired.
                exit_reason = None

                # Consume any 1-min exit hit from the preceding snapshot window
                if sl_1min_ts is not None:
                    exit_reason  = sl_1min_type
                    buy_ltp      = snap_buy_ltp
                    sell_ltp     = snap_sell_ltp
                    sl_exec_ts   = sl_1min_ts + pd.Timedelta(minutes=1)
                    exec_ts      = sl_exec_ts
                    exec_col     = 'open'
                    sl_1min_ts   = None

                # Trend flip check
                if exit_reason is None and flip_15:
                    if (direction == 'bullish' and trend_15 == False) or \
                       (direction == 'bearish' and trend_15 == True):
                        exit_reason = 'trend_flip_15'

                # 15-min fallback exit check (with 09:15 guard)
                if exit_reason is None:
                    unrealised_15  = _calc_pl(buy_entry, buy_ltp, sell_entry, sell_ltp)
                    days_in_trade_15 = (ts.date() - trade_entry_date).days if trade_entry_date else 0
                    ex_15 = check_exits(
                        unrealised_15, max_profit, trailing_profit_floor,
                        ts, gate_date, max_unrealised_pl_so_far, use_trailing,
                        days_in_trade=days_in_trade_15, net_debit=net_debit,
                        active_pt_pct=active_pt_pct,
                        active_gate_pct=active_gate_pct,
                        active_hard_stop=active_hard_stop)
                    if ex_15:
                        if ts.time() < pd.Timestamp(NO_EXIT_BEFORE).time():
                            recheck_ts       = pd.Timestamp(f"{ts.date()} {NO_EXIT_BEFORE}:00")
                            recheck_spot     = get_1min_value(nifty_1m, recheck_ts, 'close')
                            recheck_buy_ltp  = get_option_price(buy_opt_df,  recheck_ts, 'close') or buy_ltp
                            recheck_sell_ltp = get_option_price(sell_opt_df, recheck_ts, 'close') or sell_ltp
                            if recheck_spot is not None:
                                recheck_unreal = _calc_pl(
                                    buy_entry, recheck_buy_ltp,
                                    sell_entry, recheck_sell_ltp)
                                ex_recheck = check_exits(
                                    recheck_unreal, max_profit, trailing_profit_floor,
                                    recheck_ts, gate_date, max_unrealised_pl_so_far, use_trailing,
                                    days_in_trade=days_in_trade_15, net_debit=net_debit,
                                    active_pt_pct=active_pt_pct,
                                    active_gate_pct=active_gate_pct,
                                    active_hard_stop=active_hard_stop)
                                if ex_recheck:
                                    exit_reason  = ex_recheck
                                    exec_ts      = recheck_ts
                                    exec_col     = 'open'
                                    buy_ltp      = recheck_buy_ltp
                                    sell_ltp     = recheck_sell_ltp
                        else:
                            exit_reason = ex_15

                # Pre-expiry exit — full position at 15:15 day before expiry
                # Fires after all other checks; exec at open of next candle (15:16)
                if exit_reason is None and elm_time is not None and ts >= elm_time:
                    exit_reason = 'pre_expiry_exit'
                    exec_ts     = ts + pd.Timedelta(minutes=1)
                    exec_col    = 'open'

                # Expiry check
                if expiry is not None and ts.date() >= expiry.date() \
                        and ts.time() >= pd.Timestamp('15:15').time():
                    exit_reason = 'expiry'
                    exec_ts     = expiry
                    exec_col    = 'close'

                if exit_reason:
                    # Step 1: Gap-fill between signal candle and exec candle
                    if ts < exec_ts:
                        (snap_buy_ltp, snap_sell_ltp,
                         _gf_exit_ts, _gf_exit_type,
                         trailing_profit_floor, max_unrealised_pl_so_far) = \
                            _append_1min_snapshots_window(
                                ts, exec_ts - pd.Timedelta(minutes=1),
                                nifty_1m, vix_1m, nifty_75_indexed, nifty_15,
                                buy_opt_df, sell_opt_df,
                                buy_strike, sell_strike, option_type,
                                buy_entry, sell_entry, direction, expiry,
                                net_debit, max_profit,
                                trade_log,
                                snap_buy_ltp, snap_sell_ltp,
                                trailing_profit_floor, max_unrealised_pl_so_far,
                                trade_entry_date, gate_date, use_trailing, entry_vix,
                    active_pt_pct, active_gate_pct, active_hard_stop,
                            )
                        if _gf_exit_ts is not None:
                            exec_ts     = _gf_exit_ts + pd.Timedelta(minutes=1)
                            exec_col    = 'open'
                            exit_reason = _gf_exit_type
                            buy_ltp     = snap_buy_ltp
                            sell_ltp    = snap_sell_ltp

                    # Step 2: Fetch exit prices
                    buy_exit_raw  = get_option_price(buy_opt_df,  exec_ts, exec_col) or buy_ltp
                    sell_exit_raw = get_option_price(sell_opt_df, exec_ts, exec_col) or sell_ltp

                    if exit_reason == 'expiry':
                        buy_exit_net  = buy_exit_raw
                        sell_exit_net = sell_exit_raw
                    else:
                        buy_exit_net  = apply_slippage(buy_exit_raw,  is_buy=False)
                        sell_exit_net = apply_slippage(sell_exit_raw, is_buy=True)

                    base_pl = _calc_pl(buy_entry, buy_exit_net, sell_entry, sell_exit_net)

                    # Step 3: Exit additional lots simultaneously
                    if has_additional:
                        add_buy_exit_raw  = get_option_price(buy_opt_df,  exec_ts, exec_col) or add_buy_ltp
                        add_sell_exit_raw = get_option_price(sell_opt_df, exec_ts, exec_col) or add_sell_ltp
                        if exit_reason == 'expiry':
                            add_buy_exit_net  = add_buy_exit_raw
                            add_sell_exit_net = add_sell_exit_raw
                        else:
                            add_buy_exit_net  = apply_slippage(add_buy_exit_raw,  is_buy=False)
                            add_sell_exit_net = apply_slippage(add_sell_exit_raw, is_buy=True)
                        add_booked_pl  = _calc_pl(
                            add_buy_entry,  add_buy_exit_net,
                            add_sell_entry, add_sell_exit_net)
                        has_additional = False

                    # Step 4: Normalised P&L
                    pl_points = round(
                        base_pl + add_booked_pl * ADDITIONAL_LOT_MULTIPLIER, 2)
                    pl_rupees = pl_points * LOT_SIZE

                    # Exit snapshot
                    exec_spot_val = get_1min_value(nifty_1m, exec_ts, 'close') or spot
                    exec_vix_val  = get_1min_value(vix_1m,   exec_ts, 'close')
                    prior_75_exec = nifty_75_indexed[nifty_75_indexed.index <= exec_ts]
                    trend_75_exec = prior_75_exec.iloc[-1]['trend'] \
                                    if not prior_75_exec.empty else trend_75
                    prior_15_exec = nifty_15[nifty_15['time_stamp'] <= exec_ts]
                    trend_15_exec = prior_15_exec.iloc[-1]['trend'] \
                                    if not prior_15_exec.empty else trend_15
                    _exit_days = (exec_ts.date() - trade_entry_date).days if trade_entry_date else 0
                    exit_snapshot = _build_snapshot(
                        exec_ts, exec_spot_val, exec_vix_val,
                        buy_strike, sell_strike, option_type,
                        buy_exit_raw, sell_exit_raw,
                        buy_entry, sell_entry,
                        direction, trend_75_exec, trend_15_exec, expiry,
                        net_debit=net_debit, max_profit=max_profit,
                        days_in_trade=_exit_days,
                        trailing_profit_floor=trailing_profit_floor,
                        max_unrealised_pl_so_far=max_unrealised_pl_so_far,
                        entry_vix=entry_vix,
                        realised_pl_pts=pl_points,
                        realised_pl_rs=pl_rupees,
                    )
                    trade_log.append(exit_snapshot)

                    exit_vix    = get_1min_value(vix_1m, exec_ts, 'close')
                    trade_stats = _compute_trade_stats(trade_log, direction)
                    trade_record = _build_trade_record(
                        entry_time, exec_ts, direction, expiry,
                        buy_strike, sell_strike, option_type,
                        entry_spot, spot,
                        buy_entry, sell_entry,
                        buy_exit_net, sell_exit_net,
                        net_debit, max_profit,
                        pl_points, pl_rupees, exit_reason,
                        trade_stats=trade_stats,
                        entry_vix=entry_vix, exit_vix=exit_vix,
                        trail_active=use_trailing,
                active_pt_pct=active_pt_pct,
                active_gate_pct=active_gate_pct,
                active_gate_days=active_gate_days,
                active_hard_stop=active_hard_stop,
                    )
                    trade_counter += 1
                    all_trades.append(trade_record)
                    _log_exit(trade_record)
                    _save_trade_log(trade_counter, entry_time, trade_log)

                    # Reset all trade state
                    in_trade      = False; trade_log      = []
                    direction     = None;  buy_strike     = None
                    sell_strike   = None;  option_type    = None
                    expiry        = None;  elm_time       = None
                    buy_entry     = None;  sell_entry     = None
                    net_debit     = None;  max_profit     = None
                    buy_opt_df    = None;  sell_opt_df    = None
                    snap_buy_ltp  = None;  snap_sell_ltp  = None
                    entry_exec_ts = None;  entry_vix      = None
                    has_additional    = False; add_buy_entry  = None
                    add_sell_entry    = None;  add_buy_ltp    = None
                    add_sell_ltp      = None;  add_booked_pl  = 0.0
                    trailing_profit_floor    = None
                    max_unrealised_pl_so_far = 0.0
                    trade_entry_date         = None
                    gate_date                = None
                    use_trailing             = False
                    active_pt_pct            = None
                    active_gate_pct          = None
                    active_gate_days         = None
                    active_hard_stop         = None
                    sl_1min_ts               = None
                    sl_1min_type             = None

            # --------------------------------------------------------------
            # Entry / re-entry
            # --------------------------------------------------------------
            if not in_trade and flip_15:
                entry_direction = None
                if trend_75 == True  and trend_15 == True:
                    entry_direction = 'bullish'
                elif trend_75 == False and trend_15 == False:
                    entry_direction = 'bearish'

                if entry_direction is None or not has_next:
                    continue

                # Entry filters — evaluated on signal candle time (ts), not exec_ts
                # VIX is read at signal time for the bullish VIX exclusion filter
                _signal_vix = get_1min_value(vix_1m, ts, 'close')
                if not entry_allowed(ts, entry_direction, _signal_vix):
                    continue

                exec_spot = next_row['open']

                selected_expiry = get_expiry(exec_ts, contracts_df, holidays_set)
                if selected_expiry is None:
                    logger.warning(f"  No expiry found for {exec_ts} — skipping")
                    continue

                sel_buy_strike, sel_sell_strike, sel_otype = select_strikes(
                    exec_spot, entry_direction)

                cache_key_buy  = (selected_expiry, sel_buy_strike,  sel_otype)
                cache_key_sell = (selected_expiry, sel_sell_strike, sel_otype)
                if cache_key_buy  not in option_df_cache:
                    option_df_cache[cache_key_buy]  = load_option_data(
                        selected_expiry, sel_buy_strike,  sel_otype)
                if cache_key_sell not in option_df_cache:
                    option_df_cache[cache_key_sell] = load_option_data(
                        selected_expiry, sel_sell_strike, sel_otype)

                sel_buy_df  = option_df_cache[cache_key_buy]
                sel_sell_df = option_df_cache[cache_key_sell]

                buy_entry_raw  = get_option_price(sel_buy_df,  exec_ts, 'open')
                sell_entry_raw = get_option_price(sel_sell_df, exec_ts, 'open')

                if buy_entry_raw is None or sell_entry_raw is None:
                    logger.debug(f"  Option data missing at {exec_ts} — skipping")
                    continue

                sel_buy_entry  = apply_slippage(buy_entry_raw,  is_buy=True)
                sel_sell_entry = apply_slippage(sell_entry_raw, is_buy=False)
                sel_net_debit  = sel_buy_entry - sel_sell_entry
                sel_max_profit = HEDGE_POINTS - sel_net_debit

                if sel_net_debit <= 0 or sel_max_profit <= 0:
                    logger.debug(
                        f"  Invalid spread at {exec_ts}: "
                        f"net_debit={sel_net_debit:.1f}, max_profit={sel_max_profit:.1f} — skipping")
                    continue

                expiry_row   = contracts_df[contracts_df['end_date'] == selected_expiry]
                sel_elm_time = expiry_row['elm_time'].iloc[0] if not expiry_row.empty else None

                # Resolve direction-specific parameters once at entry.
                # entry_vix for resolution uses exec_ts (same as PT/gate lookups).
                _entry_vix_for_resolve = get_1min_value(vix_1m, exec_ts, 'close')

                # VIX-band base PT PCT (used when no directional override)
                if _entry_vix_for_resolve is not None and _entry_vix_for_resolve >= PROFIT_TARGET_VIX_HIGH:
                    _base_pt_pct = PROFIT_TARGET_PCT_HIGH_VIX
                elif _entry_vix_for_resolve is not None and _entry_vix_for_resolve >= PROFIT_TARGET_VIX_LOW:
                    _base_pt_pct = PROFIT_TARGET_PCT_MID_VIX
                else:
                    _base_pt_pct = PROFIT_TARGET_PCT_LOW_VIX

                # VIX-band base gate PCT (used when no directional override)
                if _entry_vix_for_resolve is not None and _entry_vix_for_resolve < TIME_GATE_VIX_THRESHOLD:
                    _base_gate_pct = TIME_GATE_MIN_PROFIT_PCT_LOW_VIX
                else:
                    _base_gate_pct = TIME_GATE_MIN_PROFIT_PCT_HIGH_VIX

                _sel_active_pt_pct    = resolve_param(entry_direction,
                                            PROFIT_TARGET_PCT_BULL,
                                            PROFIT_TARGET_PCT_BEAR,
                                            _base_pt_pct)
                _sel_active_gate_pct  = resolve_param(entry_direction,
                                            TIME_GATE_MIN_PROFIT_PCT_BULL,
                                            TIME_GATE_MIN_PROFIT_PCT_BEAR,
                                            _base_gate_pct)
                _sel_active_gate_days = resolve_param(entry_direction,
                                            TIME_GATE_DAYS_BULL,
                                            TIME_GATE_DAYS_BEAR,
                                            TIME_GATE_DAYS)
                _sel_active_hard_stop = resolve_param(entry_direction,
                                            HARD_STOP_POINTS_BULL,
                                            HARD_STOP_POINTS_BEAR,
                                            HARD_STOP_POINTS)

                in_trade      = True
                direction     = entry_direction
                buy_strike    = sel_buy_strike
                sell_strike   = sel_sell_strike
                option_type   = sel_otype
                expiry        = selected_expiry
                elm_time      = sel_elm_time
                entry_time    = exec_ts
                entry_spot    = exec_spot
                entry_vix     = _entry_vix_for_resolve
                buy_entry     = sel_buy_entry
                sell_entry    = sel_sell_entry
                net_debit     = sel_net_debit
                max_profit    = sel_max_profit
                buy_ltp       = buy_entry
                sell_ltp      = sell_entry
                buy_opt_df    = sel_buy_df
                sell_opt_df   = sel_sell_df
                trade_log     = []
                snap_buy_ltp  = buy_entry
                snap_sell_ltp = sell_entry
                entry_exec_ts = exec_ts
                has_additional           = ENABLE_ADDITIONAL_LOTS
                add_buy_entry            = buy_entry
                add_sell_entry           = sell_entry
                add_buy_ltp              = buy_entry
                add_sell_ltp             = sell_entry
                add_booked_pl            = 0.0
                trailing_profit_floor    = None
                max_unrealised_pl_so_far = 0.0
                trade_entry_date         = exec_ts.date()
                active_pt_pct            = _sel_active_pt_pct
                active_gate_pct          = _sel_active_gate_pct
                active_gate_days         = _sel_active_gate_days
                active_hard_stop         = _sel_active_hard_stop
                gate_date                = get_gate_date(exec_ts, active_gate_days, holidays_set)
                use_trailing             = (
                    ENABLE_TRAILING_PROFIT and
                    entry_vix is not None and
                    entry_vix >= TRAIL_VIX_THRESHOLD
                )

                logger.info(
                    f"  ENTRY {direction:8s} | {exec_ts} | "
                    f"Spot: {exec_spot:.0f} | "
                    f"Buy  {sel_buy_strike}{sel_otype.upper()} @ {buy_entry:.1f} | "
                    f"Sell {sel_sell_strike}{sel_otype.upper()} @ {sell_entry:.1f} | "
                    f"Debit: {net_debit:.1f} | MaxProfit: {max_profit:.1f} | "
                    f"Expiry: {selected_expiry.date()}"
                )

        # ------------------------------------------------------------------
        # Capture 15:16–15:30 tail on high-VIX days
        # ------------------------------------------------------------------
        if in_trade:
            day_close   = pd.Timestamp(f"{day_date} 15:30:00")
            last_anchor = last_15min_ts if last_15min_ts is not None \
                          else pd.Timestamp(f"{day_date} 15:15:00")
            if last_anchor < day_close:
                (snap_buy_ltp, snap_sell_ltp, _, _,
                 trailing_profit_floor, max_unrealised_pl_so_far) = \
                    _append_1min_snapshots_window(
                        last_anchor, day_close,
                        nifty_1m, vix_1m, nifty_75_indexed, nifty_15,
                        buy_opt_df, sell_opt_df,
                        buy_strike, sell_strike, option_type,
                        buy_entry, sell_entry, direction, expiry,
                        net_debit, max_profit,
                        trade_log,
                        snap_buy_ltp, snap_sell_ltp,
                        trailing_profit_floor, max_unrealised_pl_so_far,
                        trade_entry_date, gate_date, use_trailing, entry_vix,
                    active_pt_pct, active_gate_pct, active_hard_stop,
                    )

    logger.info(f"Backtest complete. Total trades: {len(all_trades)}")
    return all_trades


# ---------------------------------------------------------------------------
# Summary and entry point
# ---------------------------------------------------------------------------

def save_trade_summary(all_trades: list):
    """Save consolidated trade summary and print statistics."""
    if not all_trades:
        logger.info("No trades generated.")
        return

    df = pd.DataFrame(all_trades)
    df.to_csv(TRADE_SUMMARY_FILE, index=False)

    total    = len(df)
    winners  = df[df['pl_points'] > 0]
    losers   = df[df['pl_points'] <= 0]
    win_rate = len(winners) / total * 100
    avg_win  = winners['pl_points'].mean() if len(winners) else 0
    avg_loss = losers['pl_points'].mean()  if len(losers)  else 0
    total_pl = df['pl_rupees'].sum()

    streak = max_streak = 0
    for pl in df['pl_points']:
        if pl <= 0:
            streak    += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    logger.info("=" * 60)
    logger.info("APOLLO DEBIT SPREAD BACKTEST SUMMARY")
    logger.info("=" * 60)
    logger.info(f"  Total trades       : {total}")
    logger.info(f"  Winners            : {len(winners)} ({win_rate:.1f}%)")
    logger.info(f"  Losers             : {len(losers)}")
    logger.info(f"  Avg winner (pts)   : {avg_win:.2f}")
    logger.info(f"  Avg loser  (pts)   : {avg_loss:.2f}")
    if avg_loss != 0:
        logger.info(f"  Reward:Risk        : {abs(avg_win/avg_loss):.2f}")
    logger.info(f"  Max consec losses  : {max_streak}")
    logger.info(f"  Total P&L (₹)      : {total_pl:+,.0f}")
    logger.info(f"  Exit breakdown:")
    for reason, count in df['exit_reason'].value_counts().items():
        logger.info(f"    {reason:25s}: {count}")
    logger.info("=" * 60)
    logger.info(f"  Saved to: {TRADE_SUMMARY_FILE}")


if __name__ == "__main__":
    logger.info("=== Apollo Debit Spread Backtest starting ===")
    logger.info(f"  Spread type    : {SPREAD_TYPE}")
    logger.info(f"  BUY_LEG_OFFSET : {BUY_LEG_OFFSET}")
    logger.info(f"  HEDGE_POINTS   : {HEDGE_POINTS}")
    logger.info(f"  VIX threshold  : {VIX_THRESHOLD}")
    logger.info(f"  Additional lots: {'ON' if ENABLE_ADDITIONAL_LOTS else 'OFF'}")
    logger.info(f"  Hard stop      : {'ON' if ENABLE_HARD_STOP else 'OFF'} — -{HARD_STOP_POINTS:.1f} pts")
    logger.info(f"  Profit target  : {'ON' if ENABLE_PROFIT_TARGET else 'OFF'} — "
                f"{PROFIT_TARGET_PCT_LOW_VIX*100:.0f}%/<{PROFIT_TARGET_VIX_LOW:.0f} | "
                f"{PROFIT_TARGET_PCT_MID_VIX*100:.0f}%/{PROFIT_TARGET_VIX_LOW:.0f}-{PROFIT_TARGET_VIX_HIGH:.0f} | "
                f"{PROFIT_TARGET_PCT_HIGH_VIX*100:.0f}%/≥{PROFIT_TARGET_VIX_HIGH:.0f}")
    logger.info(f"  Day 0 SL       : {'ON' if ENABLE_DAY0_SPREAD_SL else 'OFF'} — {DAY0_SPREAD_SL_PCT*100:.0f}% of net debit")
    logger.info(f"  Time gate      : {'ON' if ENABLE_TIME_GATE else 'OFF'} — {TIME_GATE_DAYS}d, from {TIME_GATE_CHECK_TIME}, "
                f"{TIME_GATE_MIN_PROFIT_PCT_LOW_VIX*100:.0f}%/<VIX{TIME_GATE_VIX_THRESHOLD:.0f} | "
                f"{TIME_GATE_MIN_PROFIT_PCT_HIGH_VIX*100:.0f}%/≥VIX{TIME_GATE_VIX_THRESHOLD:.0f}")
    logger.info(f"  Trailing profit: {'ON' if ENABLE_TRAILING_PROFIT else 'OFF'} — VIX >= {TRAIL_VIX_THRESHOLD}, triggers {TRAIL_TRIGGER_1}/{TRAIL_TRIGGER_2}/{TRAIL_TRIGGER_3}")
    logger.info(f"  Excl. days     : {EXCLUDE_TRADE_DAYS if EXCLUDE_TRADE_DAYS else 'none'}")
    logger.info(f"  Excl. candles  : {EXCLUDE_SIGNAL_CANDLES if EXCLUDE_SIGNAL_CANDLES else 'none'}")
    logger.info(f"  Excl. bull VIX : {EXCLUDE_BULLISH_VIX_ABOVE if EXCLUDE_BULLISH_VIX_ABOVE is not None else 'none'}")
    logger.info(f"  PT  bull/bear  : {PROFIT_TARGET_PCT_BULL}/{PROFIT_TARGET_PCT_BEAR} (None=use base)")
    logger.info(f"  Gate bull/bear : pct {TIME_GATE_MIN_PROFIT_PCT_BULL}/{TIME_GATE_MIN_PROFIT_PCT_BEAR} | days {TIME_GATE_DAYS_BULL}/{TIME_GATE_DAYS_BEAR}")
    logger.info(f"  Stop bull/bear : {HARD_STOP_POINTS_BULL}/{HARD_STOP_POINTS_BEAR} pts (None=use base)")

    nifty_15, nifty_75, vix_daily = load_precomputed()
    nifty_1m, vix_1m              = load_1min_data()

    holidays_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data_pipeline', 'config', 'holidays.csv')
    holidays_df_elm = pd.read_csv(holidays_path, parse_dates=['date'])
    holidays_df_elm['date'] = pd.to_datetime(holidays_df_elm['date']).dt.date
    contracts_df = load_contracts(holidays_df_elm)

    logger.info(f"  Contracts      : {len(contracts_df)} expiries")

    all_trades = run_backtest(
        nifty_15, nifty_75, vix_daily, contracts_df,
        nifty_1m, vix_1m, holidays_df_elm)
    save_trade_summary(all_trades)

    logger.info("=== Apollo Debit Spread Backtest complete ===")