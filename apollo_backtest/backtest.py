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
    INDEX_SL_OFFSET, OPTION_SL_MULTIPLIER, SPREAD_LOSS_CAP,
    OPTION_SL_BASE_PCT, OPTION_SL_DAY_REDUCTION,
    OPTION_SL_TRAIL_TRIGGER, OPTION_SL_TRAIL_FLOOR1,
    OPTION_SL_TRAIL_LOCK2, OPTION_SL_TRAIL_FLOOR2,
    NO_EXIT_BEFORE, ADDITIONAL_LOT_MULTIPLIER, ELM_SECONDS_BEFORE_EXPIRY,
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

def compute_option_sl(sell_entry: float, buy_entry: float,
                      buy_ltp: float, days_in_trade: int,
                      peak_unrealised_pl: float) -> float:
    """
    Compute the dynamic option SL threshold for the sold option.
    Returns the maximum sell_ltp at which the position should be held.
    Exit if sell_ltp > returned value.

    Three mechanisms — tightest wins:
    1. Base SL tightened by days in trade:
       sell_entry + net_credit * (BASE_PCT - DAY_REDUCTION * days)
       Day 0: +50%, Day 1: +40%, Day 2: +30% ...
    2. Trail floor 1 (breakeven): once peak P&L > TRAIL_TRIGGER * net_credit
    3. Trail floor 2 (lock 25%): once peak P&L > TRAIL_LOCK2 * net_credit
    Floors only move up — never loosen once triggered.
    """
    net_credit = sell_entry - buy_entry

    # 1. Base SL tightening
    pct    = max(OPTION_SL_BASE_PCT - OPTION_SL_DAY_REDUCTION * days_in_trade, 0.05)
    base_sl = sell_entry + net_credit * pct

    # 2. Trail floor 1: breakeven
    # spread P&L = (sell_entry - sell_ltp) + (buy_ltp - buy_entry) >= FLOOR1 * net_credit
    # sell_ltp <= sell_entry + (buy_ltp - buy_entry) - FLOOR1 * net_credit
    trail1_sl = None
    if peak_unrealised_pl > OPTION_SL_TRAIL_TRIGGER * net_credit:
        trail1_sl = sell_entry + (buy_ltp - buy_entry) - OPTION_SL_TRAIL_FLOOR1 * net_credit

    # 3. Trail floor 2: lock in 25% of net credit
    trail2_sl = None
    if peak_unrealised_pl > OPTION_SL_TRAIL_LOCK2 * net_credit:
        trail2_sl = sell_entry + (buy_ltp - buy_entry) - OPTION_SL_TRAIL_FLOOR2 * net_credit

    candidates = [c for c in [base_sl, trail1_sl, trail2_sl] if c is not None]
    return min(candidates)

# ---------------------------------------------------------------------------
# Stop loss checks
# ---------------------------------------------------------------------------

def check_stop_losses(spot: float, sell_strike: int, direction: str,
                      sell_ltp: float, sell_entry: float,
                      buy_ltp: float, buy_entry: float,
                      option_sl_level: float = None) -> str:
    """
    Check all three stop loss conditions.
    Returns triggered SL type string, or None.
    """
    # Index SL: fires when spot approaches sell strike from OTM side
    # Bearish (sold CE): SL when spot >= sell_strike - INDEX_SL_OFFSET
    # Bullish (sold PE): SL when spot <= sell_strike + INDEX_SL_OFFSET
    if direction == 'bearish' and spot >= sell_strike - INDEX_SL_OFFSET:
        return 'index_sl'
    if direction == 'bullish' and spot <= sell_strike + INDEX_SL_OFFSET:
        return 'index_sl'

    # Option SL: use dynamic level if provided, else fall back to multiplier
    effective_option_sl = option_sl_level if option_sl_level is not None \
                          else sell_entry * OPTION_SL_MULTIPLIER
    if sell_ltp >= effective_option_sl:
        return 'option_sl'

    if SPREAD_TYPE == 'credit':
        net_credit   = sell_entry - buy_entry
        max_loss     = HEDGE_POINTS - net_credit
        current_loss = (sell_ltp - sell_entry) - (buy_ltp - buy_entry)
        if max_loss > 0 and current_loss >= max_loss * SPREAD_LOSS_CAP:
            return 'spread_loss_cap'

    return None


# ---------------------------------------------------------------------------
# Slippage
# ---------------------------------------------------------------------------

def apply_slippage(price: float, is_buy: bool) -> float:
    """Add slippage to buys, subtract from sells."""
    return (price + SLIPPAGE_POINTS) if is_buy else (price - SLIPPAGE_POINTS)


