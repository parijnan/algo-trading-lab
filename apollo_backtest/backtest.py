"""
backtest.py — Apollo Backtest Engine
Dual-timeframe Supertrend trend-following credit spread strategy.
Deployed only when India VIX > VIX_THRESHOLD.

Run precompute.py first to generate intermediate files.

Execution model:
  - Signal fires on candle CLOSE
  - Entry/exit executes at OPEN of the next candle
  - Slippage applied per leg at entry and exit
  - Per-trade 1-min log captures a snapshot every minute while in trade
"""

import os
import sys
import logging
import warnings
import pandas as pd
import numpy as np
import mibian

sys.path.insert(0, os.path.dirname(__file__))

from configs import (
    NIFTY_INDEX_FILE, VIX_INDEX_FILE,
    NIFTY_OPTIONS_PATH, CONTRACT_LIST_FILE,
    NIFTY_15MIN_FILE, NIFTY_75MIN_FILE, VIX_DAILY_FILE,
    TRADE_LOGS_DIR, TRADE_SUMMARY_FILE,
    VIX_THRESHOLD,
    TARGET_DELTA, HEDGE_POINTS, STRIKE_STEP, MIN_DTE,
    INDEX_SL_OFFSET, NO_EXIT_BEFORE,
    ENABLE_INDEX_SL, ENABLE_OPTION_SL, ENABLE_SPREAD_SL, ENABLE_TRAILING_SL,
    OPTION_SL_MULTIPLIERS, OPTION_SL_FLOOR_MULT,
    SPREAD_SL_PCTS, SPREAD_SL_FLOOR_PCT,
    TRAILING_SL_TRIGGER_1, TRAILING_SL_FLOOR_1,
    TRAILING_SL_TRIGGER_2, TRAILING_SL_FLOOR_2,
    TRAILING_SL_TRIGGER_3, TRAILING_SL_FLOOR_3,
    ADDITIONAL_LOT_MULTIPLIER, ELM_SECONDS_BEFORE_EXPIRY,
    SLIPPAGE_POINTS, LOT_SIZE, RISK_FREE_RATE,
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
# Data loading
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
    """
    Load raw 1-min Nifty spot and VIX data for per-trade logging.
    Strips timezone info and filters to market hours.
    """
    logger.info("Loading 1-min index data for trade logging...")

    nifty_1m = pd.read_csv(NIFTY_INDEX_FILE, parse_dates=['time_stamp'])
    nifty_1m['time_stamp'] = pd.to_datetime(
        nifty_1m['time_stamp'], utc=False).dt.tz_localize(None)

    vix_1m = pd.read_csv(VIX_INDEX_FILE, parse_dates=['time_stamp'])
    vix_1m['time_stamp'] = pd.to_datetime(
        vix_1m['time_stamp'], utc=False).dt.tz_localize(None)

    # Apply backtest date range
    if BACKTEST_START_DATE:
        nifty_1m = nifty_1m[nifty_1m['time_stamp'] >= pd.Timestamp(BACKTEST_START_DATE)]
        vix_1m   = vix_1m[vix_1m['time_stamp']     >= pd.Timestamp(BACKTEST_START_DATE)]
    if BACKTEST_END_DATE:
        nifty_1m = nifty_1m[nifty_1m['time_stamp'] <= pd.Timestamp(BACKTEST_END_DATE)]
        vix_1m   = vix_1m[vix_1m['time_stamp']     <= pd.Timestamp(BACKTEST_END_DATE)]

    # Index by timestamp for fast lookups
    nifty_1m = nifty_1m.set_index('time_stamp').sort_index()
    vix_1m   = vix_1m.set_index('time_stamp').sort_index()

    logger.info(f"  1-min Nifty: {len(nifty_1m):,} rows")
    logger.info(f"  1-min VIX  : {len(vix_1m):,} rows")
    return nifty_1m, vix_1m


def load_contracts(holidays_df: pd.DataFrame = None):
    """
    Load Nifty weekly expiry contract list.
    Uses 'end_date' as the accurate expiry timestamp (15:30 on expiry day).
    Also computes elm_time for each expiry:
    elm_time = end_date - ELM_SECONDS_BEFORE_EXPIRY (Monday 15:15 for Tuesday expiry)
    Adjusted back if the preceding day is a market holiday.
    """
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
    Falls back to last available price before timestamp if not found.
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
    Get a value from a timestamp-indexed 1-min DataFrame.
    Falls back to the last available value before timestamp.
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
# Expiry selection
# ---------------------------------------------------------------------------

def get_expiry(signal_time: pd.Timestamp,
               contracts_df: pd.DataFrame) -> pd.Timestamp:
    """
    Select appropriate expiry:
    - Use current weekly expiry if DTE >= MIN_DTE (calendar days)
    - Roll to next weekly expiry if DTE < MIN_DTE

    Returns end_date (accurate 15:30 expiry timestamp) for use as the
    expiry value throughout the backtest. expiry_date is used only for
    DTE calculation and folder name lookup (date portion only).
    """
    signal_date = signal_time.date()
    future = contracts_df[
        contracts_df['expiry_date'].dt.date >= signal_date
    ]

    if future.empty:
        return None

    current_row = future.iloc[0]
    dte = (current_row['expiry_date'].date() - signal_date).days

    if dte >= MIN_DTE:
        return current_row['end_date']
    elif len(future) > 1:
        return future.iloc[1]['end_date']
    return None


# ---------------------------------------------------------------------------
# Strike selection using mibian (Black-Scholes delta)
# ---------------------------------------------------------------------------

def compute_delta(spot: float, strike: int, dte_days: float,
                  option_price: float, option_type: str) -> float:
    """
    Back out IV from market price using mibian, then compute delta.
    Returns absolute delta value, or None if computation fails.
    """
    try:
        if option_type == 'ce':
            implied = mibian.BS(
                [spot, strike, RISK_FREE_RATE, dte_days],
                callPrice=option_price)
        else:
            implied = mibian.BS(
                [spot, strike, RISK_FREE_RATE, dte_days],
                putPrice=option_price)
        iv = implied.impliedVolatility
        if iv is None or iv <= 0 or iv > 500:
            return None
        bs    = mibian.BS([spot, strike, RISK_FREE_RATE, dte_days], volatility=iv)
        delta = abs(bs.callDelta if option_type == 'ce' else bs.putDelta)
        return delta
    except Exception:
        return None


def select_strike(spot: float, expiry: pd.Timestamp,
                  exec_ts: pd.Timestamp, direction: str,
                  option_df_cache: dict) -> tuple:
    """
    Scan OTM strikes and find the one with the highest delta <= TARGET_DELTA.
    Returns (strike, option_type, entry_price) or (None, None, None).
    """
    option_type = 'ce' if direction == 'bearish' else 'pe'
    dte_days    = max((expiry.date() - exec_ts.date()).days, 0.5)

    if direction == 'bearish':
        atm        = int(np.ceil(spot / STRIKE_STEP) * STRIKE_STEP)
        candidates = range(atm, atm + 3000, STRIKE_STEP)
    else:
        atm        = int(np.floor(spot / STRIKE_STEP) * STRIKE_STEP)
        candidates = range(atm, atm - 3000, -STRIKE_STEP)

    for strike in candidates:
        cache_key = (expiry, strike, option_type)
        if cache_key not in option_df_cache:
            option_df_cache[cache_key] = load_option_data(
                expiry, strike, option_type)

        opt_df = option_df_cache[cache_key]
        price  = get_option_price(opt_df, exec_ts, price_col='open')

        if price is None or price <= 0.5:
            continue

        delta = compute_delta(spot, strike, dte_days, price, option_type)
        if delta is None:
            continue

        if delta <= TARGET_DELTA:
            return strike, option_type, price

    return None, None, None


# ---------------------------------------------------------------------------
# Stop loss computation
# ---------------------------------------------------------------------------

def get_option_sl_level(sell_entry: float, days_in_trade: int) -> float:
    """
    Compute the option SL threshold for the sold option.
    Returns the sell_ltp level at which the option SL fires.
    Exit if sell_ltp >= returned value.

    Multiplier steps down each calendar day in trade.
    Uses OPTION_SL_MULTIPLIERS list (index = days_in_trade).
    Floors at OPTION_SL_FLOOR_MULT from Day len(OPTION_SL_MULTIPLIERS) onwards.
    """
    if days_in_trade < len(OPTION_SL_MULTIPLIERS):
        mult = OPTION_SL_MULTIPLIERS[days_in_trade]
    else:
        mult = OPTION_SL_FLOOR_MULT
    return sell_entry * mult


def get_spread_sl_level(net_credit: float, days_in_trade: int) -> float:
    """
    Compute the spread SL threshold as a P&L floor.
    Returns the unrealised_pl_pts level at which the spread SL fires.
    Exit if unrealised_pl_pts <= returned value (which is <= 0).

    Pct steps down each calendar day in trade.
    Uses SPREAD_SL_PCTS list (index = days_in_trade).
    Floors at SPREAD_SL_FLOOR_PCT from Day len(SPREAD_SL_PCTS) onwards.
    """
    if days_in_trade < len(SPREAD_SL_PCTS):
        pct = SPREAD_SL_PCTS[days_in_trade]
    else:
        pct = SPREAD_SL_FLOOR_PCT
    return -net_credit * pct


def update_trailing_sl(trailing_sl_floor: float,
                       unrealised_pl: float,
                       net_credit: float) -> float:
    """
    Update the trailing SL floor based on current unrealised P&L.
    Returns the new (or unchanged) trailing_sl_floor.
    Floor is a ratchet — only ever moves up, never reverts.
    Returns None if trailing SL has not yet been activated.

    Stage 1: activates at TRAILING_SL_TRIGGER_1 * net_credit
             floor = TRAILING_SL_FLOOR_1 * net_credit
    Stage 2: upgrades at TRAILING_SL_TRIGGER_2 * net_credit
             floor = TRAILING_SL_FLOOR_2 * net_credit
    Stage 3: upgrades at TRAILING_SL_TRIGGER_3 * net_credit
             floor = TRAILING_SL_FLOOR_3 * net_credit
    """
    new_floor = trailing_sl_floor  # None if not yet activated

    if unrealised_pl >= net_credit * TRAILING_SL_TRIGGER_3:
        candidate = net_credit * TRAILING_SL_FLOOR_3
    elif unrealised_pl >= net_credit * TRAILING_SL_TRIGGER_2:
        candidate = net_credit * TRAILING_SL_FLOOR_2
    elif unrealised_pl >= net_credit * TRAILING_SL_TRIGGER_1:
        candidate = net_credit * TRAILING_SL_FLOOR_1
    else:
        candidate = None

    if candidate is not None:
        # Ratchet: only move the floor up, never down
        if new_floor is None or candidate > new_floor:
            new_floor = candidate

    return new_floor


# ---------------------------------------------------------------------------
# Stop loss checks
# ---------------------------------------------------------------------------

def check_stop_losses(spot: float, sell_strike: int, direction: str,
                      sell_ltp: float, sell_entry: float,
                      buy_ltp: float, buy_entry: float,
                      days_in_trade: int = 0,
                      trailing_sl_floor: float = None) -> str:
    """
    Check all four SL conditions concurrently.
    Returns the triggered SL type string, or None if none triggered.
    Each SL can be individually disabled via ENABLE_* toggles in configs.

    1. index_sl    — spot within INDEX_SL_OFFSET of sell strike
    2. option_sl   — sell_ltp >= sell_entry * day-adjusted multiplier
    3. spread_sl   — unrealised_pl <= -net_credit * day-adjusted pct
    4. trailing_sl — unrealised_pl <= trailing_sl_floor (once activated)
    """
    net_credit    = sell_entry - buy_entry
    unrealised_pl = (sell_entry - sell_ltp) + (buy_ltp - buy_entry)

    # 1. Index SL
    if ENABLE_INDEX_SL:
        if direction == 'bearish' and spot >= sell_strike - INDEX_SL_OFFSET:
            return 'index_sl'
        if direction == 'bullish' and spot <= sell_strike + INDEX_SL_OFFSET:
            return 'index_sl'

    # 2. Option SL
    if ENABLE_OPTION_SL:
        option_sl_level = get_option_sl_level(sell_entry, days_in_trade)
        if sell_ltp >= option_sl_level:
            return 'option_sl'

    # 3. Spread SL
    if ENABLE_SPREAD_SL:
        spread_sl_level = get_spread_sl_level(net_credit, days_in_trade)
        if unrealised_pl <= spread_sl_level:
            return 'spread_sl'

    # 4. Trailing SL
    if ENABLE_TRAILING_SL and trailing_sl_floor is not None:
        if unrealised_pl <= trailing_sl_floor:
            return 'trailing_sl'

    return None


# ---------------------------------------------------------------------------
# Slippage
# ---------------------------------------------------------------------------

def apply_slippage(price: float, is_buy: bool) -> float:
    """Add slippage to buys, subtract from sells. Floor at 0 — options can't be negative."""
    return (price + SLIPPAGE_POINTS) if is_buy else max(price - SLIPPAGE_POINTS, 0.0)


# ---------------------------------------------------------------------------
# Per-trade 1-min snapshot
# ---------------------------------------------------------------------------

def _build_snapshot(ts: pd.Timestamp, spot: float, vix: float,
                    sell_strike: int, buy_strike: int, option_type: str,
                    sell_ltp: float, buy_ltp: float,
                    sell_entry: float, buy_entry: float,
                    direction: str, trend_75, trend_15,
                    expiry: pd.Timestamp,
                    days_in_trade: int = 0,
                    trailing_sl_floor: float = None,
                    realised_pl_pts: float = None,
                    realised_pl_rs:  float = None) -> dict:
    """
    Build a single 1-min snapshot row for the per-trade log.
    Captures everything needed to analyse trade behaviour post-hoc.

    realised_pl_pts / realised_pl_rs are only populated on the final
    (exit) row — they reflect slippage-adjusted P&L matching the trade
    summary. All other rows have None for these columns.
    unrealised_pl_pts reflects mark-to-market value without slippage.
    """
    net_credit    = sell_entry - buy_entry
    unrealised_pl = _calc_pl(sell_entry, sell_ltp, buy_entry, buy_ltp)

    # SL reference levels for the log
    if direction == 'bearish':
        index_sl_level = sell_strike - INDEX_SL_OFFSET
    else:
        index_sl_level = sell_strike + INDEX_SL_OFFSET

    option_sl_level = round(get_option_sl_level(sell_entry, days_in_trade), 2)
    spread_sl_level = round(get_spread_sl_level(net_credit, days_in_trade), 2)
    trailing_sl_val = round(trailing_sl_floor, 2) if trailing_sl_floor is not None else None

    dte = (expiry.date() - ts.date()).days

    return {
        'time_stamp':        ts,
        'spot':              round(spot, 2),
        'vix':               round(vix, 2) if vix is not None else None,
        'sell_strike':       sell_strike,
        'buy_strike':        buy_strike,
        'option_type':       option_type,
        'sell_ltp':          round(sell_ltp, 2),
        'buy_ltp':           round(buy_ltp,  2),
        'sell_entry':        round(sell_entry, 2),
        'buy_entry':         round(buy_entry,  2),
        'unrealised_pl_pts': round(unrealised_pl, 2),
        'unrealised_pl_rs':  round(unrealised_pl * LOT_SIZE, 2),
        'realised_pl_pts':   round(realised_pl_pts, 2) if realised_pl_pts is not None else None,
        'realised_pl_rs':    round(realised_pl_rs,  2) if realised_pl_rs  is not None else None,
        'index_sl_level':    index_sl_level,
        'option_sl_level':   option_sl_level,
        'spread_sl_level':   spread_sl_level,
        'trailing_sl_level': trailing_sl_val,
        'trend_75':          trend_75,
        'trend_15':          trend_15,
        'dte':               dte,
    }


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------

def run_backtest(nifty_15: pd.DataFrame, nifty_75: pd.DataFrame,
                 vix_daily: pd.DataFrame, contracts_df: pd.DataFrame,
                 nifty_1m: pd.DataFrame, vix_1m: pd.DataFrame,
                 holidays_df: pd.DataFrame = None):
    """
    Main backtest loop.
    Iterates through all 15-min candles on high-VIX days,
    applies dual Supertrend signal logic, manages entries/exits/re-entries.
    Builds a 1-min per-trade log for every active trade.
    """
    os.makedirs(TRADE_LOGS_DIR, exist_ok=True)

    # Index 75-min by timestamp for fast lookups
    nifty_75_indexed = nifty_75.set_index('time_stamp').sort_index()

    # High-VIX dates
    high_vix_dates = set(
        vix_daily[vix_daily['vix_open'] > VIX_THRESHOLD]['date'])
    logger.info(f"High-VIX days (VIX > {VIX_THRESHOLD}): {len(high_vix_dates)}")

    all_trades      = []
    option_df_cache = {}
    trade_counter   = 0   # used to generate unique trade log filenames

    # Prepare 15-min data
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
    sl_1min_ts   = None   # 1-min SL hit timestamp from snapshot scan
    sl_1min_type = None   # 1-min SL hit type from snapshot scan

    # ------------------------------------------------------------------
    # Trade state — persists across days
    # ------------------------------------------------------------------
    in_trade       = False
    direction      = None
    sell_strike    = None
    buy_strike     = None
    option_type    = None
    expiry         = None
    elm_time       = None   # ELM exit time for additional lots
    sell_entry     = None
    buy_entry      = None
    sell_ltp       = None
    buy_ltp        = None
    entry_time     = None
    entry_spot     = None
    entry_vix      = None
    sell_opt_df    = None
    buy_opt_df     = None
    trade_log      = []
    snap_sell_ltp  = None
    snap_buy_ltp   = None
    entry_exec_ts  = None
    # Additional lots state (same strikes, half quantity)
    has_additional = False
    add_sell_entry = None
    add_buy_entry  = None
    add_sell_ltp   = None
    add_buy_ltp    = None
    add_booked_pl  = 0.0
    # SL tracking
    trailing_sl_floor  = None   # None = not yet activated; ratchet, only moves up
    trade_entry_date   = None

    for day_date in trading_days:

        # ------------------------------------------------------------------
        # Close any trade where expiry has passed without an explicit exit
        # ------------------------------------------------------------------
        if in_trade and expiry is not None and day_date > expiry.date():
            _sell_exp = get_option_price(sell_opt_df, expiry, 'close') or sell_ltp
            _buy_exp  = get_option_price(buy_opt_df,  expiry, 'close') or buy_ltp
            # No slippage on expiry — options expire at their last traded price
            pl_points = _calc_pl(sell_entry, _sell_exp, buy_entry, _buy_exp)
            pl_points = round(pl_points + add_booked_pl * ADDITIONAL_LOT_MULTIPLIER, 2)
            pl_rupees = pl_points * LOT_SIZE
            expiry_exit_vix  = get_1min_value(vix_1m,   expiry, 'close')
            expiry_exit_spot = get_1min_value(nifty_1m, expiry, 'close') or entry_spot
            trade_stats = _compute_trade_stats(trade_log)
            trade_record = _build_trade_record(
                entry_time, expiry, direction, expiry,
                sell_strike, buy_strike, option_type,
                entry_spot, expiry_exit_spot,
                sell_entry, buy_entry,
                _sell_exp, _buy_exp,
                pl_points, pl_rupees, 'expiry',
                trade_stats=trade_stats,
                entry_vix=entry_vix, exit_vix=expiry_exit_vix
            )
            trade_counter += 1
            all_trades.append(trade_record)
            _log_exit(trade_record)
            _save_trade_log(trade_counter, entry_time, trade_log)
            in_trade          = False
            trade_log         = []
            has_additional    = False
            add_booked_pl     = 0.0

        # ------------------------------------------------------------------
        # Build 1-min log for any active trade on non-high-VIX days
        # (trade may have been entered on a high-VIX day and is still open)
        # ------------------------------------------------------------------
        if in_trade and day_date not in high_vix_dates:
            snap_sell_ltp, snap_buy_ltp, sl_1min_ts, sl_1min_type, trailing_sl_floor = \
                _append_1min_snapshots(
                    day_date, nifty_1m, vix_1m,
                    nifty_75_indexed, nifty_15,
                    sell_opt_df, buy_opt_df,
                    sell_strike, buy_strike, option_type,
                    sell_entry, buy_entry, direction, expiry,
                    trade_log,
                    snap_sell_ltp, snap_buy_ltp,
                    trailing_sl_floor, trade_entry_date
                )
            # If a 1-min SL was hit on this overnight day, exit immediately.
            # exec price = open of sl_hit_ts + 1 minute
            if sl_1min_ts is not None and in_trade:
                _sl_exec_ts  = sl_1min_ts + pd.Timedelta(minutes=1)
                _sell_sl_raw = get_option_price(sell_opt_df, _sl_exec_ts, 'open') or snap_sell_ltp
                _buy_sl_raw  = get_option_price(buy_opt_df,  _sl_exec_ts, 'open') or snap_buy_ltp
                _sell_sl_net = apply_slippage(_sell_sl_raw, is_buy=True)
                _buy_sl_net  = apply_slippage(_buy_sl_raw,  is_buy=False)
                _base_pl     = _calc_pl(sell_entry, _sell_sl_net, buy_entry, _buy_sl_net)
                if has_additional:
                    _add_sell_raw = get_option_price(sell_opt_df, _sl_exec_ts, 'open') or snap_sell_ltp
                    _add_buy_raw  = get_option_price(buy_opt_df,  _sl_exec_ts, 'open') or snap_buy_ltp
                    add_booked_pl = _calc_pl(
                        add_sell_entry, apply_slippage(_add_sell_raw, is_buy=True),
                        add_buy_entry,  apply_slippage(_add_buy_raw,  is_buy=False))
                    has_additional = False
                _pl_pts = round(_base_pl + add_booked_pl * ADDITIONAL_LOT_MULTIPLIER, 2)
                _pl_rs  = _pl_pts * LOT_SIZE
                _exit_spot = get_1min_value(nifty_1m, _sl_exec_ts, 'close') or entry_spot
                _exit_vix  = get_1min_value(vix_1m,   _sl_exec_ts, 'close')
                # Add exit snapshot to trade log
                _prior_75 = nifty_75_indexed[nifty_75_indexed.index <= _sl_exec_ts]
                _t75 = _prior_75.iloc[-1]['trend'] if not _prior_75.empty else None
                _prior_15 = nifty_15[nifty_15['time_stamp'] <= _sl_exec_ts]
                _t15 = _prior_15.iloc[-1]['trend'] if not _prior_15.empty else None
                _exit_days = (_sl_exec_ts.date() - trade_entry_date).days if trade_entry_date else 0
                trade_log.append(_build_snapshot(
                    _sl_exec_ts, _exit_spot, _exit_vix,
                    sell_strike, buy_strike, option_type,
                    _sell_sl_raw, _buy_sl_raw, sell_entry, buy_entry,
                    direction, _t75, _t15, expiry,
                    days_in_trade=_exit_days,
                    trailing_sl_floor=trailing_sl_floor,
                    realised_pl_pts=_pl_pts, realised_pl_rs=_pl_rs))
                trade_stats = _compute_trade_stats(trade_log)
                trade_record = _build_trade_record(
                    entry_time, _sl_exec_ts, direction, expiry,
                    sell_strike, buy_strike, option_type,
                    entry_spot, _exit_spot,
                    sell_entry, buy_entry,
                    _sell_sl_net, _buy_sl_net,
                    _pl_pts, _pl_rs, sl_1min_type,
                    trade_stats=trade_stats,
                    entry_vix=entry_vix, exit_vix=_exit_vix)
                trade_counter += 1
                all_trades.append(trade_record)
                _log_exit(trade_record)
                _save_trade_log(trade_counter, entry_time, trade_log)
                in_trade = False; trade_log = []
                direction = None; sell_strike = None; buy_strike = None
                option_type = None; expiry = None; elm_time = None
                sell_entry = None; buy_entry = None
                sell_opt_df = None; buy_opt_df = None
                snap_sell_ltp = None; snap_buy_ltp = None
                entry_exec_ts = None; entry_vix = None
                has_additional = False; add_sell_entry = None
                add_buy_entry = None; add_sell_ltp = None
                add_buy_ltp = None; add_booked_pl = 0.0
                trailing_sl_floor = None; trade_entry_date = None
                sl_1min_ts = None; sl_1min_type = None
                trade_counter += 0  # counter already incremented above
            continue

        if day_date not in high_vix_dates:
            continue

        day_15 = nifty_15[nifty_15['date'] == day_date].reset_index(drop=True)
        if day_15.empty:
            continue

        # Market open anchor for this day — used to capture 09:15 on first candle
        day_open_anchor = pd.Timestamp(f"{day_date} 09:14:00")
        last_15min_ts   = None   # tracks last processed 15-min candle timestamp

        for idx in range(len(day_15)):
            row      = day_15.iloc[idx]
            ts       = row['time_stamp']
            spot     = row['close']
            trend_15 = row['trend']
            flip_15  = row['trend_flip']

            # Current 75-min trend
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
                # Window runs from end of previous 15-min bar to current bar.
                # On first candle of the day (idx==0), use market open anchor
                # so 09:15 itself is included.
                prev_ts = last_15min_ts if last_15min_ts is not None else day_open_anchor
                # Clamp to entry execution timestamp on the FIRST window only —
                # prevents look-ahead bias (minutes inside signal candle appearing
                # in trade log). Cleared immediately after use so subsequent days
                # are not affected.
                if entry_exec_ts is not None and prev_ts < entry_exec_ts - pd.Timedelta(minutes=1):
                    prev_ts = entry_exec_ts - pd.Timedelta(minutes=1)
                entry_exec_ts = None   # clamp used — clear so it doesn't affect future windows
                snap_sell_ltp, snap_buy_ltp, sl_1min_ts, sl_1min_type, trailing_sl_floor = \
                    _append_1min_snapshots_window(
                        prev_ts, ts, nifty_1m, vix_1m,
                        nifty_75_indexed, nifty_15,
                        sell_opt_df, buy_opt_df,
                        sell_strike, buy_strike, option_type,
                        sell_entry, buy_entry, direction, expiry,
                        trade_log,
                        snap_sell_ltp, snap_buy_ltp,
                        trailing_sl_floor, trade_entry_date
                    )


            last_15min_ts = ts

            # --------------------------------------------------------------
            # Monitor open trade for exits
            # --------------------------------------------------------------
            if in_trade:
                sell_ltp = get_option_price(
                    sell_opt_df, ts, 'close') or sell_ltp
                buy_ltp  = get_option_price(
                    buy_opt_df,  ts, 'close') or buy_ltp
                if has_additional:
                    add_sell_ltp = get_option_price(
                        sell_opt_df, ts, 'close') or add_sell_ltp
                    add_buy_ltp  = get_option_price(
                        buy_opt_df,  ts, 'close') or add_buy_ltp

                # ----------------------------------------------------------
                # ELM: exit additional lots at elm_time (Monday 15:15)
                # Base position continues with unchanged rules after this
                # ----------------------------------------------------------
                if has_additional and elm_time is not None and ts >= elm_time:
                    add_sell_exit_raw = get_option_price(
                        sell_opt_df, exec_ts, exec_col) or add_sell_ltp
                    add_buy_exit_raw  = get_option_price(
                        buy_opt_df,  exec_ts, exec_col) or add_buy_ltp
                    add_sell_exit_net = apply_slippage(add_sell_exit_raw, is_buy=True)
                    add_buy_exit_net  = apply_slippage(add_buy_exit_raw,  is_buy=False)
                    add_booked_pl     = _calc_pl(
                        add_sell_entry, add_sell_exit_net,
                        add_buy_entry,  add_buy_exit_net)
                    has_additional    = False
                    logger.info(
                        f"  ELM   additional exit | {exec_ts} | "
                        f"Add P&L: {add_booked_pl:+.2f} pts"
                    )

                exit_reason = None

                # 1-min SL detected during the preceding snapshot window scan
                # This fires immediately without waiting for 15-min candle close
                if sl_1min_ts is not None:

                    exit_reason = sl_1min_type
                    # Update LTPs to the values at the SL hit timestamp
                    sell_ltp = snap_sell_ltp
                    buy_ltp  = snap_buy_ltp
                    # Use SL hit timestamp for execution (next 1-min open)
                    sl_exec_ts  = sl_1min_ts + pd.Timedelta(minutes=1)
                    exec_ts     = sl_exec_ts
                    exec_col    = 'open'
                    sl_1min_ts  = None   # consumed

                if exit_reason is None:
                    if flip_15:
                        if (direction == 'bullish' and trend_15 == False) or \
                           (direction == 'bearish' and trend_15 == True):
                            exit_reason = 'trend_flip_15'

                    # 15-min SL check as fallback (with 9:15 guard)
                    if exit_reason is None:
                        days_in_trade_15 = (ts.date() - trade_entry_date).days if trade_entry_date else 0
                        sl = check_stop_losses(
                            spot, sell_strike, direction,
                            sell_ltp, sell_entry, buy_ltp, buy_entry,
                            days_in_trade=days_in_trade_15,
                            trailing_sl_floor=trailing_sl_floor)
                        if sl:
                            if ts.time() < pd.Timestamp(NO_EXIT_BEFORE).time():
                                # SL fired at 09:15 close — recheck at 09:16 close.
                                # If still valid, exit at 09:16 OPEN (not next 15-min open).
                                recheck_ts       = pd.Timestamp(f"{ts.date()} {NO_EXIT_BEFORE}:00")
                                recheck_spot     = get_1min_value(nifty_1m, recheck_ts, 'close')
                                recheck_sell_ltp = get_option_price(
                                    sell_opt_df, recheck_ts, 'close') or sell_ltp
                                recheck_buy_ltp  = get_option_price(
                                    buy_opt_df,  recheck_ts, 'close') or buy_ltp
                                if recheck_spot is not None:
                                    sl_recheck = check_stop_losses(
                                        recheck_spot, sell_strike, direction,
                                        recheck_sell_ltp, sell_entry,
                                        recheck_buy_ltp,  buy_entry,
                                        days_in_trade=days_in_trade_15,
                                        trailing_sl_floor=trailing_sl_floor)
                                    if sl_recheck:
                                        exit_reason = sl_recheck
                                        exec_ts  = recheck_ts  # exit at 09:16 open
                                        exec_col = 'open'
                                        sell_ltp = recheck_sell_ltp
                                        buy_ltp  = recheck_buy_ltp
                            else:
                                exit_reason = sl

                if expiry is not None and ts.date() >= expiry.date() \
                        and ts.time() >= pd.Timestamp('15:15').time():
                    exit_reason = 'expiry'
                    exec_ts  = expiry   # always exit at actual expiry timestamp
                    exec_col = 'close'  # use close price at expiry

                if exit_reason:
                    # Step 1: Fill the gap between signal candle and execution candle.
                    if ts < exec_ts:
                        _gf_sell_ltp, _gf_buy_ltp, _gf_sl_ts, _gf_sl_type, trailing_sl_floor = \
                            _append_1min_snapshots_window(
                                ts, exec_ts - pd.Timedelta(minutes=1),
                                nifty_1m, vix_1m, nifty_75_indexed, nifty_15,
                                sell_opt_df, buy_opt_df,
                                sell_strike, buy_strike, option_type,
                                sell_entry, buy_entry, direction, expiry,
                                trade_log,
                                snap_sell_ltp, snap_buy_ltp,
                                trailing_sl_floor, trade_entry_date
                            )
                        snap_sell_ltp = _gf_sell_ltp
                        snap_buy_ltp  = _gf_buy_ltp
                        if _gf_sl_ts is not None:
                            exec_ts     = _gf_sl_ts + pd.Timedelta(minutes=1)
                            exec_col    = 'open'
                            exit_reason = _gf_sl_type
                            sell_ltp    = snap_sell_ltp
                            buy_ltp     = snap_buy_ltp

                    # Step 2: Fetch exit prices
                    sell_exit_raw = get_option_price(
                        sell_opt_df, exec_ts, exec_col) or sell_ltp
                    buy_exit_raw  = get_option_price(
                        buy_opt_df,  exec_ts, exec_col) or buy_ltp

                    # No slippage on expiry exits — options expire at their last traded price
                    if exit_reason == 'expiry':
                        sell_exit_net = sell_exit_raw
                        buy_exit_net  = buy_exit_raw
                    else:
                        sell_exit_net = apply_slippage(sell_exit_raw, is_buy=True)
                        buy_exit_net  = apply_slippage(buy_exit_raw,  is_buy=False)

                    base_pl = _calc_pl(sell_entry, sell_exit_net, buy_entry, buy_exit_net)

                    # Step 3: Exit additional lots simultaneously if still active
                    if has_additional:
                        add_sell_exit_raw = get_option_price(
                            sell_opt_df, exec_ts, exec_col) or add_sell_ltp
                        add_buy_exit_raw  = get_option_price(
                            buy_opt_df,  exec_ts, exec_col) or add_buy_ltp
                        if exit_reason == 'expiry':
                            add_sell_exit_net = add_sell_exit_raw
                            add_buy_exit_net  = add_buy_exit_raw
                        else:
                            add_sell_exit_net = apply_slippage(add_sell_exit_raw, is_buy=True)
                            add_buy_exit_net  = apply_slippage(add_buy_exit_raw,  is_buy=False)
                        add_booked_pl  = _calc_pl(
                            add_sell_entry, add_sell_exit_net,
                            add_buy_entry,  add_buy_exit_net)
                        has_additional = False

                    # Step 4: Normalised P&L per unit of capital
                    pl_points = round(
                        base_pl + add_booked_pl * ADDITIONAL_LOT_MULTIPLIER, 2)
                    pl_rupees = pl_points * LOT_SIZE

                    # Capture the execution candle in the log
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
                        sell_strike, buy_strike, option_type,
                        sell_exit_raw, buy_exit_raw,
                        sell_entry, buy_entry,
                        direction, trend_75_exec, trend_15_exec, expiry,
                        days_in_trade=_exit_days,
                        trailing_sl_floor=trailing_sl_floor,
                        realised_pl_pts=pl_points,
                        realised_pl_rs=pl_rupees,
                    )
                    trade_log.append(exit_snapshot)

                    exit_vix = get_1min_value(vix_1m, exec_ts, 'close')
                    trade_stats = _compute_trade_stats(trade_log)
                    trade_record = _build_trade_record(
                        entry_time, exec_ts, direction, expiry,
                        sell_strike, buy_strike, option_type,
                        entry_spot, spot,
                        sell_entry, buy_entry,
                        sell_exit_net, buy_exit_net,
                        pl_points, pl_rupees, exit_reason,
                        trade_stats=trade_stats,
                        entry_vix=entry_vix, exit_vix=exit_vix
                    )
                    trade_counter += 1
                    all_trades.append(trade_record)
                    _log_exit(trade_record)
                    _save_trade_log(trade_counter, entry_time, trade_log)

                    in_trade       = False
                    trade_log      = []
                    direction      = None
                    sell_strike    = None
                    buy_strike     = None
                    option_type    = None
                    expiry         = None
                    elm_time       = None
                    sell_entry     = None
                    buy_entry      = None
                    sell_opt_df    = None
                    buy_opt_df     = None
                    snap_sell_ltp  = None
                    snap_buy_ltp   = None
                    entry_exec_ts  = None
                    entry_vix      = None
                    has_additional     = False
                    add_sell_entry     = None
                    add_buy_entry      = None
                    add_sell_ltp       = None
                    add_buy_ltp        = None
                    add_booked_pl      = 0.0
                    trailing_sl_floor  = None
                    trade_entry_date   = None
                    sl_1min_ts         = None
                    sl_1min_type       = None

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

                exec_spot = next_row['open']

                selected_expiry = get_expiry(exec_ts, contracts_df)
                if selected_expiry is None:
                    logger.warning(f"  No expiry found for {exec_ts} — skipping")
                    continue

                sel_strike, sel_otype, sel_price = select_strike(
                    exec_spot, selected_expiry, exec_ts,
                    entry_direction, option_df_cache)
                if sel_strike is None:
                    logger.debug(f"  No valid delta strike at {exec_ts} — skipping")
                    continue

                hedge_strike = (sel_strike + HEDGE_POINTS
                                if entry_direction == 'bearish'
                                else sel_strike - HEDGE_POINTS)

                sell_opt_df = option_df_cache.get(
                    (selected_expiry, sel_strike, sel_otype),
                    load_option_data(selected_expiry, sel_strike, sel_otype))
                buy_opt_df  = load_option_data(
                    selected_expiry, hedge_strike, sel_otype)

                sell_entry_raw = get_option_price(sell_opt_df, exec_ts, 'open')
                buy_entry_raw  = get_option_price(buy_opt_df,  exec_ts, 'open')

                if sell_entry_raw is None or buy_entry_raw is None:
                    logger.debug(f"  Option data missing at {exec_ts} — skipping")
                    continue

                sell_entry = apply_slippage(sell_entry_raw, is_buy=False)
                buy_entry  = apply_slippage(buy_entry_raw,  is_buy=True)

                expiry_row    = contracts_df[contracts_df['end_date'] == selected_expiry]
                sel_elm_time  = expiry_row['elm_time'].iloc[0] if not expiry_row.empty else None
                in_trade       = True
                direction      = entry_direction
                sell_strike    = sel_strike
                buy_strike     = hedge_strike
                option_type    = sel_otype
                expiry         = selected_expiry
                elm_time       = sel_elm_time
                entry_time     = exec_ts
                entry_spot     = exec_spot
                entry_vix      = get_1min_value(vix_1m, exec_ts, 'close')
                sell_ltp       = sell_entry
                buy_ltp        = buy_entry
                trade_log      = []
                snap_sell_ltp  = sell_entry
                snap_buy_ltp   = buy_entry
                entry_exec_ts  = exec_ts
                has_additional     = True
                add_sell_entry     = sell_entry
                add_buy_entry      = buy_entry
                add_sell_ltp       = sell_entry
                add_buy_ltp        = buy_entry
                add_booked_pl      = 0.0
                trailing_sl_floor  = None
                trade_entry_date   = exec_ts.date()

                logger.info(
                    f"  ENTRY {direction:8s} | {exec_ts} | "
                    f"Spot: {exec_spot:.0f} | "
                    f"Sell {sel_strike}{sel_otype.upper()} @ {sell_entry:.1f} | "
                    f"Buy  {hedge_strike}{sel_otype.upper()} @ {buy_entry:.1f} | "
                    f"Expiry: {selected_expiry.date()}"
                )

        # ------------------------------------------------------------------
        # Capture 15:16–15:30 tail on high-VIX days while trade is active
        # The 15-min loop ends at 15:15 — this fills the gap to close.
        # ------------------------------------------------------------------
        if in_trade:
            day_close = pd.Timestamp(f"{day_date} 15:30:00")
            last_anchor = last_15min_ts if last_15min_ts is not None \
                          else pd.Timestamp(f"{day_date} 15:15:00")
            if last_anchor < day_close:
                snap_sell_ltp, snap_buy_ltp, _, _, trailing_sl_floor = \
                    _append_1min_snapshots_window(
                        last_anchor, day_close,
                        nifty_1m, vix_1m, nifty_75_indexed, nifty_15,
                        sell_opt_df, buy_opt_df,
                        sell_strike, buy_strike, option_type,
                        sell_entry, buy_entry, direction, expiry,
                        trade_log,
                        snap_sell_ltp, snap_buy_ltp,
                        trailing_sl_floor, trade_entry_date
                    )

    logger.info(f"Backtest complete. Total trades: {len(all_trades)}")
    return all_trades


