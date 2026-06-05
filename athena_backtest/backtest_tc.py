"""
backtest_tc.py — Athena TRIPLE_CONFIRM Parallel Backtest

Parallel research track. Does NOT modify any committed files.

Applies TRIPLE_CONFIRM signals to the Athena double-calendar strategy:
  - Bullish TC: triggers early CE parachute entry before the reactive spot
                threshold (spot > ce_sell + 150) fires.
  - Bearish TC: (1) exits an active CE parachute early.
                (2) opens a PE parachute (0.35d monthly PE) — the only trigger
                    for the PE-side parachute. Simultaneously closes the PE
                    safety wing (now redundant while parachute is active).
  - PE parachute exit: bullish TC (false alarm) or spot >= pe_sell_strike.
    On exit: PE parachute sold, PE safety wing rebuyed.
  - Reactive CE spot triggers remain as fallbacks if no bullish TC fires.

TC signals are date-keyed: any candle on that date sees the signal. If
multiple TC signals fall on the same date, the last one wins.

P&L accounting:
  - All mid-trade realised (CE chute, PE chute, PE wing close/rebuy) accumulate
    in window_tc_realised, which becomes realised_pl in the main loop.
  - pe_wing_entry_eff tracks the effective PE wing entry for the final exit.

Output (never overwrites committed baseline):
  athena_backtest/data_tc/trade_summary_tc.csv

Usage (from repo root):
  python athena_backtest/backtest_tc.py
"""

import os
import sys
import logging
import warnings
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings('ignore')