# ---------------------------------------------------------------------------
# Per-trade 1-min snapshot
# ---------------------------------------------------------------------------

def _build_snapshot(ts: pd.Timestamp, spot: float, vix: float,
                    sell_strike: int, buy_strike: int, option_type: str,
                    sell_ltp: float, buy_ltp: float,
                    sell_entry: float, buy_entry: float,
                    direction: str, trend_75, trend_15,
                    expiry: pd.Timestamp,
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
    unrealised_pl = _calc_pl(sell_entry, sell_ltp, buy_entry, buy_ltp)

    # SL levels for reference — index SL fires when spot approaches ATM
    if direction == 'bearish':
        index_sl_level  = sell_strike - INDEX_SL_OFFSET
    else:
        index_sl_level  = sell_strike + INDEX_SL_OFFSET
    option_sl_level = round(sell_entry * OPTION_SL_MULTIPLIER, 2)

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
    # Dynamic SL tracking
    peak_unrealised_pl = 0.0
    trade_entry_date   = None

    for day_date in trading_days:

        # ------------------------------------------------------------------
        # Close any trade where expiry has passed without an explicit exit
        # ------------------------------------------------------------------
        if in_trade and expiry is not None and day_date > expiry.date():
            pl_points = _calc_pl(sell_entry, sell_ltp, buy_entry, buy_ltp)
            pl_rupees = pl_points * LOT_SIZE
            expiry_exit_vix = get_1min_value(vix_1m, expiry, 'close')
            trade_record = _build_trade_record(
                entry_time, expiry, direction, expiry,
                sell_strike, buy_strike, option_type,
                entry_spot, sell_ltp,
                sell_entry, buy_entry,
                apply_slippage(sell_ltp, is_buy=True),
                apply_slippage(buy_ltp,  is_buy=False),
                pl_points, pl_rupees, 'expiry',
                entry_vix=entry_vix, exit_vix=expiry_exit_vix
            )
            trade_counter += 1
            all_trades.append(trade_record)
            _log_exit(trade_record)
            _save_trade_log(trade_counter, entry_time, trade_log)
            in_trade  = False
            trade_log = []

        # ------------------------------------------------------------------
        # Build 1-min log for any active trade on non-high-VIX days
        # (trade may have been entered on a high-VIX day and is still open)
        # ------------------------------------------------------------------
        if in_trade and day_date not in high_vix_dates:
            snap_sell_ltp, snap_buy_ltp, sl_1min_ts, sl_1min_type, peak_unrealised_pl = \
                _append_1min_snapshots(
                    day_date, nifty_1m, vix_1m,
                    nifty_75_indexed, nifty_15,
                    sell_opt_df, buy_opt_df,
                    sell_strike, buy_strike, option_type,
                    sell_entry, buy_entry, direction, expiry,
                    trade_log,
                    snap_sell_ltp, snap_buy_ltp,
                    peak_unrealised_pl, trade_entry_date
                )
            # If a 1-min SL was hit on this overnight day, carry sl_1min_ts
            # forward — it will be processed on the next trading day loop
            # which has access to exec_ts (next candle open) for exit pricing.
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
                snap_sell_ltp, snap_buy_ltp, sl_1min_ts, sl_1min_type, peak_unrealised_pl = \
                    _append_1min_snapshots_window(
                        prev_ts, ts, nifty_1m, vix_1m,
                        nifty_75_indexed, nifty_15,
                        sell_opt_df, buy_opt_df,
                        sell_strike, buy_strike, option_type,
                        sell_entry, buy_entry, direction, expiry,
                        trade_log,
                        snap_sell_ltp, snap_buy_ltp,
                        peak_unrealised_pl, trade_entry_date
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
                        option_sl_lvl_15 = compute_option_sl(
                            sell_entry, buy_entry, buy_ltp,
                            days_in_trade_15, peak_unrealised_pl)
                        sl = check_stop_losses(
                            spot, sell_strike, direction,
                            sell_ltp, sell_entry, buy_ltp, buy_entry,
                            option_sl_level=option_sl_lvl_15)
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
                                        recheck_buy_ltp,  buy_entry)
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

                if exit_reason:
                    # Step 1: Fill the gap between signal candle and execution candle.
                    # This MUST happen before pricing so that if an SL fires in the
                    # gap, exec_ts is overridden to the correct 1-min candle BEFORE
                    # we fetch any exit prices.
                    if ts < exec_ts:
                        _gf_sell_ltp, _gf_buy_ltp, _gf_sl_ts, _gf_sl_type, _gf_peak = \
                            _append_1min_snapshots_window(
                                ts, exec_ts - pd.Timedelta(minutes=1),
                                nifty_1m, vix_1m, nifty_75_indexed, nifty_15,
                                sell_opt_df, buy_opt_df,
                                sell_strike, buy_strike, option_type,
                                sell_entry, buy_entry, direction, expiry,
                                trade_log,
                                snap_sell_ltp, snap_buy_ltp,
                                peak_unrealised_pl, trade_entry_date
                            )
                        peak_unrealised_pl = max(peak_unrealised_pl, _gf_peak)
                        snap_sell_ltp = _gf_sell_ltp
                        snap_buy_ltp  = _gf_buy_ltp
                        # If SL fired during gap-fill, override exec_ts and exit_reason
                        # before any pricing happens
                        if _gf_sl_ts is not None:
                            exec_ts     = _gf_sl_ts + pd.Timedelta(minutes=1)
                            exec_col    = 'open'
                            exit_reason = _gf_sl_type
                            sell_ltp    = snap_sell_ltp
                            buy_ltp     = snap_buy_ltp

                    # Step 2: Fetch exit prices using final exec_ts (may have been
                    # overridden above if SL fired during gap-fill)
                    sell_exit_raw = get_option_price(
                        sell_opt_df, exec_ts, exec_col) or sell_ltp
                    buy_exit_raw  = get_option_price(
                        buy_opt_df,  exec_ts, exec_col) or buy_ltp

                    sell_exit_net = apply_slippage(sell_exit_raw, is_buy=True)
                    buy_exit_net  = apply_slippage(buy_exit_raw,  is_buy=False)
                    base_pl       = _calc_pl(
                        sell_entry, sell_exit_net,
                        buy_entry,  buy_exit_net)

                    # Step 3: Exit additional lots simultaneously if still active
                    if has_additional:
                        add_sell_exit_raw = get_option_price(
                            sell_opt_df, exec_ts, exec_col) or add_sell_ltp
                        add_buy_exit_raw  = get_option_price(
                            buy_opt_df,  exec_ts, exec_col) or add_buy_ltp
                        add_sell_exit_net = apply_slippage(
                            add_sell_exit_raw, is_buy=True)
                        add_buy_exit_net  = apply_slippage(
                            add_buy_exit_raw,  is_buy=False)
                        add_booked_pl     = _calc_pl(
                            add_sell_entry, add_sell_exit_net,
                            add_buy_entry,  add_buy_exit_net)
                        has_additional    = False

                    # Step 4: Normalised P&L per unit of capital
                    pl_points = round(
                        base_pl + add_booked_pl * ADDITIONAL_LOT_MULTIPLIER, 2)
                    pl_rupees = pl_points * LOT_SIZE

                    # Capture the execution candle (signal ts+1) in the log
                    # so the log runs all the way to the actual exit timestamp
                    exec_spot_val = get_1min_value(nifty_1m, exec_ts, 'close') or spot
                    exec_vix_val  = get_1min_value(vix_1m,   exec_ts, 'close')
                    prior_75_exec = nifty_75_indexed[nifty_75_indexed.index <= exec_ts]
                    trend_75_exec = prior_75_exec.iloc[-1]['trend']                                     if not prior_75_exec.empty else trend_75
                    prior_15_exec = nifty_15[nifty_15['time_stamp'] <= exec_ts]
                    trend_15_exec = prior_15_exec.iloc[-1]['trend']                                     if not prior_15_exec.empty else trend_15
                    exit_snapshot = _build_snapshot(
                        exec_ts, exec_spot_val, exec_vix_val,
                        sell_strike, buy_strike, option_type,
                        sell_exit_raw, buy_exit_raw,
                        sell_entry, buy_entry,
                        direction, trend_75_exec, trend_15_exec, expiry,
                        realised_pl_pts=pl_points,
                        realised_pl_rs=pl_rupees
                    )
                    trade_log.append(exit_snapshot)

                    exit_vix = get_1min_value(vix_1m, exec_ts, 'close')
                    trade_record = _build_trade_record(
                        entry_time, exec_ts, direction, expiry,
                        sell_strike, buy_strike, option_type,
                        entry_spot, spot,
                        sell_entry, buy_entry,
                        sell_exit_net, buy_exit_net,
                        pl_points, pl_rupees, exit_reason,
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
                    peak_unrealised_pl = 0.0
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
                peak_unrealised_pl = 0.0
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
            last_anchor = last_15min_ts if last_15min_ts is not None                           else pd.Timestamp(f"{day_date} 15:15:00")
            if last_anchor < day_close:
                snap_sell_ltp, snap_buy_ltp, _, _, _peak_tail = \
                    _append_1min_snapshots_window(
                        last_anchor, day_close,
                        nifty_1m, vix_1m, nifty_75_indexed, nifty_15,
                        sell_opt_df, buy_opt_df,
                        sell_strike, buy_strike, option_type,
                        sell_entry, buy_entry, direction, expiry,
                        trade_log,
                        snap_sell_ltp, snap_buy_ltp,
                        peak_unrealised_pl, trade_entry_date
                    )
                peak_unrealised_pl = max(peak_unrealised_pl, _peak_tail)

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
                                   peak_unrealised_pl: float = 0.0,
                                   trade_entry_date = None):
    """
    Append 1-min snapshots for every minute in (from_ts, to_ts] to trade_log.
    Checks index and dynamic option SL on every 1-min candle.
    Returns (last_sell_ltp, last_buy_ltp, sl_ts, sl_type, peak_unrealised_pl).
    """
    running_sell_ltp   = last_sell_ltp if last_sell_ltp is not None else sell_entry
    running_buy_ltp    = last_buy_ltp  if last_buy_ltp  is not None else buy_entry
    sl_hit_ts          = None
    sl_hit_type        = None
    running_peak_pl    = peak_unrealised_pl

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

        snapshot = _build_snapshot(
            ts, spot, vix,
            sell_strike, buy_strike, option_type,
            running_sell_ltp, running_buy_ltp,
            sell_entry, buy_entry,
            direction, trend_75, trend_15, expiry
        )
        trade_log.append(snapshot)

        # Update unrealised P&L and peak
        unrealised = (sell_entry - running_sell_ltp) + (running_buy_ltp - buy_entry)
        if unrealised > running_peak_pl:
            running_peak_pl = unrealised

        # Compute dynamic option SL level for this candle
        days_in_trade = (ts.date() - trade_entry_date).days if trade_entry_date else 0
        option_sl_lvl = compute_option_sl(
            sell_entry, buy_entry, running_buy_ltp,
            days_in_trade, running_peak_pl)

        # Check SL on every 1-min candle — stop at first hit.
        # No guard here: SL at candle X close → exit at X+1 open, always correct.
        # The NO_EXIT_BEFORE guard only applies at the 15-min fallback level.
        if sl_hit_ts is None:
            sl = check_stop_losses(
                spot, sell_strike, direction,
                running_sell_ltp, sell_entry,
                running_buy_ltp,  buy_entry,
                option_sl_level=option_sl_lvl)
            if sl:
                sl_hit_ts   = ts
                sl_hit_type = sl
                break   # stop scanning — exit will be handled by caller

    return running_sell_ltp, running_buy_ltp, sl_hit_ts, sl_hit_type, running_peak_pl


def _append_1min_snapshots(day_date, nifty_1m, vix_1m,
                            nifty_75_indexed, nifty_15,
                            sell_opt_df, buy_opt_df,
                            sell_strike, buy_strike, option_type,
                            sell_entry, buy_entry, direction, expiry,
                            trade_log: list,
                            last_sell_ltp: float = None,
                            last_buy_ltp:  float = None,
                            peak_unrealised_pl: float = 0.0,
                            trade_entry_date = None):
    """
    Append all 1-min snapshots for a full day (for overnight-held trades on non-high-VIX days).
    Returns (last_sell_ltp, last_buy_ltp, sl_hit_ts, sl_hit_type, peak_unrealised_pl).
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
        peak_unrealised_pl, trade_entry_date
    )


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _calc_pl(sell_entry: float, sell_exit: float,
             buy_entry: float, buy_exit: float) -> float:
    """Calculate P&L in points for a credit spread."""
    return round((sell_entry - sell_exit) + (buy_exit - buy_entry), 2)


def _build_trade_record(entry_time, exit_time, direction, expiry,
                         sell_strike, buy_strike, option_type,
                         entry_spot, exit_spot,
                         sell_entry, buy_entry,
                         sell_exit, buy_exit,
                         pl_points, pl_rupees, exit_reason,
                         entry_vix=None, exit_vix=None) -> dict:
    return {
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
    logger.info(f"  Index SL     : {INDEX_SL_OFFSET} pts before sell strike reaches ATM")
    logger.info(f"  Option SL    : {OPTION_SL_MULTIPLIER}x entry premium")
    logger.info(f"  Spread SL    : {SPREAD_LOSS_CAP*100:.0f}% of max loss")

    all_trades = run_backtest(
        nifty_15, nifty_75, vix_daily, contracts_df,
        nifty_1m, vix_1m, holidays_df_elm)
    save_trade_summary(all_trades)

    logger.info("=== Apollo Backtest complete ===")