# ---------------------------------------------------------------------------
# 1-min snapshot helpers
# ---------------------------------------------------------------------------

def _append_1min_snapshots_window(from_ts: pd.Timestamp, to_ts: pd.Timestamp,
                                   nifty_1m, vix_1m, nifty_75_indexed,
                                   nifty_15, sell_opt_df, buy_opt_df,
                                   sell_strike, buy_strike, option_type,
                                   sell_entry, buy_entry, direction, expiry,
                                   trade_log: list,
                                   last_sell_ltp: float = None,
                                   last_buy_ltp:  float = None,
                                   trailing_sl_floor: float = None,
                                   trade_entry_date = None):
    """
    Append 1-min snapshots for every minute in (from_ts, to_ts] to trade_log.
    Checks all SLs on every 1-min candle.
    Updates trailing_sl_floor as a ratchet — only ever moves up.
    Returns (last_sell_ltp, last_buy_ltp, sl_ts, sl_type, trailing_sl_floor).
    """
    running_sell_ltp   = last_sell_ltp if last_sell_ltp is not None else sell_entry
    running_buy_ltp    = last_buy_ltp  if last_buy_ltp  is not None else buy_entry
    running_trail_sl   = trailing_sl_floor
    sl_hit_ts          = None
    sl_hit_type        = None
    net_credit         = sell_entry - buy_entry

    window = nifty_1m[
        (nifty_1m.index > from_ts) & (nifty_1m.index <= to_ts)
    ]
    for ts, row in window.iterrows():
        spot = float(row['close'])
        vix  = get_1min_value(vix_1m, ts, 'close')

        # Fetch option prices — carry forward last known on miss
        fetched_sell = get_option_price(sell_opt_df, ts, 'close')
        fetched_buy  = get_option_price(buy_opt_df,  ts, 'close')
        if fetched_sell is not None:
            running_sell_ltp = fetched_sell
        if fetched_buy is not None:
            running_buy_ltp = fetched_buy

        prior_75 = nifty_75_indexed[nifty_75_indexed.index <= ts]
        trend_75 = prior_75.iloc[-1]['trend'] if not prior_75.empty else None

        prior_15 = nifty_15[nifty_15['time_stamp'] <= ts]
        trend_15 = prior_15.iloc[-1]['trend'] if not prior_15.empty else None

        unrealised    = (sell_entry - running_sell_ltp) + (running_buy_ltp - buy_entry)
        days_in_trade = (ts.date() - trade_entry_date).days if trade_entry_date else 0

        # Update trailing SL ratchet before building snapshot and checking SLs
        running_trail_sl = update_trailing_sl(running_trail_sl, unrealised, net_credit)

        snapshot = _build_snapshot(
            ts, spot, vix,
            sell_strike, buy_strike, option_type,
            running_sell_ltp, running_buy_ltp,
            sell_entry, buy_entry,
            direction, trend_75, trend_15, expiry,
            days_in_trade=days_in_trade,
            trailing_sl_floor=running_trail_sl,
        )
        trade_log.append(snapshot)

        # Check all SLs on every 1-min candle — stop at first hit.
        # No guard here: SL at candle X close → exit at X+1 open, always correct.
        # The NO_EXIT_BEFORE guard only applies at the 15-min fallback level.
        if sl_hit_ts is None:
            sl = check_stop_losses(
                spot, sell_strike, direction,
                running_sell_ltp, sell_entry,
                running_buy_ltp,  buy_entry,
                days_in_trade=days_in_trade,
                trailing_sl_floor=running_trail_sl)
            if sl:
                sl_hit_ts   = ts
                sl_hit_type = sl
                break   # stop scanning — exit will be handled by caller

    return running_sell_ltp, running_buy_ltp, sl_hit_ts, sl_hit_type, running_trail_sl


