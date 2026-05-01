"""
backtest.py — Athena Backtest Engine
Double calendar spread strategy on Nifty weekly options (Calendar Condor).

Structure:
  - Sell flat-delta CE and PE on the near-term expiry (sell leg)
  - Buy same strikes on the last expiry of the current month (buy leg)
  - Buy far-OTM wings (e.g. 5-delta) on the monthly expiry for gap protection
  - Entry: 10:30 AM on the day before the sell expiry
  - Strike rounding: nearest 100 points

Execution model:
  - Entry at ENTRY_TIME on the day before sell expiry
  - Pre-expiry exit at ELM_EXIT_TIME on the day before sell expiry
  - SL fires on 1-min candle close → exit at open of next 1-min candle
  - All six legs always exit simultaneously on any trigger
"""

import os
import sys
import logging
import warnings
from datetime import date, timedelta

import pandas as pd
import numpy as np
import mibian

# Add parent directory to path so we can import from apollo_backtest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from apollo_backtest.technical_indicators import SupertrendIndicator

from configs_realtime import (
    NIFTY_INDEX_FILE, VIX_INDEX_FILE,
    NIFTY_OPTIONS_PATH, CONTRACT_LIST_FILE,
    DATA_DIR, TRADE_LOGS_DIR, TRADE_SUMMARY_FILE,
    ENTRY_TIME, STRIKE_STEP, BUY_LEG_MIN_DTE,
    VIX_DELTA_BANDS,
    ENABLE_VIX_FILTER, VIX_FILTER_LOW, VIX_FILTER_HIGH,
    ENABLE_PROFIT_TARGET, PROFIT_TARGET_PCT_NET_DEBIT,
    ENABLE_INDEX_SL, INDEX_SL_OFFSET,
    ENABLE_OPTION_SL, OPTION_SL_MULTIPLIER,
    ENABLE_SPREAD_SL, SPREAD_SL_POINTS,
    ENABLE_TRAIL_STOP, TRAIL_ACTIVATION_POINTS, TRAIL_POINTS,
    ENABLE_ASYMMETRIC_DELTA, DELTA_TESTED_SIDE, DELTA_SAFE_SIDE,
    ENABLE_SAFETY_WINGS, SAFETY_WING_DELTA,
    ELM_EXIT_TIME,
    ENABLE_ADJUSTMENT, ADJUST_BUY_LEG,
    ADJUSTMENT_TRIGGER_OFFSET, ADJUSTMENT_WING_THRESHOLD, ADJUSTMENT_MIN_CREDIT_GAIN,
    ADJUSTMENT_NEW_STRIKE_DISTANCE, ADJUSTMENT_EXCLUDED_DAYS,
    ENABLE_EMERGENCY_HEDGE, EMERGENCY_HEDGE_DELTA, EMERGENCY_TRIGGER_OFFSET,
    EMERGENCY_EXIT_OFFSET, EMERGENCY_MAX_ATTEMPTS,
    SLIPPAGE_POINTS, LOT_SIZE, RISK_FREE_RATE,
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
# Trading day helper — used by load_contracts and run_backtest
# ---------------------------------------------------------------------------

def last_trading_day_before(target_date: date, holidays_set: set) -> date:
    """
    Return the last trading day strictly before target_date.
    Steps back one calendar day at a time, skipping weekends and holidays.
    Returns None if no valid trading day found within 10 calendar days.
    Used for both elm_time computation (last trading day before sell expiry)
    and entry date computation (last trading day before prior expiry).
    """
    d = target_date - timedelta(days=1)
    for _ in range(10):
        if d.weekday() < 5 and d not in holidays_set:
            return d
        d -= timedelta(days=1)
    return None


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_index_data():
    """
    Load 1-min Nifty spot and VIX data.
    Strips timezone info and indexes by timestamp for fast lookups.
    """
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

    logger.info(f"  1-min Nifty : {len(nifty_1m):,} rows")
    logger.info(f"  1-min VIX   : {len(vix_1m):,} rows")
    return nifty_1m, vix_1m


def load_contracts(holidays_df: pd.DataFrame = None) -> pd.DataFrame:
    """
    Load Nifty weekly expiry contract list.
    Computes elm_time = 15:15 on the last trading day before each expiry.
    Holiday-adjusted via last_trading_day_before() — handles any number of
    consecutive holidays or weekend bridges correctly.
    """
    df = pd.read_csv(CONTRACT_LIST_FILE)
    df['expiry_date'] = pd.to_datetime(df['expiry_date'], utc=False).dt.tz_localize(None)
    df['end_date']    = pd.to_datetime(df['end_date'],    utc=False).dt.tz_localize(None)

    holidays_set = set()
    if holidays_df is not None:
        holidays_set = set(holidays_df['date'].values)

    elm_times = []
    for _, row in df.iterrows():
        expiry_date = row['expiry_date'].date()
        last_trading = last_trading_day_before(expiry_date, holidays_set)
        if last_trading is not None:
            elm = pd.Timestamp(f"{last_trading} {ELM_EXIT_TIME}:00")
        else:
            # Fallback: should never happen with a valid contract list
            elm = row['end_date'] - pd.Timedelta(seconds=87300)
        elm_times.append(elm)

    df['elm_time'] = elm_times
    df = df.sort_values('expiry_date').reset_index(drop=True)
    return df


def load_option_data(expiry_date: pd.Timestamp, strike: int,
                     option_type: str,
                     _debug_entry: str = None) -> pd.DataFrame:
    """
    Load 1-min option data for a given expiry, strike and type.
    Returns empty DataFrame if file not found.
    """
    expiry_str = expiry_date.strftime('%Y-%m-%d')
    filepath   = os.path.join(
        NIFTY_OPTIONS_PATH, expiry_str, f"{strike}{option_type}.csv")
    if _debug_entry:
        logger.info(f"  [FILE-DEBUG] {_debug_entry} | loading: {filepath} | exists={os.path.exists(filepath)}")
    if not os.path.exists(filepath):
        return pd.DataFrame()
    df = pd.read_csv(filepath, parse_dates=['datetime'])
    df['datetime'] = pd.to_datetime(df['datetime'])
    return df


def get_option_price(option_df: pd.DataFrame, timestamp: pd.Timestamp,
                     price_col: str = 'open') -> float:
    """
    Get option price at a given timestamp.
    Falls back to last available close price before timestamp if exact match not found.
    Returns None if no data available.
    """
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
    """
    Get value from timestamp-indexed 1-min DataFrame.
    Falls back to last available value before timestamp.
    Returns None if not found.
    """
    if timestamp in indexed_df.index:
        val = indexed_df.loc[timestamp, col]
        return float(val) if pd.notna(val) else None
    prior = indexed_df[indexed_df.index < timestamp]
    if not prior.empty:
        return float(prior[col].iloc[-1])
    return None


def get_supertrend_regime(st_df: pd.DataFrame, ts: pd.Timestamp) -> bool:
    """
    True if Bullish (Close > Supertrend), False if Bearish.
    Uses the last completed 75m candle.
    """
    try:
        # Find the latest 75m candle that ends before or at ts
        idx = st_df.index.get_indexer([ts], method='ffill')[0]
        if idx == -1: return True # Default Bullish
        return bool(st_df.iloc[idx]['is_bullish'])
    except Exception:
        return True # Default Bullish


# ---------------------------------------------------------------------------
# Expiry helpers
# ---------------------------------------------------------------------------

def get_prior_expiry(sell_expiry_date: date,
                     contracts_df: pd.DataFrame) -> date:
    """
    Return the expiry immediately before sell_expiry_date in the contract list.
    This is the expiry whose option data is being sold.
    Entry = trading day before this prior expiry.
    Returns None if no prior expiry exists.
    """
    expiry_dates = sorted(contracts_df['expiry_date'].dt.date.unique())
    prior = [d for d in expiry_dates if d < sell_expiry_date]
    return prior[-1] if prior else None


def compute_entry_date(prior_expiry_date: date,
                       holidays_set: set) -> date:
    """
    Return the trading day immediately before prior_expiry_date.
    Entry = last trading day before the expiry preceding the sell expiry.
    Example: sell_expiry=14 Aug, prior_expiry=7 Aug → entry=6 Aug.
    """
    return last_trading_day_before(prior_expiry_date, holidays_set)


def select_buy_expiry(entry_date: date,
                      sell_expiry_date: date,
                      contracts_df: pd.DataFrame) -> date:
    """
    Select the buy leg expiry from the contract list.
    Buy expiry = last expiry of the current calendar month.
    If DTE from entry_date < BUY_LEG_MIN_DTE, roll to last expiry of next month.
    Must be strictly after sell_expiry_date.
    Returns buy_expiry_date, or None if not found.
    """
    expiry_dates = sorted(contracts_df['expiry_date'].dt.date.unique())

    # Last expiry of current month
    current_month = [d for d in expiry_dates
                     if d.year == entry_date.year and d.month == entry_date.month]
    buy_expiry = max(current_month) if current_month else None

    # Roll to next month if DTE too short or not found
    if buy_expiry is None or (buy_expiry - entry_date).days < BUY_LEG_MIN_DTE:
        next_month      = entry_date.month % 12 + 1
        next_month_year = entry_date.year + (1 if entry_date.month == 12 else 0)
        next_month_expiries = [d for d in expiry_dates
                               if d.year == next_month_year and d.month == next_month]
        buy_expiry = max(next_month_expiries) if next_month_expiries else None

    if buy_expiry is None:
        return None

    # Buy expiry must be strictly after sell expiry
    if buy_expiry <= sell_expiry_date:
        return None

    return buy_expiry


def get_end_date(expiry_date: date, contracts_df: pd.DataFrame) -> pd.Timestamp:
    """
    Look up the accurate end_date (15:30 timestamp) for a given expiry date.
    Returns None if not found in contract list.
    """
    target = pd.Timestamp(expiry_date)
    mask   = contracts_df['expiry_date'].dt.date == expiry_date
    rows   = contracts_df[mask]
    if rows.empty:
        return None
    return rows.iloc[0]['end_date']


def get_elm_time(sell_expiry_date: date, contracts_df: pd.DataFrame) -> pd.Timestamp:
    """Return pre-computed elm_time for the sell expiry from contract list."""
    mask = contracts_df['expiry_date'].dt.date == sell_expiry_date
    rows = contracts_df[mask]
    if rows.empty:
        return None
    return rows.iloc[0]['elm_time']


# ---------------------------------------------------------------------------
# mibian — delta and theoretical value computation
# ---------------------------------------------------------------------------

def compute_delta(spot: float, strike: int, dte_days: float,
                  option_price: float, option_type: str) -> float:
    """
    Back out IV from market price using mibian, then compute delta.
    Returns absolute delta value, or None if computation fails.
    option_type: 'ce' or 'pe'
    """
    try:
        if option_type == 'ce':
            implied = mibian.BS(
                [spot, strike, RISK_FREE_RATE, dte_days], callPrice=option_price)
        else:
            implied = mibian.BS(
                [spot, strike, RISK_FREE_RATE, dte_days], putPrice=option_price)
        iv = implied.impliedVolatility
        if iv is None or iv <= 0 or iv > 500:
            return None
        bs    = mibian.BS([spot, strike, RISK_FREE_RATE, dte_days], volatility=iv)
        delta = abs(bs.callDelta if option_type == 'ce' else bs.putDelta)
        return delta
    except Exception:
        return None


def compute_iv(spot: float, strike: int, dte_days: float,
               option_price: float, option_type: str) -> float:
    """
    Back out implied volatility from market price using mibian.
    Returns IV (%), or None if computation fails.
    """
    try:
        if option_type == 'ce':
            implied = mibian.BS(
                [spot, strike, RISK_FREE_RATE, dte_days], callPrice=option_price)
        else:
            implied = mibian.BS(
                [spot, strike, RISK_FREE_RATE, dte_days], putPrice=option_price)
        iv = implied.impliedVolatility
        if iv is None or iv <= 0 or iv > 500:
            return None
        return iv
    except Exception:
        return None


def compute_theoretical_value(spot: float, strike: int, dte_days: float,
                               iv: float, option_type: str) -> float:
    """
    Compute theoretical option value using Black-Scholes at given IV and DTE.
    Returns theoretical price, or None if computation fails.
    """
    try:
        if dte_days <= 0:
            return None
        bs = mibian.BS([spot, strike, RISK_FREE_RATE, dte_days], volatility=iv)
        return bs.callPrice if option_type == 'ce' else bs.putPrice
    except Exception:
        return None


def compute_max_theoretical_profit(spot: float,
                                    ce_sell_strike: int, pe_sell_strike: int,
                                    sell_expiry: pd.Timestamp, buy_expiry: pd.Timestamp,
                                    entry_ts: pd.Timestamp,
                                    ce_sell_df, pe_sell_df,
                                    ce_buy_df, pe_buy_df,
                                    ce_sell_entry: float, pe_sell_entry: float,
                                    ce_buy_entry: float, pe_buy_entry: float) -> float:
    """
    Approximate max theoretical profit of the double calendar at entry.

    Max profit of a calendar occurs when spot pins exactly at the sell strike
    at sell expiry. At that point:
      - Sell leg expires worthless (full premium received is kept)
      - Buy leg has remaining DTE and is ATM — maximum time value

    For each side:
      1. Back out IV from sell leg market price (using current spot, OTM)
      2. Project buy leg value at sell expiry assuming spot = sell_strike (ATM pin)
         This uses sell leg IV and buy leg remaining DTE after sell expiry
      3. Max profit per side = sell_entry + proj_buy - buy_entry
         (sell premium kept + buy leg ATM value at sell expiry - cost of buy leg)

    Combined = CE side + PE side.
    Returns combined max theoretical profit, or fallback (total net debit) on failure.
    """
    sell_dte = max((sell_expiry.date() - entry_ts.date()).days, 0.5)
    # Remaining DTE of buy leg after sell expiry
    buy_dte_at_sell_expiry = max((buy_expiry.date() - sell_expiry.date()).days, 0.5)

    def side_max_profit(sell_strike, sell_entry_raw, buy_entry_raw, opt_type):
        # Back out IV from sell leg at current spot (OTM)
        iv = compute_iv(spot, sell_strike, sell_dte, sell_entry_raw, opt_type)
        if iv is None:
            return None
        # Project buy leg value at sell expiry with spot pinned at sell_strike (ATM)
        # This is the scenario that maximises calendar P&L
        proj_buy = compute_theoretical_value(
            sell_strike, sell_strike, buy_dte_at_sell_expiry, iv, opt_type)
        if proj_buy is None:
            return None
        # Max profit = sell premium kept + buy leg ATM value - cost of buy leg
        return sell_entry_raw + proj_buy - buy_entry_raw

    # Fetch raw (pre-slippage) entry prices for IV computation
    ce_sell_raw = get_option_price(ce_sell_df, entry_ts, 'open')
    pe_sell_raw = get_option_price(pe_sell_df, entry_ts, 'open')
    ce_buy_raw  = get_option_price(ce_buy_df,  entry_ts, 'open')
    pe_buy_raw  = get_option_price(pe_buy_df,  entry_ts, 'open')

    ce_max = None
    pe_max = None

    if ce_sell_raw and ce_buy_raw:
        ce_max = side_max_profit(ce_sell_strike, ce_sell_raw, ce_buy_raw, 'ce')

    if pe_sell_raw and pe_buy_raw:
        pe_max = side_max_profit(pe_sell_strike, pe_sell_raw, pe_buy_raw, 'pe')

    if ce_max is not None and pe_max is not None:
        return round(ce_max + pe_max, 2)

    # Fallback: use total net debit as conservative proxy
    net_ce = (ce_buy_entry - ce_sell_entry) if ce_buy_entry and ce_sell_entry else 0
    net_pe = (pe_buy_entry - pe_sell_entry) if pe_buy_entry and pe_sell_entry else 0
    return round(net_ce + net_pe, 2)


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------

def get_target_delta(entry_vix: float) -> float:
    """
    Return the target delta for the given entry VIX level.
    Selects the first band in VIX_DELTA_BANDS where entry_vix <= vix_upper_bound.
    Falls back to the last band's delta if entry_vix exceeds all bounds.
    """
    for vix_upper, delta in VIX_DELTA_BANDS:
        if entry_vix <= vix_upper:
            return delta
    return VIX_DELTA_BANDS[-1][1]


def select_strike(spot: float, sell_expiry: pd.Timestamp,
                  entry_ts: pd.Timestamp, option_type: str,
                  opt_df_cache: dict,
                  target_delta: float = None) -> tuple:
    """
    Scan strikes from ATM outward and select first with abs(delta) <= target_delta.
    ATM rounded to nearest STRIKE_STEP (100 for Nifty).
    CE: scan upward from ATM. PE: scan downward from ATM.

    target_delta: the delta threshold to use. If None, falls back to last
    band in VIX_DELTA_BANDS (should always be provided by caller).

    Returns (strike, entry_price_raw) or (None, None) if no valid strike found.
    entry_price_raw is before slippage.
    """
    if target_delta is None:
        target_delta = VIX_DELTA_BANDS[-1][1]
    dte_days = max((sell_expiry.date() - entry_ts.date()).days, 0.5)
    atm      = int(round(spot / STRIKE_STEP) * STRIKE_STEP)

    if option_type == 'ce':
        candidates = range(atm, atm + 5000, STRIKE_STEP)
    else:
        candidates = range(atm, atm - 5000, -STRIKE_STEP)

    for strike in candidates:
        cache_key = (sell_expiry, strike, option_type)
        if cache_key not in opt_df_cache:
            opt_df_cache[cache_key] = load_option_data(sell_expiry, strike, option_type)

        opt_df = opt_df_cache[cache_key]
        price  = get_option_price(opt_df, entry_ts, 'open')

        if price is None or price <= 0.5:
            continue

        delta = compute_delta(spot, strike, dte_days, price, option_type)
        if delta is None:
            continue

        if delta <= target_delta:
            return strike, price

    return None, None


# ---------------------------------------------------------------------------
# Slippage
# ---------------------------------------------------------------------------

def apply_slippage(price: float, is_buy: bool) -> float:
    """Add slippage to buys, subtract from sells. Floor at 0."""
    return (price + SLIPPAGE_POINTS) if is_buy else max(price - SLIPPAGE_POINTS, 0.0)


# ---------------------------------------------------------------------------
# P&L computation
# ---------------------------------------------------------------------------

def calc_strategy_pl(sell_entry: float, sell_ltp: float,
                     buy_entry: float,  buy_ltp: float,
                     wing_entry: float = 0.0, wing_ltp: float = 0.0,
                     emer_entry: float = 0.0, emer_ltp: float = 0.0) -> float:
    """
    Unrealised P&L for one side of the strategy (CE or PE).
    (sell_entry - sell_ltp) + (buy_ltp - buy_entry) + (wing_ltp - wing_entry)
    """
    sell_pl = sell_entry - sell_ltp
    buy_pl  = buy_ltp - buy_entry
    wing_pl = wing_ltp - wing_entry
    emer_pl = emer_ltp - emer_entry
    return round(sell_pl + buy_pl + wing_pl + emer_pl, 2)


# ---------------------------------------------------------------------------
# SL checks
# ---------------------------------------------------------------------------

def check_index_sl(spot: float,
                   ce_sell_strike: int, pe_sell_strike: int) -> bool:
    """
    Check index SL on either side.
    CE: spot >= ce_sell_strike - INDEX_SL_OFFSET (spot approaching CE sell from below)
    PE: spot <= pe_sell_strike + INDEX_SL_OFFSET (spot approaching PE sell from above)
    Returns True if either side is breached.
    """
    if not ENABLE_INDEX_SL:
        return False
    ce_breached = spot >= ce_sell_strike - INDEX_SL_OFFSET
    pe_breached = spot <= pe_sell_strike + INDEX_SL_OFFSET
    return ce_breached or pe_breached


def check_option_sl(ce_sell_ltp: float, ce_sell_entry: float,
                    pe_sell_ltp: float, pe_sell_entry: float) -> bool:
    """
    Check option SL: sell LTP > OPTION_SL_MULTIPLIER * sell_entry on either side.
    Returns True if either side is breached.
    """
    if not ENABLE_OPTION_SL:
        return False
    ce_breached = ce_sell_ltp > OPTION_SL_MULTIPLIER * ce_sell_entry
    pe_breached = pe_sell_ltp > OPTION_SL_MULTIPLIER * pe_sell_entry
    return ce_breached or pe_breached


def check_spread_sl(combined_pl: float) -> bool:
    """
    Check spread SL: exit when combined unrealised P&L <= -SPREAD_SL_POINTS.
    SPREAD_SL_POINTS = None disables this check entirely.
    Returns True if threshold breached.
    """
    if not ENABLE_SPREAD_SL:
        return False
    if SPREAD_SL_POINTS is None:
        return False
    return combined_pl <= -SPREAD_SL_POINTS


def check_trail_stop(combined_pl: float, running_peak_pl: float) -> bool:
    """
    Check trailing stop.
    Trail activates once running_peak_pl >= TRAIL_ACTIVATION_POINTS.
    Fires when combined_pl <= running_peak_pl - TRAIL_POINTS.
    Returns True if trail fires.
    """
    if not ENABLE_TRAIL_STOP:
        return False
    if running_peak_pl < TRAIL_ACTIVATION_POINTS:
        return False
    return combined_pl <= running_peak_pl - TRAIL_POINTS


def check_profit_target(combined_pl: float, total_net_debit: float) -> bool:
    """
    Check profit target: combined unrealised P&L >= PROFIT_TARGET_PCT_NET_DEBIT * total net debit paid.
    Denominator is net debit (capital at risk), not max theoretical profit.
    Returns True if target reached.
    """
    if not ENABLE_PROFIT_TARGET:
        return False
    if total_net_debit <= 0:
        return False
    return combined_pl >= PROFIT_TARGET_PCT_NET_DEBIT * total_net_debit


def determine_breached_side(spot: float, entry_spot: float) -> str:
    """
    Determine which side was breached based on spot vs entry spot.
    Spot above entry → CE side breached.
    Spot below entry → PE side breached.
    Returns 'ce' or 'pe'.
    """
    return 'ce' if spot >= entry_spot else 'pe'


def determine_sl_triggered_side(exit_reason: str, spot: float,
                                 entry_spot: float) -> str:
    """
    Determine which side triggered the SL for SL exits.
    For index_sl and option_sl: CE if spot above entry, PE if below.
    For spread_sl: same spot-vs-entry logic (combined breach, side by direction).
    For profit_target and pre_expiry: returns 'none' — no SL fired.
    """
    if exit_reason in ('index_sl', 'option_sl', 'spread_sl'):
        return determine_breached_side(spot, entry_spot)
    return 'none'


# ---------------------------------------------------------------------------
# Per-trade 1-min snapshot
# ---------------------------------------------------------------------------

def build_snapshot(ts: pd.Timestamp, spot: float, vix: float,
                   ce_sell_strike: int, pe_sell_strike: int,
                   ce_sell_ltp: float, ce_buy_ltp: float,
                   pe_sell_ltp: float, pe_buy_ltp: float,
                   ce_sell_entry: float, ce_buy_entry: float,
                   pe_sell_entry: float, pe_buy_entry: float,
                   total_net_debit: float,
                   max_theoretical_profit: float,
                   running_realised_pl: float = 0.0,
                   ce_wing_ltp: float = 0.0, ce_wing_entry: float = 0.0,
                   pe_wing_ltp: float = 0.0, pe_wing_entry: float = 0.0) -> dict:
    """Build one row for the per-trade 1-min log."""
    ce_unrealised_pl = calc_strategy_pl(ce_sell_entry, ce_sell_ltp, 
                                         ce_buy_entry,  ce_buy_ltp,
                                         ce_wing_entry, ce_wing_ltp)
    pe_unrealised_pl = calc_strategy_pl(pe_sell_entry, pe_sell_ltp, 
                                         pe_buy_entry,  pe_buy_ltp,
                                         pe_wing_entry, pe_wing_ltp)
    combined_unrealised_pl = round(ce_unrealised_pl + pe_unrealised_pl, 2)
    cumulative_pl = round(running_realised_pl + combined_unrealised_pl, 2)

    ce_index_sl = ce_sell_strike - INDEX_SL_OFFSET if ENABLE_INDEX_SL else None
    pe_index_sl = pe_sell_strike + INDEX_SL_OFFSET if ENABLE_INDEX_SL else None
    ce_opt_sl   = round(ce_sell_entry * OPTION_SL_MULTIPLIER, 2) if ENABLE_OPTION_SL else None
    pe_opt_sl   = round(pe_sell_entry * OPTION_SL_MULTIPLIER, 2) if ENABLE_OPTION_SL else None

    return {
        'time_stamp':             ts,
        'spot':                   round(spot, 2),
        'vix':                    round(vix, 2) if vix is not None else None,
        'ce_sell_strike':         ce_sell_strike,
        'pe_sell_strike':         pe_sell_strike,
        'ce_sell_ltp':            round(ce_sell_ltp, 2),
        'ce_buy_ltp':             round(ce_buy_ltp,  2),
        'pe_sell_ltp':            round(pe_sell_ltp, 2),
        'pe_buy_ltp':             round(pe_buy_ltp,  2),
        'ce_wing_ltp':            round(ce_wing_ltp, 2),
        'pe_wing_ltp':            round(pe_wing_ltp, 2),
        'ce_unrealised_pl':       ce_unrealised_pl,
        'pe_unrealised_pl':       pe_unrealised_pl,
        'combined_unrealised_pl': combined_unrealised_pl,
        'running_realised_pl':    round(running_realised_pl, 2),
        'cumulative_pl':          cumulative_pl,
        'ce_index_sl_level':      ce_index_sl,
        'pe_index_sl_level':      pe_index_sl,
        'ce_option_sl_level':     ce_opt_sl,
        'pe_option_sl_level':     pe_opt_sl,
    }


# ---------------------------------------------------------------------------
# 1-min snapshot window scanner
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 1-min snapshot window scanner
# ---------------------------------------------------------------------------

def append_1min_snapshots_window(from_ts: pd.Timestamp, to_ts: pd.Timestamp,
                                  nifty_1m: pd.DataFrame, vix_1m: pd.DataFrame,
                                  ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
                                  ce_sell_strike: int, pe_sell_strike: int,
                                  ce_sell_entry: float, ce_buy_entry: float,
                                  pe_sell_entry: float, pe_buy_entry: float,
                                  total_net_debit: float,
                                  max_theoretical_profit: float,
                                  entry_spot: float,
                                  elm_time: pd.Timestamp,
                                  trade_log: list,
                                  last_ce_sell_ltp: float,
                                  last_ce_buy_ltp:  float,
                                  last_pe_sell_ltp: float,
                                  last_pe_buy_ltp:  float,
                                  running_realised_pl: float = 0.0,
                                  running_peak_pl: float = 0.0,
                                  entry_time: pd.Timestamp = None,
                                  sell_expiry_end: pd.Timestamp = None,
                                  adjustment_already_made: bool = False,
                                  ce_wing_df = None, pe_wing_df = None,
                                  ce_wing_entry: float = 0.0, pe_wing_entry: float = 0.0,
                                  last_ce_wing_ltp: float = 0.0, last_pe_wing_ltp: float = 0.0,
                                  opt_df_cache: dict = None, buy_expiry_end: pd.Timestamp = None):
    """
    Append 1-min snapshots for every minute in (from_ts, to_ts] to trade_log.
    Checks all exit conditions, Emergency Hedge, and structural adjustments.
    """
    running_ce_sell  = last_ce_sell_ltp
    running_ce_buy   = last_ce_buy_ltp
    running_pe_sell  = last_pe_sell_ltp
    running_pe_buy   = last_pe_buy_ltp
    running_ce_wing  = last_ce_wing_ltp
    running_pe_wing  = last_pe_wing_ltp
    
    # Track realized P&L *internal* to this window for the hedge
    window_realised_pl = 0.0

    # Emergency Hedge State
    emer_active = False
    emer_strike = None
    emer_entry  = 0.0
    emer_ltp    = 0.0
    emer_df     = None
    emer_attempts = 0

    sl_hit_ts        = None
    sl_hit_reason    = None
    adj_trigger_ts   = None
    adj_winning_side = None

    window = nifty_1m[
        (nifty_1m.index > from_ts) & (nifty_1m.index <= to_ts)
    ]

    for ts, row in window.iterrows():
        spot = float(row['close'])
        vix  = get_1min_value(vix_1m, ts, 'close')

        # Update LTPs
        v = get_option_price(ce_sell_df, ts, 'close')
        if v is not None: running_ce_sell = v
        v = get_option_price(ce_buy_df,  ts, 'close')
        if v is not None: running_ce_buy  = v
        v = get_option_price(pe_sell_df, ts, 'close')
        if v is not None: running_pe_sell = v
        v = get_option_price(pe_buy_df,  ts, 'close')
        if v is not None: running_pe_buy  = v
        
        if ce_wing_df is not None:
            v = get_option_price(ce_wing_df, ts, 'close')
            if v is not None: running_ce_wing = v
        if pe_wing_df is not None:
            v = get_option_price(pe_wing_df, ts, 'close')
            if v is not None: running_pe_wing = v

        # --- Emergency Hedge Logic ---
        if ENABLE_EMERGENCY_HEDGE and buy_expiry_end is not None and opt_df_cache is not None:
            if not emer_active and emer_attempts < EMERGENCY_MAX_ATTEMPTS:
                if spot >= ce_sell_strike - EMERGENCY_TRIGGER_OFFSET:
                    stk, pr = select_strike(spot, buy_expiry_end, ts, 'ce', opt_df_cache, EMERGENCY_HEDGE_DELTA)
                    if stk:
                        emer_strike = stk
                        emer_entry  = apply_slippage(pr, is_buy=True)
                        emer_ltp    = pr
                        emer_df     = opt_df_cache.get((buy_expiry_end, stk, 'ce'))
                        emer_active = True
                        emer_attempts += 1
                        logger.info(f"  [EMERGENCY] Bought Parachute CE {emer_strike} @ {emer_entry:.1f} at {ts} | spot={spot:.0f}")

            if emer_active:
                v = get_option_price(emer_df, ts, 'close')
                if v is not None: emer_ltp = v
                if spot <= ce_sell_strike + EMERGENCY_EXIT_OFFSET:
                    exit_pr = apply_slippage(emer_ltp, is_buy=False)
                    realised_emer = round(exit_pr - emer_entry, 2)
                    window_realised_pl += realised_emer
                    logger.info(f"  [EMERGENCY] Sold Parachute CE {emer_strike} @ {exit_pr:.1f} at {ts} | P&L: {realised_emer:.1f} | spot={spot:.0f}")
                    emer_active = False
                    emer_entry  = 0.0
                    emer_ltp    = 0.0

        ce_unrealised_pl = calc_strategy_pl(ce_sell_entry, running_ce_sell,
                                             ce_buy_entry,  running_ce_buy,
                                             ce_wing_entry, running_ce_wing,
                                             emer_entry, emer_ltp)
        pe_unrealised_pl = calc_strategy_pl(pe_sell_entry, running_pe_sell,
                                             pe_buy_entry,  running_pe_buy,
                                             pe_wing_entry, running_pe_wing)
        combined_unrealised_pl = round(ce_unrealised_pl + pe_unrealised_pl, 2)
        cumulative_pl = round(running_realised_pl + window_realised_pl + combined_unrealised_pl, 2)

        if cumulative_pl > running_peak_pl:
            running_peak_pl = cumulative_pl

        trade_log.append(build_snapshot(
            ts, spot, vix,
            ce_sell_strike, pe_sell_strike,
            running_ce_sell, running_ce_buy,
            running_pe_sell, running_pe_buy,
            ce_sell_entry, ce_buy_entry,
            pe_sell_entry, pe_buy_entry,
            total_net_debit, max_theoretical_profit,
            running_realised_pl=(running_realised_pl + window_realised_pl),
            ce_wing_ltp=running_ce_wing, ce_wing_entry=ce_wing_entry,
            pe_wing_ltp=running_pe_wing, pe_wing_entry=pe_wing_entry,
        ))

        # Exit checks
        if elm_time is not None and ts >= elm_time:
            sl_hit_ts, sl_hit_reason = ts, 'pre_expiry'
            break
        if check_spread_sl(combined_unrealised_pl):
            sl_hit_ts, sl_hit_reason = ts, 'spread_sl'
            break
        if check_index_sl(spot, ce_sell_strike, pe_sell_strike):
            sl_hit_ts, sl_hit_reason = ts, 'index_sl'
            break
        if check_option_sl(running_ce_sell, ce_sell_entry, running_pe_sell, pe_sell_entry):
            sl_hit_ts, sl_hit_reason = ts, 'option_sl'
            break
        if check_trail_stop(combined_unrealised_pl, running_peak_pl):
            sl_hit_ts, sl_hit_reason = ts, 'trail_stop'
            break
        if check_profit_target(combined_unrealised_pl, total_net_debit):
            sl_hit_ts, sl_hit_reason = ts, 'profit_target'
            break

        # Structural Adjustment Trigger
        if (ENABLE_ADJUSTMENT and not adjustment_already_made
                and entry_time is not None and sell_expiry_end is not None):
            days_in_trade = (ts.date() - entry_time.date()).days
            excluded = ADJUSTMENT_EXCLUDED_DAYS if isinstance(ADJUSTMENT_EXCLUDED_DAYS, (tuple, list, set)) else (ADJUSTMENT_EXCLUDED_DAYS,)
            if days_in_trade not in excluded:
                upside_stress = False
                if spot >= ce_sell_strike - ADJUSTMENT_TRIGGER_OFFSET:
                    upside_stress = True
                if ce_wing_df is not None and (running_ce_wing - ce_wing_entry) >= ADJUSTMENT_WING_THRESHOLD:
                    upside_stress = True
                if upside_stress:
                    adj_trigger_ts, adj_winning_side = ts, 'pe'
                    break

                downside_stress = False
                # Disabled downside roll for now
                if downside_stress:
                    adj_trigger_ts, adj_winning_side = ts, 'ce'
                    break

    if emer_active:
        exit_pr = apply_slippage(emer_ltp, is_buy=False)
        realised_emer = round(exit_pr - emer_entry, 2)
        window_realised_pl += realised_emer
        logger.info(f"  [EMERGENCY] Final Closure Parachute CE {emer_strike} @ {exit_pr:.1f} at {ts} | P&L: {realised_emer:.1f} | spot={spot:.0f}")

    return (running_ce_sell, running_ce_buy, running_pe_sell, running_pe_buy,
            sl_hit_ts, sl_hit_reason, running_peak_pl,
            adj_trigger_ts, adj_winning_side,
            running_ce_wing, running_pe_wing,
            round(window_realised_pl, 2))


def build_trade_record(entry_time, entry_spot, entry_vix,
                        sell_expiry, buy_expiry,
                        ce_sell_strike, pe_sell_strike,
                        ce_sell_entry, ce_buy_entry,
                        pe_sell_entry, pe_buy_entry,
                        ce_sell_delta, pe_sell_delta,
                        net_debit_ce, net_debit_pe,
                        max_theoretical_profit,
                        target_delta_used,
                        # Exit fields
                        exit_time, exit_reason,
                        ce_sell_exit, ce_buy_exit,
                        pe_sell_exit, pe_buy_exit,
                        # Trade duration stats
                        max_spot, min_spot, max_vix, min_vix,
                        max_pl_points, min_pl_points,
                        max_pl_time, min_pl_time,
                        trail_activation_reached,
                        # SL detail columns
                        sl_triggered_side, sl_trigger_time, sl_trigger_spot,
                        sl_trigger_day,
                        untouched_sell_ltp_at_sl, untouched_buy_ltp_at_sl,
                        untouched_net_value_at_sl, days_remaining_at_sl,
                        # Adjustment fields
                        adjustment_made=False,
                        adj_side=None,
                        adj_ce_sell_strike=None,
                        adj_pe_sell_strike=None,
                        adj_ce_new_sell_entry=None,   # new sell leg proceeds
                        adj_ce_sell_buyback=None,     # cost to close old sell leg
                        adj_pe_new_sell_entry=None,
                        adj_pe_sell_buyback=None,
                        adj_ce_new_sell_exit=None,
                        adj_ce_new_sell_exit_raw=None,
                        adj_pe_new_sell_exit=None,
                        adj_pe_new_sell_exit_raw=None,
                        adj_ce_buy_buyback=None,      # proceeds from closing old buy leg
                        adj_pe_buy_buyback=None,
                        adj_ce_new_buy_entry=None,    # cost of new buy leg
                        adj_pe_new_buy_entry=None,
                        adj_ce_new_buy_exit=None,
                        adj_pe_new_buy_exit=None,
                        adj_exit_reason=None,
                        adj_pl_points=None,
                        adj_entry_spot=None,
                        adj_days_remaining=None,
                        adj_trigger_day=None,
                        # Safety Wing fields
                        ce_wing_strike=None, pe_wing_strike=None,
                        ce_wing_entry=0.0, pe_wing_entry=0.0,
                        ce_wing_exit=0.0, pe_wing_exit=0.0,
                        realised_pl=0.0) -> dict:
    """Build a complete trade summary record."""
    ce_pl = _calc_exit_pl(ce_sell_entry, ce_sell_exit, ce_buy_entry, ce_buy_exit)
    pe_pl = _calc_exit_pl(pe_sell_entry, pe_sell_exit, pe_buy_entry, pe_buy_exit)
    wing_pl = (ce_wing_exit - ce_wing_entry) + (pe_wing_exit - pe_wing_entry)
    base_pl   = round(ce_pl + pe_pl + wing_pl, 2)
    adj_pl    = round(adj_pl_points, 2) if adj_pl_points is not None else 0.0
    
    # realised_pl includes locked-in gains from Emergency Hedge and previous adjustments
    total_pl  = round(base_pl + adj_pl + realised_pl, 2)
    total_rs  = round(total_pl * LOT_SIZE, 2)

    return {
        'entry_time':                  entry_time,
        'entry_spot':                  entry_spot,
        'entry_vix':                   round(entry_vix, 2) if entry_vix is not None else None,
        'sell_expiry':                 sell_expiry,
        'buy_expiry':                  buy_expiry,
        'ce_sell_strike':              ce_sell_strike,
        'pe_sell_strike':              pe_sell_strike,
        'ce_wing_strike':              ce_wing_strike,
        'pe_wing_strike':              pe_wing_strike,
        'ce_sell_entry':               round(ce_sell_entry, 2),
        'ce_buy_entry':                round(ce_buy_entry,  2),
        'pe_sell_entry':               round(pe_sell_entry, 2),
        'pe_buy_entry':                round(pe_buy_entry,  2),
        'ce_wing_entry':               round(ce_wing_entry, 2),
        'pe_wing_entry':               round(pe_wing_entry, 2),
        'ce_sell_delta':               round(ce_sell_delta, 4) if ce_sell_delta else None,
        'pe_sell_delta':               round(pe_sell_delta, 4) if pe_sell_delta else None,
        'target_delta_used':           round(target_delta_used, 4),
        'net_debit_ce':                round(net_debit_ce, 2),
        'net_debit_pe':                round(net_debit_pe, 2),
        'max_theoretical_profit':      round(max_theoretical_profit, 2),
        'exit_time':                   exit_time,
        'exit_reason':                 exit_reason,
        'ce_sell_exit':                round(ce_sell_exit, 2) if ce_sell_exit else None,
        'ce_buy_exit':                 round(ce_buy_exit,  2) if ce_buy_exit  else None,
        'pe_sell_exit':                round(pe_sell_exit, 2) if pe_sell_exit is not None else None,
        'pe_buy_exit':                 round(pe_buy_exit, 2) if pe_buy_exit is not None else None,
        'ce_wing_exit':                round(ce_wing_exit, 2),
        'pe_wing_exit':                round(pe_wing_exit, 2),

        'ce_pl_points':                round(ce_pl, 2),
        'pe_pl_points':                round(pe_pl, 2),
        'max_spot':                    max_spot,
        'min_spot':                    min_spot,
        'max_vix':                     max_vix,
        'min_vix':                     min_vix,
        'max_pl_points':               max_pl_points,
        'min_pl_points':               min_pl_points,
        'max_pl_time':                 max_pl_time,
        'min_pl_time':                 min_pl_time,
        'trail_activation_reached':    trail_activation_reached,
        'sl_triggered_side':           sl_triggered_side,
        'sl_trigger_time':             sl_trigger_time,
        'sl_trigger_spot':             round(sl_trigger_spot, 2) if sl_trigger_spot is not None else None,
        'sl_trigger_day':              sl_trigger_day,
        'untouched_sell_ltp_at_sl':    round(untouched_sell_ltp_at_sl, 2) if untouched_sell_ltp_at_sl is not None else None,
        'untouched_buy_ltp_at_sl':     round(untouched_buy_ltp_at_sl,  2) if untouched_buy_ltp_at_sl  is not None else None,
        'untouched_net_value_at_sl':   round(untouched_net_value_at_sl, 2) if untouched_net_value_at_sl is not None else None,
        'days_remaining_at_sl':        days_remaining_at_sl,
        'adjustment_made':             adjustment_made,
        'adj_side':                    adj_side,
        'adj_ce_sell_strike':          adj_ce_sell_strike,
        'adj_pe_sell_strike':          adj_pe_sell_strike,
        'adj_ce_new_sell_entry':       round(adj_ce_new_sell_entry, 2) if adj_ce_new_sell_entry is not None else None,
        'adj_ce_sell_buyback':         round(adj_ce_sell_buyback,   2) if adj_ce_sell_buyback   is not None else None,
        'adj_pe_new_sell_entry':       round(adj_pe_new_sell_entry, 2) if adj_pe_new_sell_entry is not None else None,
        'adj_pe_sell_buyback':         round(adj_pe_sell_buyback,   2) if adj_pe_sell_buyback   is not None else None,
        'adj_ce_new_sell_exit':        round(adj_ce_new_sell_exit,  2) if adj_ce_new_sell_exit  is not None else None,
        'adj_pe_new_sell_exit':        round(adj_pe_new_sell_exit,  2) if adj_pe_new_sell_exit  is not None else None,
        'adj_ce_buy_buyback':          round(adj_ce_buy_buyback,    2) if adj_ce_buy_buyback    is not None else None,
        'adj_pe_buy_buyback':          round(adj_pe_buy_buyback,    2) if adj_pe_buy_buyback    is not None else None,
        'adj_ce_new_buy_entry':        round(adj_ce_new_buy_entry,  2) if adj_ce_new_buy_entry  is not None else None,
        'adj_pe_new_buy_entry':        round(adj_pe_new_buy_entry,  2) if adj_pe_new_buy_entry  is not None else None,
        'adj_ce_new_buy_exit':         round(adj_ce_new_buy_exit,   2) if adj_ce_new_buy_exit   is not None else None,
        'adj_pe_new_buy_exit':         round(adj_pe_new_buy_exit,   2) if adj_pe_new_buy_exit   is not None else None,
        'adj_exit_reason':             adj_exit_reason,
        'adj_pl_points':               round(adj_pl_points, 2) if adj_pl_points is not None else None,
        'adj_entry_spot':              round(adj_entry_spot, 2) if adj_entry_spot is not None else None,
        'adj_days_remaining':          adj_days_remaining,
        'adj_trigger_day':             adj_trigger_day,
        'total_pl_points':             total_pl,
        'total_pl_rupees':             total_rs,
    }


def _calc_exit_pl(sell_entry, sell_exit, buy_entry, buy_exit) -> float:
    """P&L for one side of calendar: (sell_entry - sell_exit) + (buy_exit - buy_entry)."""
    if any(v is None for v in [sell_entry, sell_exit, buy_entry, buy_exit]):
        return 0.0
    return round((sell_entry - sell_exit) + (buy_exit - buy_entry), 2)


# ---------------------------------------------------------------------------
# Save helpers
# ---------------------------------------------------------------------------

def save_trade_log(trade_counter: int, entry_time: pd.Timestamp, trade_log: list):
    """Save per-trade 1-min log. Filename: trade_NNNN_YYYY-MM-DD.csv"""
    if not trade_log:
        return
    entry_str = pd.Timestamp(entry_time).strftime('%Y-%m-%d')
    filename  = f"trade_{trade_counter:04d}_{entry_str}.csv"
    filepath  = os.path.join(TRADE_LOGS_DIR, filename)
    pd.DataFrame(trade_log).to_csv(filepath, index=False)
    logger.debug(f"  Trade log saved: {filename} ({len(trade_log)} rows)")


def save_trade_summary(all_trades: list):
    """Save consolidated trade summary and print statistics."""
    if not all_trades:
        logger.info("No trades generated.")
        return

    df = pd.DataFrame(all_trades)
    df.to_csv(TRADE_SUMMARY_FILE, index=False)

    total    = len(df)
    winners  = df[df['total_pl_points'] > 0]
    losers   = df[df['total_pl_points'] <= 0]
    win_rate = len(winners) / total * 100 if total > 0 else 0
    avg_win  = winners['total_pl_points'].mean() if len(winners) else 0
    avg_loss = losers['total_pl_points'].mean()  if len(losers)  else 0
    total_pl = df['total_pl_rupees'].sum()

    streak = max_streak = 0
    for pl in df['total_pl_points']:
        if pl <= 0:
            streak    += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    adj_count = df['adjustment_made'].sum() if 'adjustment_made' in df.columns else 0

    logger.info("=" * 60)
    logger.info("ATHENA BACKTEST SUMMARY")
    logger.info("=" * 60)
    logger.info(f"  Total trades       : {total}")
    logger.info(f"  Winners            : {len(winners)} ({win_rate:.1f}%)")
    logger.info(f"  Losers             : {len(losers)}")
    logger.info(f"  Avg winner (pts)   : {avg_win:.2f}")
    logger.info(f"  Avg loser  (pts)   : {avg_loss:.2f}")
    if avg_loss != 0:
        logger.info(f"  Reward:Risk        : {abs(avg_win / avg_loss):.2f}")
    logger.info(f"  Max consec losses  : {max_streak}")
    logger.info(f"  Total P&L (₹)      : {total_pl:+,.0f}")
    logger.info(f"  Adjustments made   : {adj_count}")
    logger.info(f"  Exit breakdown:")
    for reason, count in df['exit_reason'].value_counts().items():
        logger.info(f"    {reason:25s}: {count}")
    logger.info("=" * 60)
    logger.info(f"  Saved to: {TRADE_SUMMARY_FILE}")


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------

def run_backtest(nifty_1m: pd.DataFrame, vix_1m: pd.DataFrame,
                 contracts_df: pd.DataFrame,
                 holidays_df: pd.DataFrame = None):
    """
    Main backtest loop.
    """
    os.makedirs(TRADE_LOGS_DIR, exist_ok=True)

    logger.info("Resampling 75m data for Supertrend regime...")
    nifty_75m = nifty_1m.resample('75min').agg({
        'open':  'first',
        'high':  'max',
        'low':   'min',
        'close': 'last'
    }).dropna()
    nifty_75m.columns = ['Open', 'High', 'Low', 'Close']
    st_indicator = SupertrendIndicator(period=10, multiplier=3.0)
    nifty_75m = st_indicator.calculate(nifty_75m)
    # Identify trend: True if close > ST (bullish), False if close < ST (bearish)
    nifty_75m['is_bullish'] = nifty_75m['Close'] > nifty_75m['Supertrend']

    all_trades    = []
    opt_df_cache  = {}
    trade_counter = 0

    # Build holiday set for fast lookup
    holidays_set = set()
    if holidays_df is not None:
        holidays_set = set(holidays_df['date'].values)

    # Collect all sell expiry dates from contract list, filtered to backtest scope
    all_expiry_dates = sorted(contracts_df['expiry_date'].dt.date.unique())
    if BACKTEST_START_DATE:
        start = pd.Timestamp(BACKTEST_START_DATE).date()
        all_expiry_dates = [d for d in all_expiry_dates if d >= start]
    if BACKTEST_END_DATE:
        end = pd.Timestamp(BACKTEST_END_DATE).date()
        all_expiry_dates = [d for d in all_expiry_dates if d <= end]

    logger.info(f"Sell expiries in scope: {len(all_expiry_dates)}")

    entry_ts_str = f" {ENTRY_TIME}:00"

    # Skip reason counters — logged at end to show where entries are failing
    skip_counts = {
        'no_entry_day':   0,
        'no_spot':        0,
        'vix_filtered':   0,
        'no_buy_expiry':  0,
        'expiry_not_in_contracts': 0,
        'strike_failed':  0,
        'missing_price':  0,
    }

    for expiry_idx, sell_expiry_date in enumerate(all_expiry_dates, 1):

        if expiry_idx % 50 == 0 or expiry_idx == len(all_expiry_dates):
            logger.info(f"  Progress: {expiry_idx}/{len(all_expiry_dates)} expiries | "
                        f"Trades so far: {trade_counter}")

        # ----------------------------------------------------------------
        # Compute entry date: trading day before the prior expiry
        # Entry day = day before the expiry immediately preceding sell_expiry
        # e.g. sell=14 Aug, prior_expiry=7 Aug, entry=6 Aug
        # ----------------------------------------------------------------
        prior_expiry_date = get_prior_expiry(sell_expiry_date, contracts_df)
        if prior_expiry_date is None:
            skip_counts['no_entry_day'] += 1
            logger.debug(f"  {sell_expiry_date}: No prior expiry in contract list — skipping")
            continue

        entry_date = compute_entry_date(prior_expiry_date, holidays_set)
        if entry_date is None:
            skip_counts['no_entry_day'] += 1
            logger.debug(f"  {sell_expiry_date}: Cannot find trading day before prior expiry {prior_expiry_date} — skipping")
            continue

        entry_ts = pd.Timestamp(f"{entry_date}{entry_ts_str}")

        # Check data availability at entry time
        spot = get_1min_value(nifty_1m, entry_ts, 'close')
        if spot is None:
            skip_counts['no_spot'] += 1
            logger.debug(f"  {sell_expiry_date}: No spot data at {entry_date} {ENTRY_TIME} — skipping")
            continue

        entry_vix = get_1min_value(vix_1m, entry_ts, 'close')

        # VIX filter
        if ENABLE_VIX_FILTER:
            if entry_vix is None or not (VIX_FILTER_LOW <= entry_vix <= VIX_FILTER_HIGH):
                skip_counts['vix_filtered'] += 1
                logger.debug(f"  {sell_expiry_date}: VIX {entry_vix} outside "
                             f"[{VIX_FILTER_LOW}, {VIX_FILTER_HIGH}] — skipping")
                continue

        # ----------------------------------------------------------------
        # Expiry selection
        # ----------------------------------------------------------------
        sell_expiry_end = get_end_date(sell_expiry_date, contracts_df)
        elm_time        = get_elm_time(sell_expiry_date, contracts_df)

        buy_expiry_date = select_buy_expiry(entry_date, sell_expiry_date, contracts_df)
        if buy_expiry_date is None:
            skip_counts['no_buy_expiry'] += 1
            logger.debug(f"  {sell_expiry_date}: No valid buy expiry found — skipping")
            continue

        buy_expiry_end = get_end_date(buy_expiry_date, contracts_df)

        if sell_expiry_end is None or buy_expiry_end is None:
            skip_counts['expiry_not_in_contracts'] += 1
            logger.info(f"  {sell_expiry_date}: end_date lookup failed — "
                        f"sell_end={sell_expiry_end} buy_end={buy_expiry_end} — skipping")
            continue

        # ----------------------------------------------------------------
        # Strike selection — CE and PE simultaneously
        # ----------------------------------------------------------------
        target_delta_used = get_target_delta(entry_vix) if entry_vix is not None \
            else VIX_DELTA_BANDS[-1][1]

        ce_delta_target = target_delta_used
        pe_delta_target = target_delta_used

        if ENABLE_ASYMMETRIC_DELTA:
            is_bullish = get_supertrend_regime(nifty_75m, entry_ts)
            if is_bullish:
                # Market is moving UP: CE is the tested side (safer = lower delta)
                # PE is the safe side (aggressive = higher delta)
                ce_delta_target = DELTA_TESTED_SIDE
                pe_delta_target = DELTA_SAFE_SIDE
            else:
                # Market is moving DOWN: PE is the tested side
                ce_delta_target = DELTA_SAFE_SIDE
                pe_delta_target = DELTA_TESTED_SIDE

        pe_sell_strike, pe_sell_raw = select_strike(
            spot, sell_expiry_end, entry_ts, 'pe', opt_df_cache, pe_delta_target)

        ce_sell_strike, ce_sell_raw = select_strike(
            spot, sell_expiry_end, entry_ts, 'ce', opt_df_cache, ce_delta_target)

        if ce_sell_strike is None or pe_sell_strike is None:
            skip_counts['strike_failed'] += 1
            logger.info(f"  {sell_expiry_date}: Strike selection failed — "
                        f"entry={entry_date} spot={spot:.0f} "
                        f"targets=[{ce_delta_target}, {pe_delta_target}] "
                        f"strikes=[{ce_sell_strike}, {pe_sell_strike}] — skipping")
            continue

        # --- Phase 2 Adjustment: Matching Strikes ---
        # We go back to the symmetric setup where long and short strikes match.
        ce_buy_strike = ce_sell_strike
        pe_buy_strike = pe_sell_strike

        ce_wing_strike = None
        pe_wing_strike = None
        if ENABLE_SAFETY_WINGS:
            # --- Phase 2 Adjustment: PE-Only Wing ---
            # We skip the CE wing to save cost.
            ce_wing_strike = None
            ce_wing_raw = 0.0
            
            pe_wing_strike, pe_wing_raw = select_strike(
                spot, buy_expiry_end, entry_ts, 'pe', opt_df_cache, SAFETY_WING_DELTA)

        # ----------------------------------------------------------------
        # Load all option files (4 for base + 2 for wings)
        # ----------------------------------------------------------------
        _file_debug = f"trade-{sell_expiry_date}" if str(entry_date) == '2025-01-29' else None

        ce_sell_df = opt_df_cache.get(
            (sell_expiry_end, ce_sell_strike, 'ce'),
            load_option_data(sell_expiry_end, ce_sell_strike, 'ce',
                             f"{_file_debug} CE-sell" if _file_debug else None))
        pe_sell_df = opt_df_cache.get(
            (sell_expiry_end, pe_sell_strike, 'pe'),
            load_option_data(sell_expiry_end, pe_sell_strike, 'pe',
                             f"{_file_debug} PE-sell" if _file_debug else None))

        ce_buy_df_key = (buy_expiry_end, ce_buy_strike, 'ce')
        pe_buy_df_key = (buy_expiry_end, pe_buy_strike, 'pe')
        if ce_buy_df_key not in opt_df_cache:
            opt_df_cache[ce_buy_df_key] = load_option_data(
                buy_expiry_end, ce_buy_strike, 'ce',
                f"{_file_debug} CE-buy" if _file_debug else None)
        if pe_buy_df_key not in opt_df_cache:
            opt_df_cache[pe_buy_df_key] = load_option_data(
                buy_expiry_end, pe_buy_strike, 'pe',
                f"{_file_debug} PE-buy" if _file_debug else None)
        ce_buy_df = opt_df_cache[ce_buy_df_key]
        pe_buy_df = opt_df_cache[pe_buy_df_key]
        
        ce_wing_df = None
        pe_wing_df = None
        if ENABLE_SAFETY_WINGS and ce_wing_strike and pe_wing_strike:
            ce_wing_key = (buy_expiry_end, ce_wing_strike, 'ce')
            pe_wing_key = (buy_expiry_end, pe_wing_strike, 'pe')
            if ce_wing_key not in opt_df_cache:
                opt_df_cache[ce_wing_key] = load_option_data(
                    buy_expiry_end, ce_wing_strike, 'ce',
                    f"{_file_debug} CE-wing" if _file_debug else None)
            if pe_wing_key not in opt_df_cache:
                opt_df_cache[pe_wing_key] = load_option_data(
                    buy_expiry_end, pe_wing_strike, 'pe',
                    f"{_file_debug} PE-wing" if _file_debug else None)
            ce_wing_df = opt_df_cache[ce_wing_key]
            pe_wing_df = opt_df_cache[pe_wing_key]

        if _file_debug:
            # Also log whether ce_sell_df came from cache (may have been loaded by earlier trade)
            was_cached = (sell_expiry_end, ce_sell_strike, 'ce') in opt_df_cache
            logger.info(f"  [FILE-DEBUG] {_file_debug} CE-sell from_cache={was_cached} "
                        f"expiry_end={sell_expiry_end} strike={ce_sell_strike} "
                        f"rows={len(ce_sell_df)}")

        # ----------------------------------------------------------------
        # Entry pricing (raw then slippage-adjusted)
        # ----------------------------------------------------------------
        ce_buy_raw = get_option_price(ce_buy_df, entry_ts, 'open')
        pe_buy_raw = get_option_price(pe_buy_df, entry_ts, 'open')

        if any(v is None for v in [ce_sell_raw, ce_buy_raw, pe_sell_raw, pe_buy_raw]):
            skip_counts['missing_price'] += 1
            logger.info(f"  {entry_date}: Missing option price — "
                        f"ce_sell={ce_sell_raw} ce_buy={ce_buy_raw} "
                        f"pe_sell={pe_sell_raw} pe_buy={pe_buy_raw} "
                        f"sell_exp={sell_expiry_date} buy_exp={buy_expiry_date} "
                        f"ce_strike={ce_sell_strike} pe_strike={pe_sell_strike} — skipping")
            continue

        # Sell legs: we sell → apply_slippage(is_buy=False)
        # Buy legs:  we buy  → apply_slippage(is_buy=True)
        ce_sell_entry = apply_slippage(ce_sell_raw, is_buy=False)
        ce_buy_entry  = apply_slippage(ce_buy_raw,  is_buy=True)
        pe_sell_entry = apply_slippage(pe_sell_raw, is_buy=False)
        pe_buy_entry  = apply_slippage(pe_buy_raw,  is_buy=True)
        
        ce_wing_entry = 0.0
        pe_wing_entry = 0.0
        if ENABLE_SAFETY_WINGS and ce_wing_df is not None and pe_wing_df is not None:
            ce_wing_raw = get_option_price(ce_wing_df, entry_ts, 'open')
            pe_wing_raw = get_option_price(pe_wing_df, entry_ts, 'open')
            if ce_wing_raw is not None and pe_wing_raw is not None:
                ce_wing_entry = apply_slippage(ce_wing_raw, is_buy=True)
                pe_wing_entry = apply_slippage(pe_wing_raw, is_buy=True)

        # Net debit per side = what you pay (buy leg cost − sell leg premium received)
        # Always positive for a calendar — far-term option is worth more than near-term
        net_debit_ce = round(ce_buy_entry - ce_sell_entry + ce_wing_entry, 2)
        net_debit_pe = round(pe_buy_entry - pe_sell_entry + pe_wing_entry, 2)
        total_net_debit = round(net_debit_ce + net_debit_pe, 2)

        # ----------------------------------------------------------------
        # Delta and max theoretical profit at entry
        # ----------------------------------------------------------------
        sell_dte    = max((sell_expiry_date - entry_date).days, 0.5)
        ce_sell_delta = compute_delta(spot, ce_sell_strike, sell_dte,
                                      ce_sell_raw, 'ce')
        pe_sell_delta = compute_delta(spot, pe_sell_strike, sell_dte,
                                      pe_sell_raw, 'pe')

        # Wings are not included in max theoretical profit (as they are far OTM insurance)
        # to keep the 'potential yield' figure realistic.
        max_theoretical_profit = compute_max_theoretical_profit(
            spot, ce_sell_strike, pe_sell_strike,
            sell_expiry_end, buy_expiry_end, entry_ts,
            ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
            ce_sell_entry, pe_sell_entry,
            ce_buy_entry,  pe_buy_entry)

        wing_str = f" | CE wing {ce_wing_strike} @ {ce_wing_entry:.1f} | PE wing {pe_wing_strike} @ {pe_wing_entry:.1f}" if ENABLE_SAFETY_WINGS else ""
        logger.info(
            f"  ENTRY {entry_date} | Spot: {spot:.0f} | "
            f"CE sell {ce_sell_strike} @ {ce_sell_entry:.1f} | "
            f"PE sell {pe_sell_strike} @ {pe_sell_entry:.1f}"
            f"{wing_str} | "
            f"Net debit: {total_net_debit:.1f} | "
            f"Sell exp: {sell_expiry_date} | Buy exp: {buy_expiry_date}"
        )

        # ----------------------------------------------------------------
        # Scan 1-min candles from entry to elm_time / sell expiry
        # ----------------------------------------------------------------
        # Scanning window: entry_ts to sell expiry end (15:30)
        scan_start = entry_ts
        scan_end   = sell_expiry_end

        trade_log = []
        ce_sell_ltp = ce_sell_entry
        ce_buy_ltp  = ce_buy_entry
        pe_sell_ltp = pe_sell_entry
        pe_buy_ltp  = pe_buy_entry
        ce_wing_ltp = ce_wing_entry
        pe_wing_ltp = pe_wing_entry
        running_realised_pl = 0.0
        running_peak_pl = 0.0

        (ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
         sl_ts, sl_reason, running_peak_pl,
         adj_trigger_ts, adj_winning_side,
         ce_wing_ltp, pe_wing_ltp,
         window_realised_emer_pl) = append_1min_snapshots_window(
            scan_start, scan_end,
            nifty_1m, vix_1m,
            ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
            ce_sell_strike, pe_sell_strike,
            ce_sell_entry, ce_buy_entry,
            pe_sell_entry, pe_buy_entry,
            total_net_debit, max_theoretical_profit,
            spot,
            elm_time, trade_log,
            ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
            running_realised_pl=running_realised_pl,
            running_peak_pl=running_peak_pl,
            entry_time=entry_ts,
            sell_expiry_end=sell_expiry_end,
            adjustment_already_made=False,
            ce_wing_df=ce_wing_df, pe_wing_df=pe_wing_df,
            ce_wing_entry=ce_wing_entry, pe_wing_entry=pe_wing_entry,
            last_ce_wing_ltp=ce_wing_ltp, last_pe_wing_ltp=pe_wing_ltp,
            opt_df_cache=opt_df_cache, buy_expiry_end=buy_expiry_end)

        running_realised_pl += window_realised_emer_pl

        # ----------------------------------------------------------------
        # Initialise adjustment state — must be before exit pricing
        # ----------------------------------------------------------------
        adj_made                = False
        adj_side                = None
        adj_ce_sell_strike      = None
        adj_pe_sell_strike      = None
        adj_ce_new_sell_entry   = None   # new sell leg proceeds
        adj_ce_sell_buyback     = None   # cost to close old sell leg
        adj_pe_new_sell_entry   = None
        adj_pe_sell_buyback     = None
        adj_ce_new_sell_exit    = None
        adj_pe_new_sell_exit    = None
        adj_ce_buy_buyback      = None   # proceeds from closing old buy leg
        adj_pe_buy_buyback      = None
        adj_ce_new_buy_entry    = None   # cost of new buy leg
        adj_pe_new_buy_entry    = None
        adj_ce_new_buy_exit     = None
        adj_pe_new_buy_exit     = None
        adj_exit_reason         = None
        adj_pl_points           = None
        adj_entry_spot_val      = None
        adj_days_remaining_val  = None
        adj_trigger_day_val     = None
        # Originals preserved before roll mutates them (used for correct P&L accounting)
        orig_ce_sell_entry  = ce_sell_entry
        orig_pe_sell_entry  = pe_sell_entry
        orig_ce_buy_entry   = ce_buy_entry
        orig_pe_buy_entry   = pe_buy_entry
        orig_ce_sell_strike = ce_sell_strike
        orig_pe_sell_strike = pe_sell_strike

        # ----------------------------------------------------------------
        # Exit the base position
        # ----------------------------------------------------------------
        if sl_ts is None:
            # No SL fired — position held to sell expiry (pre-expiry is
            # handled inside the window scanner; if we reach here without
            # sl_ts it means the elm_time or expiry was the last candle)
            sl_ts     = scan_end
            sl_reason = 'pre_expiry'

        # Determine exit timestamp and pricing column
        if sl_reason == 'pre_expiry':
            exit_ts  = elm_time if elm_time is not None else scan_end
            use_col  = 'close'
            slip     = False   # no slippage on pre-expiry
        else:
            exit_ts  = sl_ts + pd.Timedelta(minutes=1)
            use_col  = 'open'
            slip     = True

        def get_exit_price(opt_df, ltp_fallback, is_buy):
            raw = get_option_price(opt_df, exit_ts, use_col) or ltp_fallback
            if slip:
                return apply_slippage(raw, is_buy=is_buy), raw
            return raw, raw

        ce_sell_exit, ce_sell_exit_raw = get_exit_price(ce_sell_df, ce_sell_ltp, is_buy=True)
        ce_buy_exit,  ce_buy_exit_raw  = get_exit_price(ce_buy_df,  ce_buy_ltp,  is_buy=False)
        pe_sell_exit, pe_sell_exit_raw = get_exit_price(pe_sell_df, pe_sell_ltp, is_buy=True)
        pe_buy_exit,  pe_buy_exit_raw  = get_exit_price(pe_buy_df,  pe_buy_ltp,  is_buy=False)
        
        ce_wing_exit = ce_wing_ltp
        pe_wing_exit = pe_wing_ltp
        if ENABLE_SAFETY_WINGS and ce_wing_df is not None and pe_wing_df is not None:
            ce_wing_exit, _ = get_exit_price(ce_wing_df, ce_wing_ltp, is_buy=False)
            pe_wing_exit, _ = get_exit_price(pe_wing_df, pe_wing_ltp, is_buy=False)

        ce_pl_base = _calc_exit_pl(ce_sell_entry, ce_sell_exit,
                                    ce_buy_entry,  ce_buy_exit)
        pe_pl_base = _calc_exit_pl(pe_sell_entry, pe_sell_exit,
                                    pe_buy_entry,  pe_buy_exit)
        
        # Add wing P&L to base (simple buy/sell exit for wings)
        wing_pl_total = (ce_wing_exit - ce_wing_entry) + (pe_wing_exit - pe_wing_entry)
        base_pl = round(ce_pl_base + pe_pl_base + wing_pl_total, 2)

        # NOTE: exit snapshot is appended after the adjustment block below,
        # so it always reflects the final position (post-roll if adjustment fired)

        # ----------------------------------------------------------------
        # Winning side roll adjustment
        # adj_trigger_ts is set if the scanner detected a roll trigger
        # ----------------------------------------------------------------
        if adj_trigger_ts is not None and adj_winning_side is not None:

            # Roll execution timestamp: open of next 1-min candle
            roll_ts   = adj_trigger_ts + pd.Timedelta(minutes=1)
            roll_spot = get_1min_value(nifty_1m, roll_ts, 'close') or spot

            win  = adj_winning_side   # side being rolled ('ce' or 'pe')
            lose = 'pe' if win == 'ce' else 'ce'  # untouched side

            # Step 1: compute new sell strike — step existing sell strike toward spot
            # CE roll (Trigger B): new CE sell = ce_sell_strike - distance (moves down toward spot)
            # PE roll (Trigger A): new PE sell = pe_sell_strike + distance (moves up toward spot)
            # OTM check: new CE must be > roll_spot; new PE must be < roll_spot
            if win == 'ce':
                new_sell_strike = ce_sell_strike - ADJUSTMENT_NEW_STRIKE_DISTANCE
                otm_ok = new_sell_strike > roll_spot
            else:
                new_sell_strike = pe_sell_strike + ADJUSTMENT_NEW_STRIKE_DISTANCE
                otm_ok = new_sell_strike < roll_spot

            if not otm_ok:
                logger.info(
                    f"  ADJ SKIP | New {win.upper()} sell strike {new_sell_strike} "
                    f"is not OTM at spot {roll_spot:.0f} — roll aborted")
                (ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
                 sl_ts, sl_reason, running_peak_pl, _, _,
                 ce_wing_ltp, pe_wing_ltp,
                 window_realised_emer_pl) = \
                    append_1min_snapshots_window(
                        roll_ts - pd.Timedelta(minutes=1), scan_end,
                        nifty_1m, vix_1m,
                        ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
                        ce_sell_strike, pe_sell_strike,
                        ce_sell_entry, ce_buy_entry,
                        pe_sell_entry, pe_buy_entry,
                        total_net_debit, max_theoretical_profit,
                        spot, elm_time, trade_log,
                        ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
                        running_realised_pl=running_realised_pl,
                        running_peak_pl=running_peak_pl,
                        entry_time=entry_ts,
                        sell_expiry_end=sell_expiry_end,
                        adjustment_already_made=True,
                        ce_wing_df=ce_wing_df, pe_wing_df=pe_wing_df,
                        ce_wing_entry=ce_wing_entry, pe_wing_entry=pe_wing_entry,
                        last_ce_wing_ltp=ce_wing_ltp, last_pe_wing_ltp=pe_wing_ltp,
                        opt_df_cache=opt_df_cache, buy_expiry_end=buy_expiry_end)

                running_realised_pl += window_realised_emer_pl
                if sl_ts is None:
                    sl_ts = scan_end; sl_reason = 'pre_expiry'
                if sl_reason == 'pre_expiry':
                    exit_ts = elm_time if elm_time is not None else scan_end
                    use_col = 'close'; slip = False
                else:
                    exit_ts = sl_ts + pd.Timedelta(minutes=1)
                    use_col = 'open'; slip = True
                ce_sell_exit, ce_sell_exit_raw = get_exit_price(ce_sell_df, ce_sell_ltp, is_buy=True)
                ce_buy_exit,  ce_buy_exit_raw  = get_exit_price(ce_buy_df,  ce_buy_ltp,  is_buy=False)
                pe_sell_exit, pe_sell_exit_raw = get_exit_price(pe_sell_df, pe_sell_ltp, is_buy=True)
                pe_buy_exit,  pe_buy_exit_raw  = get_exit_price(pe_buy_df,  pe_buy_ltp,  is_buy=False)
                ce_pl_base = _calc_exit_pl(ce_sell_entry, ce_sell_exit, ce_buy_entry, ce_buy_exit)
                pe_pl_base = _calc_exit_pl(pe_sell_entry, pe_sell_exit, pe_buy_entry, pe_buy_exit)
                base_pl = round(ce_pl_base + pe_pl_base, 2)

            if otm_ok:
                # Step 2: buy back old sell leg (cost to close)
                old_sell_df  = ce_sell_df  if win == 'ce' else pe_sell_df
                old_sell_ltp = ce_sell_ltp if win == 'ce' else pe_sell_ltp
                buyback_raw   = get_option_price(old_sell_df, roll_ts, 'open') or old_sell_ltp
                buyback_price = apply_slippage(buyback_raw, is_buy=True)

                # Step 3: sell new option at new_sell_strike
                new_sell_key = (sell_expiry_end, new_sell_strike, win)
                if new_sell_key not in opt_df_cache:
                    opt_df_cache[new_sell_key] = load_option_data(
                        sell_expiry_end, new_sell_strike, win)
                new_sell_df  = opt_df_cache[new_sell_key]
                new_sell_raw = get_option_price(new_sell_df, roll_ts, 'open')

                if new_sell_raw is not None:
                    new_sell_entry_temp = apply_slippage(new_sell_raw, is_buy=False)
                    credit_gain = round(new_sell_entry_temp - buyback_price, 2)
                    if credit_gain < ADJUSTMENT_MIN_CREDIT_GAIN:
                        logger.info(
                            f"  ADJ SKIP | Credit gain {credit_gain:.1f} < {ADJUSTMENT_MIN_CREDIT_GAIN} "
                            f"— roll not worth the risk at {roll_ts}")
                        new_sell_raw = None # Force skip logic below

                if new_sell_raw is None:
                    logger.info(
                        f"  ADJ SKIP | No data for new sell strike {new_sell_strike}{win} "
                        f"at {roll_ts} — roll aborted")
                    # scanner already stopped — re-run remainder with adjustment disabled
                    (ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
                     sl_ts, sl_reason, running_peak_pl, _, _,
                     ce_wing_ltp, pe_wing_ltp,
                     window_realised_emer_pl) = \
                        append_1min_snapshots_window(
                            roll_ts - pd.Timedelta(minutes=1), scan_end,
                            nifty_1m, vix_1m,
                            ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
                            ce_sell_strike, pe_sell_strike,
                            ce_sell_entry, ce_buy_entry,
                            pe_sell_entry, pe_buy_entry,
                            total_net_debit, max_theoretical_profit,
                            spot, elm_time, trade_log,
                            ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
                            running_realised_pl=running_realised_pl,
                            running_peak_pl=running_peak_pl,
                            entry_time=entry_ts,
                            sell_expiry_end=sell_expiry_end,
                            adjustment_already_made=True,
                            ce_wing_df=ce_wing_df, pe_wing_df=pe_wing_df,
                            ce_wing_entry=ce_wing_entry, pe_wing_entry=pe_wing_entry,
                            last_ce_wing_ltp=ce_wing_ltp, last_pe_wing_ltp=pe_wing_ltp,
                            opt_df_cache=opt_df_cache, buy_expiry_end=buy_expiry_end)

                    running_realised_pl += window_realised_emer_pl
                    # Re-price exit with updated sl_ts/sl_reason from resumed scan
                    if sl_ts is None:
                        sl_ts     = scan_end
                        sl_reason = 'pre_expiry'
                    if sl_reason == 'pre_expiry':
                        exit_ts  = elm_time if elm_time is not None else scan_end
                        use_col  = 'close'
                        slip     = False
                    else:
                        exit_ts  = sl_ts + pd.Timedelta(minutes=1)
                        use_col  = 'open'
                        slip     = True
                    ce_sell_exit, ce_sell_exit_raw = get_exit_price(ce_sell_df, ce_sell_ltp, is_buy=True)
                    ce_buy_exit,  ce_buy_exit_raw  = get_exit_price(ce_buy_df,  ce_buy_ltp,  is_buy=False)
                    pe_sell_exit, pe_sell_exit_raw = get_exit_price(pe_sell_df, pe_sell_ltp, is_buy=True)
                    pe_buy_exit,  pe_buy_exit_raw  = get_exit_price(pe_buy_df,  pe_buy_ltp,  is_buy=False)
                    ce_pl_base = _calc_exit_pl(ce_sell_entry, ce_sell_exit, ce_buy_entry, ce_buy_exit)
                    pe_pl_base = _calc_exit_pl(pe_sell_entry, pe_sell_exit, pe_buy_entry, pe_buy_exit)
                    base_pl    = round(ce_pl_base + pe_pl_base, 2)
                else:
                    new_sell_entry = apply_slippage(new_sell_raw, is_buy=False)

                    adj_trigger_day_val    = (adj_trigger_ts.date() - entry_date).days
                    adj_entry_spot_val     = roll_spot
                    adj_days_remaining_val = (sell_expiry_end.date() - roll_ts.date()).days

                    logger.info(
                        f"  ADJUSTMENT ROLL {win.upper()} | {roll_ts} | "
                        f"spot {roll_spot:.0f} | "
                        f"new sell {new_sell_strike}{win} @ {new_sell_entry:.1f} | "
                        f"buyback old sell @ {buyback_price:.1f} | "
                        f"days remaining: {adj_days_remaining_val}")

                    # Step 4a: handle buy leg roll if enabled
                    new_buy_entry   = None
                    new_buy_df      = None
                    buyback_buy_raw = None
                    buyback_buy_price = None

                    if ADJUST_BUY_LEG:
                        old_buy_df    = ce_buy_df  if win == 'ce' else pe_buy_df
                        old_buy_ltp   = ce_buy_ltp if win == 'ce' else pe_buy_ltp
                        buy_expiry_ts = buy_expiry_end  # same buy expiry as original

                        # Load new buy leg — same strike as new sell, same buy expiry
                        new_buy_key = (buy_expiry_ts, new_sell_strike, win)
                        if new_buy_key not in opt_df_cache:
                            opt_df_cache[new_buy_key] = load_option_data(
                                buy_expiry_ts, new_sell_strike, win)
                        new_buy_df  = opt_df_cache[new_buy_key]
                        new_buy_raw = get_option_price(new_buy_df, roll_ts, 'open')

                        if new_buy_raw is None:
                            logger.info(
                                f"  ADJ SKIP | No data for new buy leg {new_sell_strike}{win} "
                                f"buy_exp={buy_expiry_ts.date()} at {roll_ts} — roll aborted")
                            # Abort entire adjustment — re-run remainder unchanged
                            (ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
                             sl_ts, sl_reason, running_peak_pl, _, _,
                             ce_wing_ltp, pe_wing_ltp,
                             window_realised_emer_pl) = \
                                append_1min_snapshots_window(
                                    roll_ts - pd.Timedelta(minutes=1), scan_end,
                                    nifty_1m, vix_1m,
                                    ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
                                    ce_sell_strike, pe_sell_strike,
                                    ce_sell_entry, ce_buy_entry,
                                    pe_sell_entry, pe_buy_entry,
                                    total_net_debit, max_theoretical_profit,
                                    spot, elm_time, trade_log,
                                    ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
                                    running_realised_pl=running_realised_pl,
                                    running_peak_pl=running_peak_pl,
                                    entry_time=entry_ts,
                                    sell_expiry_end=sell_expiry_end,
                                    adjustment_already_made=True,
                                    ce_wing_df=ce_wing_df, pe_wing_df=pe_wing_df,
                                    ce_wing_entry=ce_wing_entry, pe_wing_entry=pe_wing_entry,
                                    last_ce_wing_ltp=ce_wing_ltp, last_pe_wing_ltp=pe_wing_ltp,
                                    opt_df_cache=opt_df_cache, buy_expiry_end=buy_expiry_end)

                            running_realised_pl += window_realised_emer_pl
                            if sl_ts is None:
                                sl_ts = scan_end; sl_reason = 'pre_expiry'
                            if sl_reason == 'pre_expiry':
                                exit_ts = elm_time if elm_time is not None else scan_end
                                use_col = 'close'; slip = False
                            else:
                                exit_ts = sl_ts + pd.Timedelta(minutes=1)
                                use_col = 'open'; slip = True
                            ce_sell_exit, ce_sell_exit_raw = get_exit_price(ce_sell_df, ce_sell_ltp, is_buy=True)
                            ce_buy_exit,  ce_buy_exit_raw  = get_exit_price(ce_buy_df,  ce_buy_ltp,  is_buy=False)
                            pe_sell_exit, pe_sell_exit_raw = get_exit_price(pe_sell_df, pe_sell_ltp, is_buy=True)
                            pe_buy_exit,  pe_buy_exit_raw  = get_exit_price(pe_buy_df,  pe_buy_ltp,  is_buy=False)
                            ce_pl_base = _calc_exit_pl(orig_ce_sell_entry, ce_sell_exit, ce_buy_entry, ce_buy_exit)
                            pe_pl_base = _calc_exit_pl(orig_pe_sell_entry, pe_sell_exit, pe_buy_entry, pe_buy_exit)
                            base_pl    = round(ce_pl_base + pe_pl_base, 2)
                            # Skip to exit — do not set adj_made
                            new_buy_df = None  # sentinel: abort was triggered

                        else:
                            # Close old buy leg (we are selling it back)
                            buyback_buy_raw   = get_option_price(old_buy_df, roll_ts, 'open') or old_buy_ltp
                            buyback_buy_price = apply_slippage(buyback_buy_raw, is_buy=False)
                            new_buy_entry     = apply_slippage(new_buy_raw, is_buy=True)

                    # Step 4b: update position state (only if buy leg check passed or disabled)
                    if not ADJUST_BUY_LEG or new_buy_df is not None:
                        # Compute realised P&L from the closed legs
                        if win == 'ce':
                            # Proceeds from original sell minus cost to buy it back
                            realised = round(ce_sell_entry - buyback_price, 2)
                            if ADJUST_BUY_LEG:
                                # Proceeds from selling old buy leg back minus original cost
                                realised = round(realised + (buyback_buy_price - ce_buy_entry), 2)
                        else:
                            realised = round(pe_sell_entry - buyback_price, 2)
                            if ADJUST_BUY_LEG:
                                realised = round(realised + (buyback_buy_price - pe_buy_entry), 2)

                        running_realised_pl = round(running_realised_pl + realised, 2)

                        if win == 'ce':
                            ce_sell_df          = new_sell_df
                            ce_sell_entry       = new_sell_entry
                            ce_sell_ltp         = new_sell_entry
                            ce_sell_strike      = new_sell_strike
                            adj_ce_sell_strike      = new_sell_strike
                            adj_ce_new_sell_entry   = new_sell_entry
                            adj_ce_sell_buyback     = buyback_price
                            if ADJUST_BUY_LEG:
                                ce_buy_df           = new_buy_df
                                ce_buy_entry        = new_buy_entry
                                ce_buy_ltp          = new_buy_entry
                                adj_ce_buy_buyback  = buyback_buy_price
                                adj_ce_new_buy_entry = new_buy_entry
                        else:
                            pe_sell_df          = new_sell_df
                            pe_sell_entry       = new_sell_entry
                            pe_sell_ltp         = new_sell_entry
                            pe_sell_strike      = new_sell_strike
                            adj_pe_sell_strike      = new_sell_strike
                            adj_pe_new_sell_entry   = new_sell_entry
                            adj_pe_sell_buyback     = buyback_price
                            if ADJUST_BUY_LEG:
                                pe_buy_df           = new_buy_df
                                pe_buy_entry        = new_buy_entry
                                pe_buy_ltp          = new_buy_entry
                                adj_pe_buy_buyback  = buyback_buy_price
                                adj_pe_new_buy_entry = new_buy_entry

                        adj_side = win
                        # Recompute total_net_debit with updated entries
                        total_net_debit = round(
                            (ce_buy_entry - ce_sell_entry) +
                            (pe_buy_entry - pe_sell_entry), 2)

                        # Step 5: re-run scanner from roll_ts for remainder of week
                        # We pass the NEW entry prices. Unrealised P&L will correctly drop
                        # to 0.0 at the roll minute, and cumulative P&L will stay smooth.
                        (ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
                         sl_ts, sl_reason, running_peak_pl, _, _,
                         ce_wing_ltp, pe_wing_ltp,
                         window_realised_emer_pl) = \
                            append_1min_snapshots_window(
                                roll_ts - pd.Timedelta(minutes=1), scan_end,
                                nifty_1m, vix_1m,
                                ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
                                ce_sell_strike, pe_sell_strike,
                                ce_sell_entry, ce_buy_entry,
                                pe_sell_entry, pe_buy_entry,
                                total_net_debit, max_theoretical_profit,
                                spot, elm_time, trade_log,
                                ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
                                running_realised_pl=running_realised_pl,
                                running_peak_pl=running_peak_pl,
                                entry_time=entry_ts,
                                sell_expiry_end=sell_expiry_end,
                                adjustment_already_made=True,
                                ce_wing_df=ce_wing_df, pe_wing_df=pe_wing_df,
                                ce_wing_entry=ce_wing_entry, pe_wing_entry=pe_wing_entry,
                                last_ce_wing_ltp=ce_wing_ltp, last_pe_wing_ltp=pe_wing_ltp,
                                opt_df_cache=opt_df_cache, buy_expiry_end=buy_expiry_end)

                        running_realised_pl += window_realised_emer_pl

                        adj_exit_reason = sl_reason if sl_reason is not None else 'pre_expiry'
                        adj_made = True

        # ----------------------------------------------------------------
        # Re-price exit if adjustment was made (sl_ts/sl_reason may have
        # changed from the second scanner call inside the adjustment block)
        # ----------------------------------------------------------------
        if adj_made:
            if sl_ts is None:
                sl_ts     = scan_end
                sl_reason = 'pre_expiry'
            if sl_reason == 'pre_expiry':
                exit_ts  = elm_time if elm_time is not None else scan_end
                use_col  = 'close'
                slip     = False
            else:
                exit_ts  = sl_ts + pd.Timedelta(minutes=1)
                use_col  = 'open'
                slip     = True

            ce_sell_exit, ce_sell_exit_raw = get_exit_price(
                ce_sell_df, ce_sell_ltp, is_buy=True)
            ce_buy_exit,  ce_buy_exit_raw  = get_exit_price(
                ce_buy_df,  ce_buy_ltp,  is_buy=False)
            pe_sell_exit, pe_sell_exit_raw = get_exit_price(
                pe_sell_df, pe_sell_ltp, is_buy=True)
            pe_buy_exit,  pe_buy_exit_raw  = get_exit_price(
                pe_buy_df,  pe_buy_ltp,  is_buy=False)

            # P&L computed by separating realised gain from adjustments
            # ce_pl_final / pe_pl_final measure the floating P&L of the ACTIVE legs at exit
            ce_pl_final = _calc_exit_pl(ce_sell_entry, ce_sell_exit,
                                        ce_buy_entry,  ce_buy_exit)
            pe_pl_final = _calc_exit_pl(pe_sell_entry, pe_sell_exit,
                                        pe_buy_entry,  pe_buy_exit)
            
            # base_pl in the summary for an adjusted trade is the final unrealised P&L 
            # of the current legs at the moment they were closed at expiry/SL.
            base_pl = round(ce_pl_final + pe_pl_final, 2)

            if adj_side == 'ce':
                sell_adj = round(adj_ce_new_sell_entry - adj_ce_sell_buyback, 2)
                buy_adj  = round(adj_ce_buy_buyback - adj_ce_new_buy_entry, 2) if ADJUST_BUY_LEG and adj_ce_buy_buyback is not None else 0.0
                adj_pl_points        = round(sell_adj + buy_adj, 2)
                adj_ce_new_sell_exit = ce_sell_exit
                adj_ce_new_buy_exit  = ce_buy_exit if ADJUST_BUY_LEG else None
            elif adj_side == 'pe':
                sell_adj = round(adj_pe_new_sell_entry - adj_pe_sell_buyback, 2)
                buy_adj  = round(adj_pe_buy_buyback - adj_pe_new_buy_entry, 2) if ADJUST_BUY_LEG and adj_pe_buy_buyback is not None else 0.0
                adj_pl_points        = round(sell_adj + buy_adj, 2)
                adj_pe_new_sell_exit = pe_sell_exit
                adj_pe_new_buy_exit  = pe_buy_exit if ADJUST_BUY_LEG else None

            # Final strategy P&L = locked in gains + final leg P&L
            total_pl = round(running_realised_pl + base_pl, 2)

            logger.info(
                f"  ADJ EXIT {adj_exit_reason:20s} | {exit_ts} | "
                f"Roll P&L: {adj_pl_points:+.1f} pts | "
                f"Total P&L: {total_pl:+.1f} pts ({total_pl * LOT_SIZE:+,.0f})"
            )
        else:
            total_pl = round(running_realised_pl + base_pl, 2)
            logger.info(
                f"  BASE EXIT {sl_reason:20s} | {exit_ts} | "
                f"P&L: {total_pl:+.1f} pts ({total_pl * LOT_SIZE:+,.0f})"
            )

        # Append final exit snapshot using current leg entries
        # Using slippage-adjusted exit prices (ce_sell_exit) ensures the final row's 
        # cumulative_pl matches the actual total_pl of the trade.
        exit_spot = get_1min_value(nifty_1m, exit_ts, 'close') or spot
        exit_vix  = get_1min_value(vix_1m,   exit_ts, 'close')
        trade_log.append(build_snapshot(
            exit_ts, exit_spot, exit_vix,
            ce_sell_strike, pe_sell_strike,
            ce_sell_exit, ce_buy_exit,
            pe_sell_exit, pe_buy_exit,
            ce_sell_entry, ce_buy_entry,
            pe_sell_entry, pe_buy_entry,
            total_net_debit, max_theoretical_profit,
            running_realised_pl=running_realised_pl,
            ce_wing_ltp=ce_wing_exit, ce_wing_entry=ce_wing_entry,
            pe_wing_ltp=pe_wing_exit, pe_wing_entry=pe_wing_entry,
        ))

        # ----------------------------------------------------------------
        # Build final trade record
        # ----------------------------------------------------------------
        # Compute min/max spot and VIX across full trade duration
        # (including adjustment if any) from the completed trade_log
        # Single pass over full trade_log (base + adjustment) to compute
        # min/max spot, VIX, P&L and their timestamps
        trade_max_spot = trade_min_spot = None
        trade_max_vix  = trade_min_vix  = None
        trade_max_pl   = trade_min_pl   = None
        trade_max_pl_time = trade_min_pl_time = None

        for snap in trade_log:
            s_spot = snap.get('spot')
            s_vix  = snap.get('vix')
            s_pl   = snap.get('combined_unrealised_pl')
            s_ts   = snap.get('time_stamp')

            if s_spot is not None:
                if trade_max_spot is None or s_spot > trade_max_spot:
                    trade_max_spot = s_spot
                if trade_min_spot is None or s_spot < trade_min_spot:
                    trade_min_spot = s_spot

            if s_vix is not None:
                if trade_max_vix is None or s_vix > trade_max_vix:
                    trade_max_vix = s_vix
                if trade_min_vix is None or s_vix < trade_min_vix:
                    trade_min_vix = s_vix

            if s_pl is not None:
                if trade_max_pl is None or s_pl > trade_max_pl:
                    trade_max_pl      = s_pl
                    trade_max_pl_time = s_ts
                if trade_min_pl is None or s_pl < trade_min_pl:
                    trade_min_pl      = s_pl
                    trade_min_pl_time = s_ts

        trade_max_spot = round(trade_max_spot, 2) if trade_max_spot is not None else None
        trade_min_spot = round(trade_min_spot, 2) if trade_min_spot is not None else None
        trade_max_vix  = round(trade_max_vix,  2) if trade_max_vix  is not None else None
        trade_min_vix  = round(trade_min_vix,  2) if trade_min_vix  is not None else None
        trade_max_pl   = round(trade_max_pl,   2) if trade_max_pl   is not None else None
        trade_min_pl   = round(trade_min_pl,   2) if trade_min_pl   is not None else None
        # Trail activation: True if peak ever reached the activation threshold
        # Uses max_pl from trade_log which covers base + adjustment
        trail_activation_reached = (trade_max_pl is not None and
                                    trade_max_pl >= TRAIL_ACTIVATION_POINTS)

        # Compute SL detail columns
        # sl_ts is the 1-min candle close that fired the SL (None for profit_target/pre_expiry)
        is_sl_exit = sl_reason in ('index_sl', 'option_sl', 'spread_sl')
        if is_sl_exit and sl_ts is not None:
            sl_trigger_spot_val = get_1min_value(nifty_1m, sl_ts, 'close') or spot
            sl_triggered_side_val = determine_sl_triggered_side(
                sl_reason, sl_trigger_spot_val, spot)
            sl_trigger_day_val = (sl_ts.date() - entry_date).days
            days_remaining_val = (elm_time.date() - sl_ts.date()).days \
                if elm_time is not None else None
            # Untouched side LTPs at SL moment — from the running LTPs
            # returned by the scanner (values at sl_ts candle close)
            if sl_triggered_side_val == 'ce':
                u_sell = pe_sell_ltp
                u_buy  = pe_buy_ltp
            else:
                u_sell = ce_sell_ltp
                u_buy  = ce_buy_ltp
            untouched_sell_val = u_sell
            untouched_buy_val  = u_buy
            untouched_net_val  = round(u_buy - u_sell, 2) \
                if u_buy is not None and u_sell is not None else None
        else:
            sl_triggered_side_val = 'none'
            sl_trigger_spot_val   = None
            sl_trigger_day_val    = None
            days_remaining_val    = None
            untouched_sell_val    = None
            untouched_buy_val     = None
            untouched_net_val     = None

        total_pl = round(base_pl + (adj_pl_points or 0.0), 2)
        trade_counter += 1

        record = build_trade_record(
            entry_ts, spot, entry_vix,
            sell_expiry_date, buy_expiry_date,
            orig_ce_sell_strike,
            orig_pe_sell_strike,
            orig_ce_sell_entry,
            orig_ce_buy_entry,
            orig_pe_sell_entry,
            orig_pe_buy_entry,
            ce_sell_delta, pe_sell_delta,
            net_debit_ce, net_debit_pe,
            max_theoretical_profit,
            target_delta_used,
            exit_ts, sl_reason,
            ce_sell_exit, ce_buy_exit,
            pe_sell_exit, pe_buy_exit,
            trade_max_spot, trade_min_spot,
            trade_max_vix,  trade_min_vix,
            trade_max_pl,   trade_min_pl,
            trade_max_pl_time, trade_min_pl_time,
            trail_activation_reached,
            sl_triggered_side_val, sl_ts, sl_trigger_spot_val,
            sl_trigger_day_val,
            untouched_sell_val, untouched_buy_val,
            untouched_net_val,  days_remaining_val,
            adjustment_made=adj_made,
            adj_side=adj_side,
            adj_ce_sell_strike=adj_ce_sell_strike,
            adj_pe_sell_strike=adj_pe_sell_strike,
            adj_ce_new_sell_entry=adj_ce_new_sell_entry,
            adj_ce_sell_buyback=adj_ce_sell_buyback,
            adj_pe_new_sell_entry=adj_pe_new_sell_entry,
            adj_pe_sell_buyback=adj_pe_sell_buyback,
            adj_ce_new_sell_exit=adj_ce_new_sell_exit,
            adj_pe_new_sell_exit=adj_pe_new_sell_exit,
            adj_ce_buy_buyback=adj_ce_buy_buyback,
            adj_pe_buy_buyback=adj_pe_buy_buyback,
            adj_ce_new_buy_entry=adj_ce_new_buy_entry,
            adj_pe_new_buy_entry=adj_pe_new_buy_entry,
            adj_ce_new_buy_exit=adj_ce_new_buy_exit,
            adj_pe_new_buy_exit=adj_pe_new_buy_exit,
            adj_exit_reason=adj_exit_reason,
            adj_pl_points=adj_pl_points,
            adj_entry_spot=adj_entry_spot_val,
            adj_days_remaining=adj_days_remaining_val,
            adj_trigger_day=adj_trigger_day_val,
            # Safety Wing fields
            ce_wing_strike=ce_wing_strike, pe_wing_strike=pe_wing_strike,
            ce_wing_entry=ce_wing_entry, pe_wing_entry=pe_wing_entry,
            ce_wing_exit=ce_wing_exit, pe_wing_exit=pe_wing_exit,
            realised_pl=running_realised_pl,
            )

        all_trades.append(record)
        save_trade_log(trade_counter, entry_ts, trade_log)

    logger.info("Skip reason summary:")
    for reason, count in skip_counts.items():
        if count > 0:
            logger.info(f"  {reason:30s}: {count}")
    logger.info(f"Backtest complete. Total trades: {len(all_trades)}")
    return all_trades


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("=== Athena Backtest starting ===")

    nifty_1m, vix_1m = load_index_data()

    holidays_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data_pipeline', 'config', 'holidays.csv')
    holidays_df = pd.read_csv(holidays_path, parse_dates=['date'])
    holidays_df['date'] = pd.to_datetime(holidays_df['date']).dt.date

    contracts_df = load_contracts(holidays_df)
    logger.info(f"  Contracts    : {len(contracts_df)} expiries")
    logger.info(f"  Entry time   : {ENTRY_TIME} on day before sell expiry")
    logger.info(f"  Delta bands  : {VIX_DELTA_BANDS}")
    logger.info(f"  Buy min DTE  : {BUY_LEG_MIN_DTE}")
    logger.info(f"  VIX filter   : {'ON' if ENABLE_VIX_FILTER else 'OFF'}"
                + (f" ({VIX_FILTER_LOW}–{VIX_FILTER_HIGH})" if ENABLE_VIX_FILTER else ""))
    logger.info(f"  Index SL     : {'ON' if ENABLE_INDEX_SL else 'OFF'} "
                f"({INDEX_SL_OFFSET} pts)")
    logger.info(f"  Option SL    : {'ON' if ENABLE_OPTION_SL else 'OFF'} "
                f"({OPTION_SL_MULTIPLIER}x entry)")
    logger.info(f"  Spread SL    : {'ON' if ENABLE_SPREAD_SL else 'OFF'} "
                f"({SPREAD_SL_POINTS} pts)")
    logger.info(f"  Profit target: {'ON' if ENABLE_PROFIT_TARGET else 'OFF'} "
                f"({PROFIT_TARGET_PCT_NET_DEBIT * 100:.0f}% of net debit)")
    logger.info(f"  Adjustment   : {'ON' if ENABLE_ADJUSTMENT else 'OFF'}"
                + (f" (trigger_offset={ADJUSTMENT_TRIGGER_OFFSET}, "
                   f"dist={ADJUSTMENT_NEW_STRIKE_DISTANCE}, "
                   f"excluded_days={ADJUSTMENT_EXCLUDED_DAYS}, "
                   f"adjust_buy_leg={ADJUST_BUY_LEG})"
                   if ENABLE_ADJUSTMENT else ""))
    logger.info(f"  Trail stop   : {'ON' if ENABLE_TRAIL_STOP else 'OFF'}"
                + (f" (activate={TRAIL_ACTIVATION_POINTS} pts, trail={TRAIL_POINTS} pts)"
                   if ENABLE_TRAIL_STOP else ""))

    all_trades = run_backtest(nifty_1m, vix_1m, contracts_df, holidays_df)
    save_trade_summary(all_trades)

    logger.info("=== Athena Backtest complete ===")