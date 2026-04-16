"""
backtest.py — Athena Backtest Engine
Double calendar spread strategy on Nifty weekly options.

Structure:
  - Sell 20-delta CE and PE on the near-term expiry (sell leg)
  - Buy same strikes on the last expiry of the current month (buy leg)
  - Entry: trading day immediately before the expiry preceding the sell expiry
    e.g. sell=14 Aug, prior expiry=7 Aug, entry=6 Aug
  - Strike rounding: nearest 100 points

Execution model:
  - Entry at ENTRY_TIME on the day before sell expiry
  - Pre-expiry exit at 15:15 on the day before sell expiry (elm_time)
  - SL fires on 1-min candle close → exit at open of next 1-min candle
  - Pre-expiry exit uses close price at elm_time (no slippage)
  - All four legs always exit simultaneously on any trigger
  - Adjustment: re-enter fresh one-sided calendar on breached side if within cutoff

Entry day is derived from the contract list and holiday calendar —
fully regime-agnostic across Thursday (pre-Sep 2025) and Tuesday
(post-Sep 2025) expiry schedules, and any future changes.

Run after Tuesday night data pipeline cron has completed.
"""

import os
import sys
import logging
import warnings
from datetime import date, timedelta

import pandas as pd
import numpy as np
import mibian

sys.path.insert(0, os.path.dirname(__file__))