def _append_1min_snapshots(day_date, nifty_1m, vix_1m,
                            nifty_75_indexed, nifty_15,
                            sell_opt_df, buy_opt_df,
                            sell_strike, buy_strike, option_type,
                            sell_entry, buy_entry, direction, expiry,
                            trade_log: list,
                            last_sell_ltp: float = None,
                            last_buy_ltp:  float = None,
                            trailing_sl_floor: float = None,
                            trade_entry_date = None):
    """
    Append all 1-min snapshots for a full day (for overnight-held trades on non-high-VIX days).
    Returns (last_sell_ltp, last_buy_ltp, sl_hit_ts, sl_hit_type, trailing_sl_floor).
    """
    day_start = pd.Timestamp(f"{day_date} 09:15:00")
    day_end   = pd.Timestamp(f"{day_date} 15:30:00")
    return _append_1min_snapshots_window(
        day_start - pd.Timedelta(minutes=1), day_end,
        nifty_1m, vix_1m, nifty_75_indexed, nifty_15,
        sell_opt_df, buy_opt_df,
        sell_strike, buy_strike, option_type,
        sell_entry, buy_entry, direction, expiry,
        trade_log,
        last_sell_ltp, last_buy_ltp,
        trailing_sl_floor, trade_entry_date
    )


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _calc_pl(sell_entry: float, sell_exit: float,
             buy_entry: float, buy_exit: float) -> float:
    """Calculate P&L in points for a credit spread."""
    return round((sell_entry - sell_exit) + (buy_exit - buy_entry), 2)