from backtest import (
    load_index_data, load_contracts, load_option_data,
    get_option_price, get_1min_value,
    select_strike, apply_slippage,
    get_target_delta, compute_delta, compute_max_theoretical_profit,
    get_prior_expiry, compute_entry_date, select_buy_expiry,
    get_end_date, get_elm_time,
    _calc_exit_pl, build_trade_record, calc_strategy_pl,
    check_spread_sl, check_index_sl, check_option_sl,
    check_trail_stop, check_profit_target,
    determine_sl_triggered_side,
    LOT_SIZE,
)
from configs import (
    BACKTEST_START_DATE, BACKTEST_END_DATE,
    ENTRY_TIME, BUY_LEG_MIN_DTE,
    VIX_DELTA_BANDS,
    ENABLE_VIX_FILTER, VIX_FILTER_LOW, VIX_FILTER_HIGH,
    ENABLE_SAFETY_WINGS, SAFETY_WING_DELTA,
    ENABLE_EMERGENCY_HEDGE, EMERGENCY_HEDGE_DELTA,
    EMERGENCY_TRIGGER_OFFSET, EMERGENCY_EXIT_OFFSET, EMERGENCY_MAX_ATTEMPTS,
    ELM_EXIT_TIME,
    TRAIL_ACTIVATION_POINTS,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_DIR             = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT        = os.path.dirname(_DIR)
TC_DATA_DIR      = os.path.join(_DIR, 'data_tc')
TC_SUMMARY       = os.path.join(TC_DATA_DIR, 'trade_summary_tc.csv')
TC_SIGNALS_CSV   = os.path.join(REPO_ROOT, 'iris_backtest', 'data',
                                 'TRIPLE_CONFIRM_excursions.csv')
BASELINE_SUMMARY = os.path.join(_DIR, 'data', 'trade_summary.csv')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TC signal loading
# ---------------------------------------------------------------------------

def _build_tc_signal_map(path: str) -> dict:
    """
    Load pre-computed TRIPLE_CONFIRM signals.
    Returns {date: direction} — date-keyed so any candle on that date sees
    the signal. If multiple signals fall on the same date, last one wins.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"TC signals file not found: {path}\n"
            f"Run: python iris_backtest/research/run_all.py")
    df = pd.read_csv(path, parse_dates=['signal_ts'])
    df['signal_ts'] = pd.to_datetime(df['signal_ts'])
    df['date'] = df['signal_ts'].dt.date
    df = df.drop_duplicates(subset='date', keep='last')
    return dict(zip(df['date'], df['direction']))


# ---------------------------------------------------------------------------
# TC window scanner — modified copy of append_1min_snapshots_window
# ---------------------------------------------------------------------------

def _scan_window_tc(from_ts: pd.Timestamp, to_ts: pd.Timestamp,
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
                    ce_wing_df=None, pe_wing_df=None,
                    ce_wing_entry: float = 0.0, pe_wing_entry: float = 0.0,
                    last_ce_wing_ltp: float = 0.0, last_pe_wing_ltp: float = 0.0,
                    ce_wing_strike: int = None, pe_wing_strike: int = None,
                    opt_df_cache: dict = None, buy_expiry_end: pd.Timestamp = None,
                    tc_signal_map: dict = None):
    """
    TC-modified window scanner. Returns a 22-item tuple:
      0-3:  running LTPs (ce_sell, ce_buy, pe_sell, pe_buy)
      4-5:  sl_hit_ts, sl_hit_reason
      6:    running_peak_pl
      7-8:  adj_trigger_ts, adj_winning_side (always None — no TC adjustment)
      9-10: running_ce_wing, running_pe_wing (tracks active wing)
      11:   window_tc_realised (CE chute + PE chute + PE wing ops)
      12-14: emer_strike, emer_entry, emer_ltp (CE chute state)
      15:   pe_chute_strike
      16:   pe_chute_pl (locked-in P&L if PE chute closed; else 0)
      17:   pe_wing_entry_eff (effective PE wing entry for final exit)
      18:   pe_wing_df_eff (effective PE wing df for final exit)
      19:   tc_ce_early (True if TC triggered CE chute before spot threshold)
      20:   tc_pe_triggered (True if bearish TC triggered PE chute)
      21:   pe_wing_close_price (price at which wing was sold; 0 if never sold)
    """
    if tc_signal_map is None:
        tc_signal_map = {}

    running_ce_sell = last_ce_sell_ltp
    running_ce_buy  = last_ce_buy_ltp
    running_pe_sell = last_pe_sell_ltp
    running_pe_buy  = last_pe_buy_ltp
    running_ce_wing = last_ce_wing_ltp
    running_pe_wing = last_pe_wing_ltp

    # Tracks LTP of whichever PE wing DF is currently active
    active_pe_wing_df  = pe_wing_df
    pe_wing_entry_eff  = pe_wing_entry   # may change if wing is sold/rebuyed
    pe_wing_df_eff     = pe_wing_df

    window_tc_realised = 0.0

    # CE parachute state (same as baseline emergency hedge)
    emer_active   = False
    emer_via_tc   = False  # True when TC triggered entry (not reactive spot)
    emer_strike   = None
    emer_entry    = 0.0
    emer_ltp      = 0.0
    emer_df       = None
    emer_attempts = 0
    tc_ce_early   = False  # True if TC triggered entry before spot threshold

    # PE parachute state (TC-only)
    pe_chute_active   = False
    pe_chute_attempts = 0
    pe_chute_strike   = None
    pe_chute_entry    = 0.0
    pe_chute_ltp      = 0.0
    pe_chute_df       = None
    pe_chute_pl_lock  = 0.0   # locked-in P&L once closed
    tc_pe_triggered   = False
    pe_wing_close_price = 0.0

    sl_hit_ts     = None
    sl_hit_reason = None

    window = nifty_1m[
        (nifty_1m.index > from_ts) & (nifty_1m.index <= to_ts)
    ]

    for ts, row in window.iterrows():
        spot = float(row['close'])
        vix  = get_1min_value(vix_1m, ts, 'close')

        # Update base LTPs
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
        if active_pe_wing_df is not None:
            v = get_option_price(active_pe_wing_df, ts, 'close')
            if v is not None: running_pe_wing = v

        # -----------------------------------------------------------------
        # CE parachute — TC-enhanced emergency hedge
        # -----------------------------------------------------------------
        if ENABLE_EMERGENCY_HEDGE and buy_expiry_end is not None and opt_df_cache is not None:
            tc_dir = tc_signal_map.get(ts.date())

            # Entry: TC early trigger OR reactive spot threshold
            if not emer_active and emer_attempts < EMERGENCY_MAX_ATTEMPTS:
                tc_entry = (tc_dir == 'bullish')
                spot_entry = (spot >= ce_sell_strike - EMERGENCY_TRIGGER_OFFSET)
                if tc_entry or spot_entry:
                    stk, pr = select_strike(
                        spot, buy_expiry_end, ts, 'ce', opt_df_cache, EMERGENCY_HEDGE_DELTA)
                    if stk:
                        emer_strike   = stk
                        emer_entry    = apply_slippage(pr, is_buy=True)
                        emer_ltp      = pr
                        emer_df       = opt_df_cache.get((buy_expiry_end, stk, 'ce'))
                        emer_active   = True
                        emer_via_tc   = tc_entry and not spot_entry
                        emer_attempts += 1
                        if emer_via_tc:
                            tc_ce_early = True
                        logger.info(
                            f"  [CE-CHUTE] Entry {'TC-early' if tc_entry and not spot_entry else 'spot-trigger'} "
                            f"{emer_strike} @ {emer_entry:.1f} at {ts} | spot={spot:.0f}")

            # LTP update
            if emer_active:
                v = get_option_price(emer_df, ts, 'close')
                if v is not None: emer_ltp = v

                # Exit: TC bearish OR reactive spot reversal (spot exit only for spot-triggered entries)
                tc_exit   = (tc_dir == 'bearish')
                spot_exit = (not emer_via_tc) and (spot <= ce_sell_strike + EMERGENCY_EXIT_OFFSET)
                if tc_exit or spot_exit:
                    exit_pr = apply_slippage(emer_ltp, is_buy=False)
                    realised_ce = round(exit_pr - emer_entry, 2)
                    window_tc_realised += realised_ce
                    logger.info(
                        f"  [CE-CHUTE] Exit {'TC-bearish' if tc_exit and not spot_exit else 'spot-reversal'} "
                        f"{emer_strike} @ {exit_pr:.1f} at {ts} | P&L: {realised_ce:.1f}")
                    emer_active  = False
                    emer_via_tc  = False
                    emer_entry   = 0.0
                    emer_ltp     = 0.0

        # -----------------------------------------------------------------
        # PE parachute — TC-only trigger
        # -----------------------------------------------------------------
        if buy_expiry_end is not None and opt_df_cache is not None:
            tc_dir_pe = tc_signal_map.get(ts.date())

            # Entry: bearish TC only
            if not pe_chute_active and pe_chute_attempts < EMERGENCY_MAX_ATTEMPTS:
                if tc_dir_pe == 'bearish':
                    stk, pr = select_strike(
                        spot, buy_expiry_end, ts, 'pe', opt_df_cache, EMERGENCY_HEDGE_DELTA)
                    if stk:
                        pe_chute_strike   = stk
                        pe_chute_entry    = apply_slippage(pr, is_buy=True)
                        pe_chute_ltp      = pr
                        pe_chute_df       = opt_df_cache.get((buy_expiry_end, stk, 'pe'))
                        pe_chute_active   = True
                        pe_chute_attempts += 1
                        tc_pe_triggered   = True
                        logger.info(
                            f"  [PE-CHUTE] Entry TC-bearish PE {pe_chute_strike} "
                            f"@ {pe_chute_entry:.1f} at {ts} | spot={spot:.0f}")

                        # Close PE wing (now redundant while PE chute is active)
                        if active_pe_wing_df is not None and pe_wing_entry_eff > 0.0:
                            pe_wing_close_price = apply_slippage(running_pe_wing, is_buy=False)
                            wing_close_pl = round(pe_wing_close_price - pe_wing_entry_eff, 2)
                            window_tc_realised += wing_close_pl
                            logger.info(
                                f"  [PE-WING ] Closed (PE chute active) @ {pe_wing_close_price:.1f} "
                                f"| P&L: {wing_close_pl:.1f}")
                            active_pe_wing_df = None
                            pe_wing_entry_eff  = 0.0
                            pe_wing_df_eff     = None
                            running_pe_wing    = 0.0

            # LTP update
            if pe_chute_active:
                v = get_option_price(pe_chute_df, ts, 'close')
                if v is not None: pe_chute_ltp = v

                # Exit: bullish TC only (PE chute is TC-triggered; no reactive spot exit
                # since spot is normally above pe_sell_strike before any bearish move)
                if tc_dir_pe == 'bullish':
                    exit_pr_pe = apply_slippage(pe_chute_ltp, is_buy=False)
                    realised_pe = round(exit_pr_pe - pe_chute_entry, 2)
                    pe_chute_pl_lock += realised_pe
                    window_tc_realised += realised_pe
                    logger.info(
                        f"  [PE-CHUTE] Exit TC-bullish "
                        f"{pe_chute_strike} @ {exit_pr_pe:.1f} at {ts} | P&L: {realised_pe:.1f}")
                    pe_chute_active = False
                    pe_chute_entry  = 0.0
                    pe_chute_ltp    = 0.0

                    # Rebuy PE wing
                    if ENABLE_SAFETY_WINGS and buy_expiry_end is not None:
                        stk_w, pr_w = select_strike(
                            spot, buy_expiry_end, ts, 'pe', opt_df_cache, SAFETY_WING_DELTA)
                        if stk_w:
                            rebuy_cost        = apply_slippage(pr_w, is_buy=True)
                            window_tc_realised -= rebuy_cost
                            pe_wing_entry_eff  = rebuy_cost
                            pe_wing_df_eff     = opt_df_cache.get((buy_expiry_end, stk_w, 'pe'))
                            active_pe_wing_df  = pe_wing_df_eff
                            running_pe_wing    = pr_w
                            logger.info(
                                f"  [PE-WING ] Rebuyed {stk_w} @ {rebuy_cost:.1f} at {ts}")

        # -----------------------------------------------------------------
        # P&L and peak tracking
        # -----------------------------------------------------------------
        ce_unrealised_pl = calc_strategy_pl(
            ce_sell_entry, running_ce_sell,
            ce_buy_entry,  running_ce_buy,
            ce_wing_entry, running_ce_wing,
            emer_entry, emer_ltp)
        pe_unrealised_pl = calc_strategy_pl(
            pe_sell_entry, running_pe_sell,
            pe_buy_entry,  running_pe_buy,
            pe_wing_entry, running_pe_wing)
        combined_unrealised_pl = round(ce_unrealised_pl + pe_unrealised_pl, 2)
        cumulative_pl = round(
            running_realised_pl + window_tc_realised + combined_unrealised_pl, 2)

        if cumulative_pl > running_peak_pl:
            running_peak_pl = cumulative_pl

        # -----------------------------------------------------------------
        # Exit condition checks
        # -----------------------------------------------------------------
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

    # -----------------------------------------------------------------
    # Close any open chutes at window end (forced exit)
    # -----------------------------------------------------------------
    if emer_active:
        exit_pr = apply_slippage(emer_ltp, is_buy=False)
        realised_ce = round(exit_pr - emer_entry, 2)
        window_tc_realised += realised_ce
        logger.info(
            f"  [CE-CHUTE] Final closure {emer_strike} @ {exit_pr:.1f} | P&L: {realised_ce:.1f}")
        emer_entry  = 0.0
        emer_ltp    = 0.0

    if pe_chute_active:
        exit_pr_pe = apply_slippage(pe_chute_ltp, is_buy=False)
        realised_pe = round(exit_pr_pe - pe_chute_entry, 2)
        pe_chute_pl_lock += realised_pe
        window_tc_realised += realised_pe
        logger.info(
            f"  [PE-CHUTE] Final closure {pe_chute_strike} @ {exit_pr_pe:.1f} | P&L: {realised_pe:.1f}")
        pe_chute_entry  = 0.0
        pe_chute_ltp    = 0.0
        pe_chute_active = False

    return (
        running_ce_sell, running_ce_buy, running_pe_sell, running_pe_buy,  # 0-3
        sl_hit_ts, sl_hit_reason,                                           # 4-5
        running_peak_pl,                                                    # 6
        None, None,                                                         # 7-8: no adjustment
        running_ce_wing, running_pe_wing,                                   # 9-10
        round(window_tc_realised, 2),                                       # 11
        emer_strike, emer_entry, emer_ltp,                                  # 12-14: CE chute
        pe_chute_strike, round(pe_chute_pl_lock, 2),                       # 15-16
        pe_wing_entry_eff, pe_wing_df_eff,                                  # 17-18
        tc_ce_early, tc_pe_triggered,                                       # 19-20
        round(pe_wing_close_price, 2),                                      # 21
    )


# ---------------------------------------------------------------------------
# Main TC backtest loop
# ---------------------------------------------------------------------------

def run_backtest_tc(nifty_1m: pd.DataFrame, vix_1m: pd.DataFrame,
                    contracts_df: pd.DataFrame,
                    holidays_df: pd.DataFrame,
                    tc_signal_map: dict) -> list:
    """
    TC-modified backtest. Mirrors run_backtest() but calls _scan_window_tc
    and records TC-specific columns. Adjustment block omitted (ENABLE_ADJUSTMENT=False).
    """
    os.makedirs(TC_DATA_DIR, exist_ok=True)

    all_trades    = []
    opt_df_cache  = {}
    trade_counter = 0

    holidays_set = set()
    if holidays_df is not None:
        holidays_set = set(holidays_df['date'].values)

    all_expiry_dates = sorted(contracts_df['expiry_date'].dt.date.unique())
    if BACKTEST_START_DATE:
        start = pd.Timestamp(BACKTEST_START_DATE).date()
        all_expiry_dates = [d for d in all_expiry_dates if d >= start]
    if BACKTEST_END_DATE:
        end = pd.Timestamp(BACKTEST_END_DATE).date()
        all_expiry_dates = [d for d in all_expiry_dates if d <= end]

    logger.info(f"Sell expiries in scope: {len(all_expiry_dates)}")

    entry_ts_str = f" {ENTRY_TIME}:00"

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
            logger.info(f"  Progress: {expiry_idx}/{len(all_expiry_dates)} | Trades: {trade_counter}")

        prior_expiry_date = get_prior_expiry(sell_expiry_date, contracts_df)
        if prior_expiry_date is None:
            skip_counts['no_entry_day'] += 1
            continue

        entry_date = compute_entry_date(prior_expiry_date, holidays_set)
        if entry_date is None:
            skip_counts['no_entry_day'] += 1
            continue

        entry_ts = pd.Timestamp(f"{entry_date}{entry_ts_str}")

        spot = get_1min_value(nifty_1m, entry_ts, 'close')
        if spot is None:
            skip_counts['no_spot'] += 1
            continue

        entry_vix = get_1min_value(vix_1m, entry_ts, 'close')

        if ENABLE_VIX_FILTER:
            if entry_vix is None or not (VIX_FILTER_LOW <= entry_vix <= VIX_FILTER_HIGH):
                skip_counts['vix_filtered'] += 1
                continue

        sell_expiry_end = get_end_date(sell_expiry_date, contracts_df)
        elm_time        = get_elm_time(sell_expiry_date, contracts_df)

        buy_expiry_date = select_buy_expiry(entry_date, sell_expiry_date, contracts_df)
        if buy_expiry_date is None:
            skip_counts['no_buy_expiry'] += 1
            continue

        buy_expiry_end = get_end_date(buy_expiry_date, contracts_df)

        if sell_expiry_end is None or buy_expiry_end is None:
            skip_counts['expiry_not_in_contracts'] += 1
            continue

        target_delta_used = get_target_delta(entry_vix) if entry_vix is not None \
            else VIX_DELTA_BANDS[-1][1]

        pe_sell_strike, pe_sell_raw = select_strike(
            spot, sell_expiry_end, entry_ts, 'pe', opt_df_cache, target_delta_used)
        ce_sell_strike, ce_sell_raw = select_strike(
            spot, sell_expiry_end, entry_ts, 'ce', opt_df_cache, target_delta_used)

        if ce_sell_strike is None or pe_sell_strike is None:
            skip_counts['strike_failed'] += 1
            continue

        ce_buy_strike = ce_sell_strike
        pe_buy_strike = pe_sell_strike

        ce_wing_strike = None
        pe_wing_strike = None
        ce_wing_raw    = 0.0
        pe_wing_raw    = 0.0
        if ENABLE_SAFETY_WINGS:
            ce_wing_strike = None
            ce_wing_raw    = 0.0
            pe_wing_strike, pe_wing_raw = select_strike(
                spot, buy_expiry_end, entry_ts, 'pe', opt_df_cache, SAFETY_WING_DELTA)

        # Load option DFs
        ce_sell_df = opt_df_cache.get(
            (sell_expiry_end, ce_sell_strike, 'ce'),
            load_option_data(sell_expiry_end, ce_sell_strike, 'ce'))
        pe_sell_df = opt_df_cache.get(
            (sell_expiry_end, pe_sell_strike, 'pe'),
            load_option_data(sell_expiry_end, pe_sell_strike, 'pe'))

        for key, exp, stk, opt in [
            ((buy_expiry_end, ce_buy_strike,  'ce'), buy_expiry_end, ce_buy_strike,  'ce'),
            ((buy_expiry_end, pe_buy_strike,  'pe'), buy_expiry_end, pe_buy_strike,  'pe'),
        ]:
            if key not in opt_df_cache:
                opt_df_cache[key] = load_option_data(exp, stk, opt)
        ce_buy_df = opt_df_cache[(buy_expiry_end, ce_buy_strike, 'ce')]
        pe_buy_df = opt_df_cache[(buy_expiry_end, pe_buy_strike, 'pe')]

        ce_wing_df = None
        pe_wing_df = None
        if ENABLE_SAFETY_WINGS:
            if pe_wing_strike:
                pe_wing_key = (buy_expiry_end, pe_wing_strike, 'pe')
                if pe_wing_key not in opt_df_cache:
                    opt_df_cache[pe_wing_key] = load_option_data(
                        buy_expiry_end, pe_wing_strike, 'pe')
                pe_wing_df = opt_df_cache[pe_wing_key]

        # Entry pricing
        ce_buy_raw = get_option_price(ce_buy_df, entry_ts, 'open')
        pe_buy_raw = get_option_price(pe_buy_df, entry_ts, 'open')

        if any(v is None for v in [ce_sell_raw, ce_buy_raw, pe_sell_raw, pe_buy_raw]):
            skip_counts['missing_price'] += 1
            continue

        ce_sell_entry = apply_slippage(ce_sell_raw, is_buy=False)
        ce_buy_entry  = apply_slippage(ce_buy_raw,  is_buy=True)
        pe_sell_entry = apply_slippage(pe_sell_raw, is_buy=False)
        pe_buy_entry  = apply_slippage(pe_buy_raw,  is_buy=True)

        ce_wing_entry = 0.0
        pe_wing_entry = 0.0
        if ENABLE_SAFETY_WINGS:
            if pe_wing_df is not None:
                pe_wing_raw_price = get_option_price(pe_wing_df, entry_ts, 'open')
                if pe_wing_raw_price is not None:
                    pe_wing_entry = apply_slippage(pe_wing_raw_price, is_buy=True)

        net_debit_ce = round(ce_buy_entry - ce_sell_entry + ce_wing_entry, 2)
        net_debit_pe = round(pe_buy_entry - pe_sell_entry + pe_wing_entry, 2)
        total_net_debit = round(net_debit_ce + net_debit_pe, 2)

        sell_dte      = max((sell_expiry_date - entry_date).days, 0.5)
        ce_sell_delta = compute_delta(spot, ce_sell_strike, sell_dte, ce_sell_raw, 'ce')
        pe_sell_delta = compute_delta(spot, pe_sell_strike, sell_dte, pe_sell_raw, 'pe')

        max_theoretical_profit = compute_max_theoretical_profit(
            spot, ce_sell_strike, pe_sell_strike,
            sell_expiry_end, buy_expiry_end, entry_ts,
            ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
            ce_sell_entry, pe_sell_entry, ce_buy_entry, pe_buy_entry)

        # -----------------------------------------------------------------
        # Scan 1-min candles with TC logic
        # -----------------------------------------------------------------
        trade_log = []
        scan_start = entry_ts
        scan_end   = sell_expiry_end

        (ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
         sl_ts, sl_reason, running_peak_pl,
         _adj_ts, _adj_side,
         ce_wing_ltp, pe_wing_ltp,
         window_tc_realised,
         emer_strike, emer_entry, emer_exit,
         pe_chute_strike_out, pe_chute_pl_out,
         pe_wing_entry_eff, pe_wing_df_eff,
         tc_ce_early, tc_pe_triggered,
         pe_wing_close_price) = _scan_window_tc(
            scan_start, scan_end,
            nifty_1m, vix_1m,
            ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
            ce_sell_strike, pe_sell_strike,
            ce_sell_entry, ce_buy_entry,
            pe_sell_entry, pe_buy_entry,
            total_net_debit, max_theoretical_profit,
            spot, elm_time, trade_log,
            ce_sell_entry, ce_buy_entry, pe_sell_entry, pe_buy_entry,
            running_realised_pl=0.0,
            running_peak_pl=0.0,
            entry_time=entry_ts,
            sell_expiry_end=sell_expiry_end,
            ce_wing_df=ce_wing_df, pe_wing_df=pe_wing_df,
            ce_wing_entry=ce_wing_entry, pe_wing_entry=pe_wing_entry,
            last_ce_wing_ltp=ce_wing_entry, last_pe_wing_ltp=pe_wing_entry,
            ce_wing_strike=ce_wing_strike, pe_wing_strike=pe_wing_strike,
            opt_df_cache=opt_df_cache, buy_expiry_end=buy_expiry_end,
            tc_signal_map=tc_signal_map)

        running_realised_pl = window_tc_realised

        # -----------------------------------------------------------------
        # Exit pricing
        # -----------------------------------------------------------------
        if sl_ts is None:
            sl_ts     = scan_end
            sl_reason = 'pre_expiry'

        if sl_reason == 'pre_expiry':
            exit_ts = elm_time if elm_time is not None else scan_end
            use_col = 'close'
            slip    = False
        else:
            exit_ts = sl_ts + pd.Timedelta(minutes=1)
            use_col = 'open'
            slip    = True

        def get_exit_price(opt_df, ltp_fallback, is_buy):
            raw = get_option_price(opt_df, exit_ts, use_col) or ltp_fallback
            if slip:
                return apply_slippage(raw, is_buy=is_buy), raw
            return raw, raw

        ce_sell_exit, _ = get_exit_price(ce_sell_df, ce_sell_ltp, is_buy=True)
        ce_buy_exit,  _ = get_exit_price(ce_buy_df,  ce_buy_ltp,  is_buy=False)
        pe_sell_exit, _ = get_exit_price(pe_sell_df, pe_sell_ltp, is_buy=True)
        pe_buy_exit,  _ = get_exit_price(pe_buy_df,  pe_buy_ltp,  is_buy=False)

        ce_wing_exit = ce_wing_ltp
        if ce_wing_df is not None:
            ce_wing_exit, _ = get_exit_price(ce_wing_df, ce_wing_ltp, is_buy=False)

        # PE wing exit uses effective DF (may be rebuyed or None if still sold)
        pe_wing_exit = 0.0
        if pe_wing_df_eff is not None:
            pe_wing_exit, _ = get_exit_price(pe_wing_df_eff, pe_wing_ltp, is_buy=False)

        # P&L using effective wing entries
        ce_pl   = _calc_exit_pl(ce_sell_entry, ce_sell_exit, ce_buy_entry, ce_buy_exit)
        pe_pl   = _calc_exit_pl(pe_sell_entry, pe_sell_exit, pe_buy_entry, pe_buy_exit)
        wing_pl = (ce_wing_exit - ce_wing_entry) + (pe_wing_exit - pe_wing_entry_eff)
        base_pl = round(ce_pl + pe_pl + wing_pl, 2)
        total_pl = round(running_realised_pl + base_pl, 2)

        logger.info(
            f"  TC EXIT {sl_reason:20s} | {exit_ts} | "
            f"base={base_pl:+.1f} realised={running_realised_pl:+.1f} "
            f"total={total_pl:+.1f} pts ({total_pl * LOT_SIZE:+,.0f}) | "
            f"tc_ce={'Y' if tc_ce_early else 'N'} tc_pe={'Y' if tc_pe_triggered else 'N'}")

        # -----------------------------------------------------------------
        # Trade stats from snapshot log
        # -----------------------------------------------------------------
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
                if trade_max_spot is None or s_spot > trade_max_spot: trade_max_spot = s_spot
                if trade_min_spot is None or s_spot < trade_min_spot: trade_min_spot = s_spot
            if s_vix is not None:
                if trade_max_vix is None or s_vix > trade_max_vix: trade_max_vix = s_vix
                if trade_min_vix is None or s_vix < trade_min_vix: trade_min_vix = s_vix
            if s_pl is not None:
                if trade_max_pl is None or s_pl > trade_max_pl:
                    trade_max_pl = s_pl; trade_max_pl_time = s_ts
                if trade_min_pl is None or s_pl < trade_min_pl:
                    trade_min_pl = s_pl; trade_min_pl_time = s_ts

        trade_max_spot = round(trade_max_spot, 2) if trade_max_spot is not None else None
        trade_min_spot = round(trade_min_spot, 2) if trade_min_spot is not None else None
        trade_max_vix  = round(trade_max_vix,  2) if trade_max_vix  is not None else None
        trade_min_vix  = round(trade_min_vix,  2) if trade_min_vix  is not None else None
        trade_max_pl   = round(trade_max_pl,   2) if trade_max_pl   is not None else None
        trade_min_pl   = round(trade_min_pl,   2) if trade_min_pl   is not None else None
        trail_activation_reached = (trade_max_pl is not None and
                                    trade_max_pl >= TRAIL_ACTIVATION_POINTS)

        is_sl_exit = sl_reason in ('index_sl', 'option_sl', 'spread_sl')
        if is_sl_exit and sl_ts is not None:
            sl_trigger_spot_val   = get_1min_value(nifty_1m, sl_ts, 'close') or spot
            sl_triggered_side_val = determine_sl_triggered_side(sl_reason, sl_trigger_spot_val, spot)
            sl_trigger_day_val    = (sl_ts.date() - entry_date).days
            days_remaining_val    = (elm_time.date() - sl_ts.date()).days if elm_time else None
            u_sell = pe_sell_ltp if sl_triggered_side_val == 'ce' else ce_sell_ltp
            u_buy  = pe_buy_ltp  if sl_triggered_side_val == 'ce' else ce_buy_ltp
            untouched_net_val = round(u_buy - u_sell, 2) if u_buy is not None and u_sell is not None else None
        else:
            sl_triggered_side_val = 'none'
            sl_trigger_spot_val   = None
            sl_trigger_day_val    = None
            days_remaining_val    = None
            u_sell = u_buy = None
            untouched_net_val = None

        trade_counter += 1

        # Build baseline record (reuses build_trade_record from backtest.py)
        # Pass pe_wing_entry_eff so wing_pl is computed correctly
        record = build_trade_record(
            entry_ts, spot, entry_vix,
            sell_expiry_date, buy_expiry_date,
            ce_sell_strike, pe_sell_strike,
            ce_sell_entry, ce_buy_entry,
            pe_sell_entry, pe_buy_entry,
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
            u_sell, u_buy, untouched_net_val, days_remaining_val,
            # No adjustment
            adjustment_made=False,
            # Wing fields — use effective PE wing entry for correct P&L
            ce_wing_strike=ce_wing_strike, pe_wing_strike=pe_wing_strike,
            ce_wing_entry=ce_wing_entry, pe_wing_entry=pe_wing_entry_eff,
            ce_wing_exit=ce_wing_exit,    pe_wing_exit=pe_wing_exit,
            # CE chute (emergency hedge)
            emer_strike=emer_strike, emer_entry=emer_entry,
            emer_exit=emer_exit, emer_pl=0.0,
            realised_pl=running_realised_pl,
        )

        # Append TC-specific columns
        record.update({
            'tc_ce_early':          tc_ce_early,
            'tc_pe_triggered':      tc_pe_triggered,
            'pe_chute_strike':      pe_chute_strike_out,
            'pe_chute_pl':          pe_chute_pl_out,
            'pe_wing_entry_orig':   round(pe_wing_entry, 2),
            'pe_wing_close_price':  pe_wing_close_price,
            'pe_wing_entry_eff':    round(pe_wing_entry_eff, 2),
            'pe_wing_exit_eff':     round(pe_wing_exit, 2),
        })

        all_trades.append(record)

    logger.info("Skip reason summary:")
    for reason, count in skip_counts.items():
        if count > 0:
            logger.info(f"  {reason:30s}: {count}")
    logger.info(f"Backtest complete. Total trades: {len(all_trades)}")
    return all_trades


# ---------------------------------------------------------------------------
# Summary save
# ---------------------------------------------------------------------------

def save_summary_tc(all_trades: list):
    """Save TC trade summary and print comparison stats."""
    if not all_trades:
        logger.info("No trades generated.")
        return

    df_tc = pd.DataFrame(all_trades)
    df_tc.to_csv(TC_SUMMARY, index=False)

    total    = len(df_tc)
    winners  = df_tc[df_tc['total_pl_points'] > 0]
    losers   = df_tc[df_tc['total_pl_points'] <= 0]
    win_rate = len(winners) / total * 100 if total > 0 else 0
    avg_win  = winners['total_pl_points'].mean() if len(winners) else 0
    avg_loss = losers['total_pl_points'].mean()  if len(losers)  else 0
    total_rs = df_tc['total_pl_rupees'].sum()

    logger.info("=" * 60)
    logger.info("ATHENA TC BACKTEST SUMMARY")
    logger.info("=" * 60)
    logger.info(f"  Total trades       : {total}")
    logger.info(f"  Winners            : {len(winners)} ({win_rate:.1f}%)")
    logger.info(f"  Losers             : {len(losers)}")
    logger.info(f"  Avg winner (pts)   : {avg_win:.2f}")
    logger.info(f"  Avg loser  (pts)   : {avg_loss:.2f}")
    if avg_loss != 0:
        logger.info(f"  Reward:Risk        : {abs(avg_win / avg_loss):.2f}")
    logger.info(f"  Total P&L (₹)      : {total_rs:+,.0f}")
    logger.info(f"  TC CE early entry  : {df_tc['tc_ce_early'].sum()} trades")
    logger.info(f"  TC PE triggered    : {df_tc['tc_pe_triggered'].sum()} trades")
    logger.info("  Exit breakdown:")
    for reason, count in df_tc['exit_reason'].value_counts().items():
        logger.info(f"    {reason:25s}: {count}")

    # Comparison with baseline if available
    if os.path.exists(BASELINE_SUMMARY):
        df_base = pd.read_csv(BASELINE_SUMMARY)
        base_total = df_base['total_pl_rupees'].sum()
        improvement = total_rs - base_total
        improvement_pct = (improvement / abs(base_total) * 100) if base_total != 0 else 0
        logger.info("=" * 60)
        logger.info("COMPARISON vs BASELINE")
        logger.info(f"  Baseline P&L (₹)   : {base_total:+,.0f}")
        logger.info(f"  TC P&L (₹)         : {total_rs:+,.0f}")
        logger.info(f"  Improvement (₹)    : {improvement:+,.0f}")
        logger.info(f"  Improvement (%)    : {improvement_pct:+.2f}%")

    logger.info("=" * 60)
    logger.info(f"  Saved to: {TC_SUMMARY}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("=== Athena TC Backtest starting ===")
    logger.info(f"  TC signals : {TC_SIGNALS_CSV}")
    logger.info(f"  Output     : {TC_SUMMARY}")

    tc_signal_map = _build_tc_signal_map(TC_SIGNALS_CSV)
    logger.info(f"  TC signals loaded: {len(tc_signal_map)} dates")

    nifty_1m, vix_1m = load_index_data()

    holidays_path = os.path.join(
        REPO_ROOT, 'data_pipeline', 'config', 'holidays.csv')
    holidays_df = pd.read_csv(holidays_path, parse_dates=['date'])
    holidays_df['date'] = pd.to_datetime(holidays_df['date']).dt.date

    contracts_df = load_contracts(holidays_df)
    logger.info(f"  Contracts  : {len(contracts_df)} expiries")
    logger.info(f"  VIX filter : {'ON' if ENABLE_VIX_FILTER else 'OFF'}"
                + (f" ({VIX_FILTER_LOW}–{VIX_FILTER_HIGH})" if ENABLE_VIX_FILTER else ""))

    all_trades = run_backtest_tc(nifty_1m, vix_1m, contracts_df, holidays_df, tc_signal_map)
    save_summary_tc(all_trades)

    logger.info("=== Athena TC Backtest complete ===")