from configs import (
    NIFTY_INDEX_FILE, VIX_INDEX_FILE,
    NIFTY_OPTIONS_PATH, CONTRACT_LIST_FILE,
    TRADE_LOGS_DIR, TRADE_SUMMARY_FILE,
    ENTRY_TIME, DELTA_TARGET, STRIKE_STEP, BUY_LEG_MIN_DTE,
    ENABLE_PROFIT_TARGET, PROFIT_TARGET_PCT,
    ENABLE_INDEX_SL, INDEX_SL_OFFSET,
    ENABLE_OPTION_SL, OPTION_SL_MULTIPLIER,
    ENABLE_SPREAD_SL, SPREAD_SL_PCT,
    ENABLE_ADJUSTMENT, ADJUSTMENT_CUTOFF_TIME,
    MAX_ADJUSTMENTS_PER_SIDE,
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
            elm = pd.Timestamp(f"{last_trading} 15:15:00")
        else:
            # Fallback: should never happen with a valid contract list
            elm = row['end_date'] - pd.Timedelta(seconds=87300)
        elm_times.append(elm)

    df['elm_time'] = elm_times
    df = df.sort_values('expiry_date').reset_index(drop=True)
    return df


def load_option_data(expiry_date: pd.Timestamp, strike: int,
                     option_type: str) -> pd.DataFrame:
    """
    Load 1-min option data for a given expiry, strike and type.
    Returns empty DataFrame if file not found.
    """
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

    For each side:
      1. Back out IV from sell leg market price
      2. Project buy leg theoretical value at sell expiry using sell leg IV
         and buy leg remaining DTE after sell expiry
      3. Max profit per side = projected_buy_value - buy_entry + sell_entry
         (sell premium kept + buy leg value at sell expiry - cost of buy leg)

    Combined = CE side + PE side.
    Returns combined max theoretical profit, or fallback (net premium * 2) on failure.
    """
    sell_dte = max((sell_expiry.date() - entry_ts.date()).days, 0.5)
    buy_dte  = max((buy_expiry.date()  - entry_ts.date()).days, 0.5)
    # Remaining DTE of buy leg after sell expiry
    buy_dte_at_sell_expiry = max((buy_expiry.date() - sell_expiry.date()).days, 0.5)

    def side_max_profit(sell_strike, sell_entry_raw, buy_entry_raw, opt_type,
                        sell_df, buy_df):
        # Raw entries (pre-slippage) needed for IV computation
        # Back out IV from sell leg
        iv = compute_iv(spot, sell_strike, sell_dte, sell_entry_raw, opt_type)
        if iv is None:
            return None
        # Project buy leg value at sell expiry
        proj_buy = compute_theoretical_value(
            spot, sell_strike, buy_dte_at_sell_expiry, iv, opt_type)
        if proj_buy is None:
            return None
        # Max profit = sell_entry + (proj_buy - buy_entry)
        # Use slippage-adjusted entries since that is our actual cost basis
        # but IV must be computed from raw market prices — use raw entries here
        return sell_entry_raw + (proj_buy - buy_entry_raw)

    # Fetch raw (pre-slippage) entry prices for IV computation
    ce_sell_raw = get_option_price(ce_sell_df, entry_ts, 'open')
    pe_sell_raw = get_option_price(pe_sell_df, entry_ts, 'open')
    ce_buy_raw  = get_option_price(ce_buy_df,  entry_ts, 'open')
    pe_buy_raw  = get_option_price(pe_buy_df,  entry_ts, 'open')

    ce_max = None
    pe_max = None

    if ce_sell_raw and ce_buy_raw:
        ce_max = side_max_profit(
            ce_sell_strike, ce_sell_raw, ce_buy_raw, 'ce',
            ce_sell_df, ce_buy_df)

    if pe_sell_raw and pe_buy_raw:
        pe_max = side_max_profit(
            pe_sell_strike, pe_sell_raw, pe_buy_raw, 'pe',
            pe_sell_df, pe_buy_df)

    if ce_max is not None and pe_max is not None:
        return round(ce_max + pe_max, 2)

    # Fallback: use net premium collected as proxy
    net_ce = (ce_sell_entry - ce_buy_entry) if ce_sell_entry and ce_buy_entry else 0
    net_pe = (pe_sell_entry - pe_buy_entry) if pe_sell_entry and pe_buy_entry else 0
    return round((net_ce + net_pe), 2)


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------

def select_strike(spot: float, sell_expiry: pd.Timestamp,
                  entry_ts: pd.Timestamp, option_type: str,
                  opt_df_cache: dict) -> tuple:
    """
    Scan strikes from ATM outward and select first with abs(delta) <= DELTA_TARGET.
    ATM rounded to nearest STRIKE_STEP (100 for Nifty).
    CE: scan upward from ATM. PE: scan downward from ATM.

    Returns (strike, entry_price_raw) or (None, None) if no valid strike found.
    entry_price_raw is before slippage.
    """
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

        if delta <= DELTA_TARGET:
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

def calc_calendar_pl(sell_entry: float, sell_ltp: float,
                     buy_entry: float,  buy_ltp: float) -> float:
    """
    Unrealised P&L for one side of the calendar spread (in points).
    Sell leg: profit when LTP falls. Buy leg: profit when LTP rises.
    P&L = (sell_entry - sell_ltp) + (buy_ltp - buy_entry)
    """
    return round((sell_entry - sell_ltp) + (buy_ltp - buy_entry), 2)


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


def check_spread_sl(combined_pl: float, total_net_debit: float) -> bool:
    """
    Check spread SL: combined unrealised P&L <= -SPREAD_SL_PCT * total net debit paid.
    A double calendar is a net debit strategy — total_net_debit is positive (what you paid).
    Returns True if loss exceeds the threshold.
    """
    if not ENABLE_SPREAD_SL:
        return False
    if total_net_debit <= 0:
        return False
    return combined_pl <= -(SPREAD_SL_PCT * total_net_debit)


def check_profit_target(combined_pl: float, max_theoretical_profit: float) -> bool:
    """
    Check profit target: combined unrealised P&L >= PROFIT_TARGET_PCT * max_theoretical_profit.
    Returns True if target reached.
    """
    if not ENABLE_PROFIT_TARGET:
        return False
    if max_theoretical_profit <= 0:
        return False
    return combined_pl >= PROFIT_TARGET_PCT * max_theoretical_profit


def determine_breached_side(spot: float, entry_spot: float) -> str:
    """
    Determine which side was breached based on spot vs entry spot.
    Spot above entry → CE side breached.
    Spot below entry → PE side breached.
    Returns 'ce' or 'pe'.
    """
    return 'ce' if spot >= entry_spot else 'pe'


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
                   realised_pl_pts: float = None,
                   realised_pl_rs: float = None) -> dict:
    """Build one row for the per-trade 1-min log."""
    ce_pl = calc_calendar_pl(ce_sell_entry, ce_sell_ltp, ce_buy_entry, ce_buy_ltp)
    pe_pl = calc_calendar_pl(pe_sell_entry, pe_sell_ltp, pe_buy_entry, pe_buy_ltp)
    combined_pl = round(ce_pl + pe_pl, 2)

    ce_index_sl = ce_sell_strike - INDEX_SL_OFFSET if ENABLE_INDEX_SL else None
    pe_index_sl = pe_sell_strike + INDEX_SL_OFFSET if ENABLE_INDEX_SL else None
    ce_opt_sl   = round(ce_sell_entry * OPTION_SL_MULTIPLIER, 2) if ENABLE_OPTION_SL else None
    pe_opt_sl   = round(pe_sell_entry * OPTION_SL_MULTIPLIER, 2) if ENABLE_OPTION_SL else None

    return {
        'time_stamp':           ts,
        'spot':                 round(spot, 2),
        'vix':                  round(vix, 2) if vix is not None else None,
        'ce_sell_strike':       ce_sell_strike,
        'pe_sell_strike':       pe_sell_strike,
        'ce_sell_ltp':          round(ce_sell_ltp, 2),
        'ce_buy_ltp':           round(ce_buy_ltp,  2),
        'pe_sell_ltp':          round(pe_sell_ltp, 2),
        'pe_buy_ltp':           round(pe_buy_ltp,  2),
        'ce_unrealised_pl':     ce_pl,
        'pe_unrealised_pl':     pe_pl,
        'combined_unrealised_pl': combined_pl,
        'ce_index_sl_level':    ce_index_sl,
        'pe_index_sl_level':    pe_index_sl,
        'ce_option_sl_level':   ce_opt_sl,
        'pe_option_sl_level':   pe_opt_sl,
        'realised_pl_pts':      round(realised_pl_pts, 2) if realised_pl_pts is not None else None,
        'realised_pl_rs':       round(realised_pl_rs,  2) if realised_pl_rs  is not None else None,
    }


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
                                  last_pe_buy_ltp:  float):
    """
    Append 1-min snapshots for every minute in (from_ts, to_ts] to trade_log.
    Checks all SL conditions and pre-expiry on every candle.
    Returns (ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
             sl_hit_ts, sl_hit_reason).
    sl_hit_ts is None if no SL fired in this window.
    """
    running_ce_sell = last_ce_sell_ltp
    running_ce_buy  = last_ce_buy_ltp
    running_pe_sell = last_pe_sell_ltp
    running_pe_buy  = last_pe_buy_ltp
    sl_hit_ts       = None
    sl_hit_reason   = None

    window = nifty_1m[
        (nifty_1m.index > from_ts) & (nifty_1m.index <= to_ts)
    ]

    for ts, row in window.iterrows():
        spot = float(row['close'])
        vix  = get_1min_value(vix_1m, ts, 'close')

        # Update LTPs — carry forward on miss
        v = get_option_price(ce_sell_df, ts, 'close')
        if v is not None: running_ce_sell = v
        v = get_option_price(ce_buy_df,  ts, 'close')
        if v is not None: running_ce_buy  = v
        v = get_option_price(pe_sell_df, ts, 'close')
        if v is not None: running_pe_sell = v
        v = get_option_price(pe_buy_df,  ts, 'close')
        if v is not None: running_pe_buy  = v

        ce_pl       = calc_calendar_pl(ce_sell_entry, running_ce_sell,
                                       ce_buy_entry,  running_ce_buy)
        pe_pl       = calc_calendar_pl(pe_sell_entry, running_pe_sell,
                                       pe_buy_entry,  running_pe_buy)
        combined_pl = round(ce_pl + pe_pl, 2)

        trade_log.append(build_snapshot(
            ts, spot, vix,
            ce_sell_strike, pe_sell_strike,
            running_ce_sell, running_ce_buy,
            running_pe_sell, running_pe_buy,
            ce_sell_entry, ce_buy_entry,
            pe_sell_entry, pe_buy_entry,
            total_net_debit, max_theoretical_profit,
        ))

        if sl_hit_ts is not None:
            break  # already found SL — stop scanning

        # Pre-expiry check
        if elm_time is not None and ts >= elm_time:
            sl_hit_ts     = ts
            sl_hit_reason = 'pre_expiry'
            break

        # Exit checks in priority order
        if check_spread_sl(combined_pl, total_net_debit):
            sl_hit_ts     = ts
            sl_hit_reason = 'spread_sl'
            break

        if check_index_sl(spot, ce_sell_strike, pe_sell_strike):
            sl_hit_ts     = ts
            sl_hit_reason = 'index_sl'
            break

        if check_option_sl(running_ce_sell, ce_sell_entry,
                           running_pe_sell, pe_sell_entry):
            sl_hit_ts     = ts
            sl_hit_reason = 'option_sl'
            break

        if check_profit_target(combined_pl, max_theoretical_profit):
            sl_hit_ts     = ts
            sl_hit_reason = 'profit_target'
            break

    return (running_ce_sell, running_ce_buy, running_pe_sell, running_pe_buy,
            sl_hit_ts, sl_hit_reason)


# ---------------------------------------------------------------------------
# Trade record builder
# ---------------------------------------------------------------------------

def build_trade_record(entry_time, entry_spot, entry_vix,
                        sell_expiry, buy_expiry,
                        ce_sell_strike, pe_sell_strike,
                        ce_sell_entry, ce_buy_entry,
                        pe_sell_entry, pe_buy_entry,
                        ce_sell_delta, pe_sell_delta,
                        net_debit_ce, net_debit_pe,
                        max_theoretical_profit,
                        # Exit fields
                        exit_time, exit_reason,
                        ce_sell_exit, ce_buy_exit,
                        pe_sell_exit, pe_buy_exit,
                        # Adjustment fields
                        adjustment_made=False,
                        adj_side=None,
                        adj_sell_strike=None,
                        adj_sell_entry=None,
                        adj_buy_entry=None,
                        adj_sell_exit=None,
                        adj_buy_exit=None,
                        adj_exit_reason=None,
                        adj_pl_points=None) -> dict:
    """Build a complete trade summary record."""
    ce_pl = _calc_exit_pl(ce_sell_entry, ce_sell_exit, ce_buy_entry, ce_buy_exit)
    pe_pl = _calc_exit_pl(pe_sell_entry, pe_sell_exit, pe_buy_entry, pe_buy_exit)
    base_pl   = round(ce_pl + pe_pl, 2)
    adj_pl    = round(adj_pl_points, 2) if adj_pl_points is not None else 0.0
    total_pl  = round(base_pl + adj_pl, 2)
    total_rs  = round(total_pl * LOT_SIZE, 2)

    return {
        'entry_time':              entry_time,
        'entry_spot':              entry_spot,
        'entry_vix':               round(entry_vix, 2) if entry_vix is not None else None,
        'sell_expiry':             sell_expiry,
        'buy_expiry':              buy_expiry,
        'ce_sell_strike':          ce_sell_strike,
        'pe_sell_strike':          pe_sell_strike,
        'ce_sell_entry':           round(ce_sell_entry, 2),
        'ce_buy_entry':            round(ce_buy_entry,  2),
        'pe_sell_entry':           round(pe_sell_entry, 2),
        'pe_buy_entry':            round(pe_buy_entry,  2),
        'ce_sell_delta':           round(ce_sell_delta, 4) if ce_sell_delta else None,
        'pe_sell_delta':           round(pe_sell_delta, 4) if pe_sell_delta else None,
        'net_debit_ce':            round(net_debit_ce, 2),
        'net_debit_pe':            round(net_debit_pe, 2),
        'max_theoretical_profit':  round(max_theoretical_profit, 2),
        'exit_time':               exit_time,
        'exit_reason':             exit_reason,
        'ce_sell_exit':            round(ce_sell_exit, 2) if ce_sell_exit else None,
        'ce_buy_exit':             round(ce_buy_exit,  2) if ce_buy_exit  else None,
        'pe_sell_exit':            round(pe_sell_exit, 2) if pe_sell_exit else None,
        'pe_buy_exit':             round(pe_buy_exit,  2) if pe_buy_exit  else None,
        'ce_pl_points':            round(ce_pl, 2),
        'pe_pl_points':            round(pe_pl, 2),
        'adjustment_made':         adjustment_made,
        'adj_side':                adj_side,
        'adj_sell_strike':         adj_sell_strike,
        'adj_sell_entry':          round(adj_sell_entry, 2) if adj_sell_entry else None,
        'adj_buy_entry':           round(adj_buy_entry,  2) if adj_buy_entry  else None,
        'adj_sell_exit':           round(adj_sell_exit,  2) if adj_sell_exit  else None,
        'adj_buy_exit':            round(adj_buy_exit,   2) if adj_buy_exit   else None,
        'adj_exit_reason':         adj_exit_reason,
        'adj_pl_points':           round(adj_pl_points, 2) if adj_pl_points is not None else None,
        'total_pl_points':         total_pl,
        'total_pl_rupees':         total_rs,
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
    Iterates through every sell expiry in the contract list.
    Entry date = trading day immediately before the sell expiry (holiday-adjusted).
    Manages the trade through to elm_time / sell expiry via 1-min candle scanning.
    Handles adjustment logic if SL fires within the cutoff window.
    """
    os.makedirs(TRADE_LOGS_DIR, exist_ok=True)

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
        ce_sell_strike, ce_sell_raw = select_strike(
            spot, sell_expiry_end, entry_ts, 'ce', opt_df_cache)
        pe_sell_strike, pe_sell_raw = select_strike(
            spot, sell_expiry_end, entry_ts, 'pe', opt_df_cache)

        if ce_sell_strike is None or pe_sell_strike is None:
            skip_counts['strike_failed'] += 1
            logger.info(f"  {sell_expiry_date}: Strike selection failed — "
                        f"entry={entry_date} spot={spot:.0f} "
                        f"CE={ce_sell_strike} PE={pe_sell_strike} — skipping")
            continue

        # ----------------------------------------------------------------
        # Load all four option files
        # ----------------------------------------------------------------
        ce_sell_df = opt_df_cache.get(
            (sell_expiry_end, ce_sell_strike, 'ce'),
            load_option_data(sell_expiry_end, ce_sell_strike, 'ce'))
        pe_sell_df = opt_df_cache.get(
            (sell_expiry_end, pe_sell_strike, 'pe'),
            load_option_data(sell_expiry_end, pe_sell_strike, 'pe'))

        ce_buy_df_key = (buy_expiry_end, ce_sell_strike, 'ce')
        pe_buy_df_key = (buy_expiry_end, pe_sell_strike, 'pe')
        if ce_buy_df_key not in opt_df_cache:
            opt_df_cache[ce_buy_df_key] = load_option_data(
                buy_expiry_end, ce_sell_strike, 'ce')
        if pe_buy_df_key not in opt_df_cache:
            opt_df_cache[pe_buy_df_key] = load_option_data(
                buy_expiry_end, pe_sell_strike, 'pe')
        ce_buy_df = opt_df_cache[ce_buy_df_key]
        pe_buy_df = opt_df_cache[pe_buy_df_key]

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

        # Net debit per side = what you pay (buy leg cost − sell leg premium received)
        # Always positive for a calendar — far-term option is worth more than near-term
        net_debit_ce = round(ce_buy_entry - ce_sell_entry, 2)
        net_debit_pe = round(pe_buy_entry - pe_sell_entry, 2)
        total_net_debit = round(net_debit_ce + net_debit_pe, 2)

        # ----------------------------------------------------------------
        # Delta and max theoretical profit at entry
        # ----------------------------------------------------------------
        sell_dte    = max((sell_expiry_date - entry_date).days, 0.5)
        ce_sell_delta = compute_delta(spot, ce_sell_strike, sell_dte,
                                      ce_sell_raw, 'ce')
        pe_sell_delta = compute_delta(spot, pe_sell_strike, sell_dte,
                                      pe_sell_raw, 'pe')

        max_theoretical_profit = compute_max_theoretical_profit(
            spot, ce_sell_strike, pe_sell_strike,
            sell_expiry_end, buy_expiry_end, entry_ts,
            ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
            ce_sell_entry, pe_sell_entry,
            ce_buy_entry,  pe_buy_entry)

        logger.info(
            f"  ENTRY {entry_date} | Spot: {spot:.0f} | "
            f"CE sell {ce_sell_strike} @ {ce_sell_entry:.1f} | "
            f"PE sell {pe_sell_strike} @ {pe_sell_entry:.1f} | "
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

        (ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
         sl_ts, sl_reason) = append_1min_snapshots_window(
            scan_start, scan_end,
            nifty_1m, vix_1m,
            ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
            ce_sell_strike, pe_sell_strike,
            ce_sell_entry, ce_buy_entry,
            pe_sell_entry, pe_buy_entry,
            total_net_debit, max_theoretical_profit,
            spot,  # entry_spot for breached side determination
            elm_time, trade_log,
            ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp)

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

        ce_pl_base = _calc_exit_pl(ce_sell_entry, ce_sell_exit,
                                    ce_buy_entry,  ce_buy_exit)
        pe_pl_base = _calc_exit_pl(pe_sell_entry, pe_sell_exit,
                                    pe_buy_entry,  pe_buy_exit)
        base_pl = round(ce_pl_base + pe_pl_base, 2)

        # Add exit snapshot to trade log
        exit_spot = get_1min_value(nifty_1m, exit_ts, 'close') or spot
        exit_vix  = get_1min_value(vix_1m,   exit_ts, 'close')
        trade_log.append(build_snapshot(
            exit_ts, exit_spot, exit_vix,
            ce_sell_strike, pe_sell_strike,
            ce_sell_exit_raw, ce_buy_exit_raw,
            pe_sell_exit_raw, pe_buy_exit_raw,
            ce_sell_entry, ce_buy_entry,
            pe_sell_entry, pe_buy_entry,
            total_net_debit, max_theoretical_profit,
            realised_pl_pts=base_pl,
            realised_pl_rs=round(base_pl * LOT_SIZE, 2),
        ))

        logger.info(
            f"  BASE EXIT {sl_reason:20s} | {exit_ts} | "
            f"P&L: {base_pl:+.1f} pts ({base_pl * LOT_SIZE:+,.0f})"
        )

        # ----------------------------------------------------------------
        # Adjustment logic
        # ----------------------------------------------------------------
        adj_made        = False
        adj_side        = None
        adj_sell_strike = None
        adj_sell_entry  = None
        adj_buy_entry   = None
        adj_sell_exit   = None
        adj_buy_exit    = None
        adj_exit_reason = None
        adj_pl_points   = None

        if (ENABLE_ADJUSTMENT
                and sl_reason in ('index_sl', 'option_sl', 'spread_sl')
                and sl_reason != 'profit_target'
                and sl_reason != 'pre_expiry'):

            # Check adjustment cutoff — based on sell expiry, not entry date.
            # Only attempt adjustment if SL fires strictly before the sell expiry day
            # at ADJUSTMENT_CUTOFF_TIME (no point re-entering on expiry day itself).
            cutoff_dt = pd.Timestamp(
                f"{sell_expiry_date} {ADJUSTMENT_CUTOFF_TIME}:00")
            adj_entry_ts = exit_ts  # adjustment enters immediately after base exit

            if adj_entry_ts < cutoff_dt:
                # Determine breached side
                exit_spot_for_adj = get_1min_value(nifty_1m, exit_ts, 'close') or spot
                adj_side = determine_breached_side(exit_spot_for_adj, spot)

                adj_opt_type = adj_side  # 'ce' or 'pe'

                # Fresh strike selection at current spot
                adj_strike, adj_sell_raw = select_strike(
                    exit_spot_for_adj, sell_expiry_end,
                    adj_entry_ts, adj_opt_type, opt_df_cache)

                if adj_strike is not None:
                    # Load buy leg for adjustment (same buy expiry)
                    adj_buy_key = (buy_expiry_end, adj_strike, adj_opt_type)
                    if adj_buy_key not in opt_df_cache:
                        opt_df_cache[adj_buy_key] = load_option_data(
                            buy_expiry_end, adj_strike, adj_opt_type)
                    adj_buy_df  = opt_df_cache[adj_buy_key]
                    adj_sell_df = opt_df_cache.get(
                        (sell_expiry_end, adj_strike, adj_opt_type),
                        load_option_data(sell_expiry_end, adj_strike, adj_opt_type))

                    adj_buy_raw = get_option_price(adj_buy_df, adj_entry_ts, 'open')

                    if adj_sell_raw is not None and adj_buy_raw is not None:
                        adj_sell_entry = apply_slippage(adj_sell_raw, is_buy=False)
                        adj_buy_entry  = apply_slippage(adj_buy_raw,  is_buy=True)
                        adj_net_prem   = round(adj_sell_entry - adj_buy_entry, 2)

                        if adj_net_prem > 0:
                            # Compute max theoretical profit for adjustment independently
                            if adj_opt_type == 'ce':
                                adj_max_profit = compute_max_theoretical_profit(
                                    exit_spot_for_adj,
                                    adj_strike, adj_strike,   # placeholder PE = CE strike
                                    sell_expiry_end, buy_expiry_end, adj_entry_ts,
                                    adj_sell_df, adj_sell_df,
                                    adj_buy_df,  adj_buy_df,
                                    adj_sell_entry, adj_sell_entry,
                                    adj_buy_entry,  adj_buy_entry)
                                # one-sided: halve the symmetric result
                                adj_max_profit = round(adj_max_profit / 2, 2)
                            else:
                                adj_max_profit = compute_max_theoretical_profit(
                                    exit_spot_for_adj,
                                    adj_strike, adj_strike,
                                    sell_expiry_end, buy_expiry_end, adj_entry_ts,
                                    adj_sell_df, adj_sell_df,
                                    adj_buy_df,  adj_buy_df,
                                    adj_sell_entry, adj_sell_entry,
                                    adj_buy_entry,  adj_buy_entry)
                                adj_max_profit = round(adj_max_profit / 2, 2)

                            adj_total_net_debit = adj_net_prem  # one-sided debit

                            logger.info(
                                f"  ADJUSTMENT {adj_opt_type.upper()} | "
                                f"Strike {adj_strike} | "
                                f"Sell @ {adj_sell_entry:.1f} | "
                                f"Buy @ {adj_buy_entry:.1f}"
                            )

                            # Scan 1-min candles for adjustment
                            adj_trade_log   = []
                            adj_sell_ltp    = adj_sell_entry
                            adj_buy_ltp     = adj_buy_entry

                            # For adjustment, we only monitor one side
                            # Reuse the two-sided scanner with same strike on both slots
                            (adj_sell_ltp, adj_buy_ltp, _, _,
                             adj_sl_ts, adj_sl_reason) = append_1min_snapshots_window(
                                adj_entry_ts, scan_end,
                                nifty_1m, vix_1m,
                                adj_sell_df, adj_sell_df,
                                adj_buy_df,  adj_buy_df,
                                adj_strike, adj_strike,
                                adj_sell_entry, adj_buy_entry,
                                adj_sell_entry, adj_buy_entry,
                                adj_total_net_debit, adj_max_profit,
                                exit_spot_for_adj,
                                elm_time, adj_trade_log,
                                adj_sell_ltp, adj_buy_ltp,
                                adj_sell_ltp, adj_buy_ltp)

                            if adj_sl_ts is None:
                                adj_sl_ts     = scan_end
                                adj_sl_reason = 'pre_expiry'

                            # Exit adjustment
                            if adj_sl_reason == 'pre_expiry':
                                adj_exit_ts  = elm_time if elm_time is not None else scan_end
                                adj_use_col  = 'close'
                                adj_slip     = False
                            else:
                                adj_exit_ts  = adj_sl_ts + pd.Timedelta(minutes=1)
                                adj_use_col  = 'open'
                                adj_slip     = True

                            def get_adj_exit(opt_df, ltp_fb, is_buy):
                                raw = get_option_price(opt_df, adj_exit_ts, adj_use_col) or ltp_fb
                                if adj_slip:
                                    return apply_slippage(raw, is_buy=is_buy), raw
                                return raw, raw

                            adj_sell_exit, _ = get_adj_exit(
                                adj_sell_df, adj_sell_ltp, is_buy=True)
                            adj_buy_exit, _  = get_adj_exit(
                                adj_buy_df,  adj_buy_ltp,  is_buy=False)

                            adj_pl_points = _calc_exit_pl(
                                adj_sell_entry, adj_sell_exit,
                                adj_buy_entry,  adj_buy_exit)
                            adj_exit_reason = adj_sl_reason
                            adj_made        = True
                            adj_sell_strike = adj_strike

                            # Append adjustment log to main trade log
                            trade_log.extend(adj_trade_log)

                            logger.info(
                                f"  ADJ EXIT {adj_exit_reason:20s} | {adj_exit_ts} | "
                                f"P&L: {adj_pl_points:+.1f} pts"
                            )

        # ----------------------------------------------------------------
        # Build final trade record
        # ----------------------------------------------------------------
        total_pl = round(base_pl + (adj_pl_points or 0.0), 2)
        trade_counter += 1

        record = build_trade_record(
            entry_ts, spot, entry_vix,
            sell_expiry_date, buy_expiry_date,
            ce_sell_strike, pe_sell_strike,
            ce_sell_entry, ce_buy_entry,
            pe_sell_entry, pe_buy_entry,
            ce_sell_delta, pe_sell_delta,
            net_debit_ce, net_debit_pe,
            max_theoretical_profit,
            exit_ts, sl_reason,
            ce_sell_exit, ce_buy_exit,
            pe_sell_exit, pe_buy_exit,
            adjustment_made=adj_made,
            adj_side=adj_side,
            adj_sell_strike=adj_sell_strike,
            adj_sell_entry=adj_sell_entry,
            adj_buy_entry=adj_buy_entry,
            adj_sell_exit=adj_sell_exit,
            adj_buy_exit=adj_buy_exit,
            adj_exit_reason=adj_exit_reason,
            adj_pl_points=adj_pl_points,
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
    logger.info(f"  Delta target : {DELTA_TARGET}")
    logger.info(f"  Buy min DTE  : {BUY_LEG_MIN_DTE}")
    logger.info(f"  Index SL     : {'ON' if ENABLE_INDEX_SL else 'OFF'} "
                f"({INDEX_SL_OFFSET} pts)")
    logger.info(f"  Option SL    : {'ON' if ENABLE_OPTION_SL else 'OFF'} "
                f"({OPTION_SL_MULTIPLIER}x entry)")
    logger.info(f"  Spread SL    : {'ON' if ENABLE_SPREAD_SL else 'OFF'} "
                f"({SPREAD_SL_PCT * 100:.0f}% of net debit)")
    logger.info(f"  Profit target: {'ON' if ENABLE_PROFIT_TARGET else 'OFF'} "
                f"({PROFIT_TARGET_PCT * 100:.0f}% of max theoretical)")
    logger.info(f"  Adjustment   : {'ON' if ENABLE_ADJUSTMENT else 'OFF'}")

    all_trades = run_backtest(nifty_1m, vix_1m, contracts_df, holidays_df)
    save_trade_summary(all_trades)

    logger.info("=== Athena Backtest complete ===")