def _compute_trade_stats(trade_log: list) -> dict:
    """
    Scan the per-trade 1-min log and compute extremes across the full trade lifetime
    (including the exit candle).

    Returns a dict with 12 fields:
      max/min unrealised_pl_pts and their timestamps
      max/min sell_ltp and their timestamps
      max/min buy_ltp and their timestamps
    """
    if not trade_log:
        return {
            'max_unrealised_pl_pts': None, 'max_unrealised_pl_ts': None,
            'min_unrealised_pl_pts': None, 'min_unrealised_pl_ts': None,
            'max_sell_ltp':          None, 'max_sell_ltp_ts':      None,
            'min_sell_ltp':          None, 'min_sell_ltp_ts':      None,
            'max_buy_ltp':           None, 'max_buy_ltp_ts':       None,
            'min_buy_ltp':           None, 'min_buy_ltp_ts':       None,
        }

    max_pl = min_pl = trade_log[0]['unrealised_pl_pts']
    max_pl_ts = min_pl_ts = trade_log[0]['time_stamp']

    max_sell = min_sell = trade_log[0]['sell_ltp']
    max_sell_ts = min_sell_ts = trade_log[0]['time_stamp']

    max_buy = min_buy = trade_log[0]['buy_ltp']
    max_buy_ts = min_buy_ts = trade_log[0]['time_stamp']

    for row in trade_log[1:]:
        ts  = row['time_stamp']
        pl  = row['unrealised_pl_pts']
        sl  = row['sell_ltp']
        bl  = row['buy_ltp']

        if pl > max_pl:  max_pl,   max_pl_ts   = pl,  ts
        if pl < min_pl:  min_pl,   min_pl_ts   = pl,  ts
        if sl > max_sell: max_sell, max_sell_ts = sl,  ts
        if sl < min_sell: min_sell, min_sell_ts = sl,  ts
        if bl > max_buy:  max_buy,  max_buy_ts  = bl,  ts
        if bl < min_buy:  min_buy,  min_buy_ts  = bl,  ts

    return {
        'max_unrealised_pl_pts': round(max_pl,   2),
        'max_unrealised_pl_ts':  max_pl_ts,
        'min_unrealised_pl_pts': round(min_pl,   2),
        'min_unrealised_pl_ts':  min_pl_ts,
        'max_sell_ltp':          round(max_sell, 2),
        'max_sell_ltp_ts':       max_sell_ts,
        'min_sell_ltp':          round(min_sell, 2),
        'min_sell_ltp_ts':       min_sell_ts,
        'max_buy_ltp':           round(max_buy,  2),
        'max_buy_ltp_ts':        max_buy_ts,
        'min_buy_ltp':           round(min_buy,  2),
        'min_buy_ltp_ts':        min_buy_ts,
    }


def _build_trade_record(entry_time, exit_time, direction, expiry,
                         sell_strike, buy_strike, option_type,
                         entry_spot, exit_spot,
                         sell_entry, buy_entry,
                         sell_exit, buy_exit,
                         pl_points, pl_rupees, exit_reason,
                         trade_stats: dict = None,
                         entry_vix=None, exit_vix=None) -> dict:
    record = {
        'entry_time':  entry_time,
        'exit_time':   exit_time,
        'direction':   direction,
        'expiry':      expiry,
        'sell_strike': sell_strike,
        'buy_strike':  buy_strike,
        'option_type': option_type,
        'spread_type': SPREAD_TYPE,
        'entry_spot':  entry_spot,
        'exit_spot':   exit_spot,
        'sell_entry':  sell_entry,
        'buy_entry':   buy_entry,
        'sell_exit':   sell_exit,
        'buy_exit':    buy_exit,
        'pl_points':   pl_points,
        'pl_rupees':   pl_rupees,
        'exit_reason': exit_reason,
        'entry_vix':   round(entry_vix, 2) if entry_vix is not None else None,
        'exit_vix':    round(exit_vix,  2) if exit_vix  is not None else None,
    }
    if trade_stats:
        record.update(trade_stats)
    else:
        record.update({
            'max_unrealised_pl_pts': None, 'max_unrealised_pl_ts': None,
            'min_unrealised_pl_pts': None, 'min_unrealised_pl_ts': None,
            'max_sell_ltp':          None, 'max_sell_ltp_ts':      None,
            'min_sell_ltp':          None, 'min_sell_ltp_ts':      None,
            'max_buy_ltp':           None, 'max_buy_ltp_ts':       None,
            'min_buy_ltp':           None, 'min_buy_ltp_ts':       None,
        })
    return record


def _log_exit(trade: dict):
    logger.info(
        f"  EXIT  {trade['exit_reason']:20s} | {trade['direction']:8s} | "
        f"Sell {trade['sell_strike']} | "
        f"P&L: {trade['pl_points']:+.1f} pts ({trade['pl_rupees']:+,.0f})"
    )


def _save_trade_log(trade_counter: int, entry_time: pd.Timestamp,
                    trade_log: list):
    """Save per-trade 1-min log. Filename: trade_NNN_YYYY-MM-DD_HHMM.csv"""
    if not trade_log:
        return
    entry_str = pd.Timestamp(entry_time).strftime('%Y-%m-%d_%H%M')
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
    logger.info("APOLLO BACKTEST SUMMARY")
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("=== Apollo Backtest starting ===")

    nifty_15, nifty_75, vix_daily = load_precomputed()
    nifty_1m, vix_1m              = load_1min_data()
    holidays_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data_pipeline', 'config', 'holidays.csv')
    holidays_df_elm = pd.read_csv(holidays_path, parse_dates=['date'])
    holidays_df_elm['date'] = pd.to_datetime(holidays_df_elm['date']).dt.date
    contracts_df = load_contracts(holidays_df_elm)

    logger.info(f"  Contracts    : {len(contracts_df)} expiries")
    logger.info(f"  VIX threshold: {VIX_THRESHOLD}")
    logger.info(f"  Spread type  : {SPREAD_TYPE}")
    logger.info(f"  Target delta : {TARGET_DELTA}")
    logger.info(f"  Hedge points : {HEDGE_POINTS}")
    logger.info(f"  Index SL     : {'ON' if ENABLE_INDEX_SL else 'OFF'} — {INDEX_SL_OFFSET} pts from sell strike")
    logger.info(f"  Option SL    : {'ON' if ENABLE_OPTION_SL else 'OFF'} — multipliers {OPTION_SL_MULTIPLIERS}, floor {OPTION_SL_FLOOR_MULT}x")
    logger.info(f"  Spread SL    : {'ON' if ENABLE_SPREAD_SL else 'OFF'} — pcts {SPREAD_SL_PCTS}, floor {SPREAD_SL_FLOOR_PCT*100:.0f}%")
    logger.info(f"  Trailing SL  : {'ON' if ENABLE_TRAILING_SL else 'OFF'} — triggers {TRAILING_SL_TRIGGER_1:.2f}/{TRAILING_SL_TRIGGER_2:.2f}/{TRAILING_SL_TRIGGER_3:.2f}")

    all_trades = run_backtest(
        nifty_15, nifty_75, vix_daily, contracts_df,
        nifty_1m, vix_1m, holidays_df_elm)
    save_trade_summary(all_trades)

    logger.info("=== Apollo Backtest complete ===")