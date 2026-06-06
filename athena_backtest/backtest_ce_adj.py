"""
backtest_ce_adj.py — Athena CE Buy Leg Roll Adjustment

Parallel research track. Does NOT modify any committed files.

When spot falls CE_ADJ_TRIGGER pts below the PE sell strike, rolls the CE buy
leg (monthly calendar buy) DOWN by CE_ADJ_VALUE pts:

  Trigger : spot <= pe_sell_strike - CE_ADJ_TRIGGER
  Action  : close CE buy (monthly, ce_sell_strike)
            open  CE buy (monthly, ce_sell_strike - CE_ADJ_VALUE)
  Limit   : 1 adjustment per trade

P&L accounting:
  realised_pl includes (roll_close_price - original_ce_buy_entry), locking
  the old leg's gain/loss.  build_trade_record uses the new ce_buy_entry_eff
  for the base P&L, so the two legs combine correctly.

Configure at the top of this file:
  CE_ADJ_TRIGGER : pts below pe_sell_strike to fire the roll  (default 100)
  CE_ADJ_VALUE   : pts to roll CE buy strike down             (default 100)

Output (never overwrites committed baseline):
  athena_backtest/data_ce_adj/trade_summary_ce_adj.csv
  athena_backtest/data_ce_adj/trade_logs_ce_adj/trade_NNNN_YYYY-MM-DD.csv

Usage (from repo root):
  python athena_backtest/backtest_ce_adj.py
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
    _calc_exit_pl, build_trade_record, calc_strategy_pl, build_snapshot,
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
# Configurable adjustment parameters (edit here before running)
# ---------------------------------------------------------------------------
CE_ADJ_TRIGGER = 100   # pts below pe_sell_strike before roll fires
CE_ADJ_VALUE   = 100   # pts to lower the CE buy strike

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_DIR             = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT        = os.path.dirname(_DIR)
ADJ_DATA_DIR     = os.path.join(_DIR, 'data_ce_adj')
ADJ_LOGS_DIR     = os.path.join(ADJ_DATA_DIR, 'trade_logs_ce_adj')
ADJ_SUMMARY      = os.path.join(ADJ_DATA_DIR, 'trade_summary_ce_adj.csv')
BASELINE_SUMMARY = os.path.join(_DIR, 'data', 'trade_summary.csv')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trade log save
# ---------------------------------------------------------------------------

def _save_trade_log_ce_adj(trade_counter: int, entry_time, trade_log: list):
    if not trade_log:
        return
    os.makedirs(ADJ_LOGS_DIR, exist_ok=True)
    entry_str = pd.Timestamp(entry_time).strftime('%Y-%m-%d')
    filepath  = os.path.join(ADJ_LOGS_DIR, f"trade_{trade_counter:04d}_{entry_str}.csv")
    pd.DataFrame(trade_log).to_csv(filepath, index=False)


# ---------------------------------------------------------------------------
# Window scanner
# ---------------------------------------------------------------------------

def _scan_window_ce_adj(
        from_ts: pd.Timestamp, to_ts: pd.Timestamp,
        nifty_1m: pd.DataFrame, vix_1m: pd.DataFrame,
        ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
        ce_sell_strike: int, pe_sell_strike: int,
        ce_sell_entry: float, ce_buy_entry: float,
        pe_sell_entry: float, pe_buy_entry: float,
        total_net_debit: float, max_theoretical_profit: float,
        entry_spot: float, elm_time: pd.Timestamp, trade_log: list,
        last_ce_sell_ltp: float, last_ce_buy_ltp: float,
        last_pe_sell_ltp: float, last_pe_buy_ltp: float,
        running_realised_pl: float = 0.0, running_peak_pl: float = 0.0,
        entry_time: pd.Timestamp = None, sell_expiry_end: pd.Timestamp = None,
        ce_wing_df=None, pe_wing_df=None,
        ce_wing_entry: float = 0.0, pe_wing_entry: float = 0.0,
        last_ce_wing_ltp: float = 0.0, last_pe_wing_ltp: float = 0.0,
        ce_wing_strike: int = None, pe_wing_strike: int = None,
        opt_df_cache: dict = None, buy_expiry_end: pd.Timestamp = None,
        ce_adj_trigger: int = CE_ADJ_TRIGGER,
        ce_adj_value: int = CE_ADJ_VALUE,
):
    """
    CE buy roll scanner. Returns a 22-item tuple:
      0-3:   final running LTPs (ce_sell, ce_buy, pe_sell, pe_buy)
      4-5:   sl_hit_ts, sl_hit_reason
      6:     running_peak_pl
      7-8:   reserved (None, None)
      9-10:  running_ce_wing, running_pe_wing
      11:    window_adj_realised  (roll P&L + CE parachute P&L)
      12-14: emer_strike, emer_entry, emer_ltp  (CE parachute state)
      15:    adj_fired (bool)
      16:    adj_new_ce_buy_strike (or None)
      17:    adj_new_ce_buy_entry
      18:    adj_old_close_price
      19:    ce_buy_entry_eff  (new entry after roll, or original if no roll)
      20:    ce_buy_df_active  (active CE buy DataFrame for exit pricing)
      21:    adj_ts (timestamp when roll fired, or None)
    """
    running_ce_sell = last_ce_sell_ltp
    running_ce_buy  = last_ce_buy_ltp
    running_pe_sell = last_pe_sell_ltp
    running_pe_buy  = last_pe_buy_ltp
    running_ce_wing = last_ce_wing_ltp
    running_pe_wing = last_pe_wing_ltp

    # CE buy roll state
    adj_fired            = False
    adj_attempts         = 0
    adj_ts               = None
    adj_new_ce_buy_stk   = None
    adj_new_ce_buy_entry = 0.0
    adj_old_close_price  = 0.0
    ce_buy_entry_eff     = ce_buy_entry   # updated to new entry after roll
    ce_buy_df_active     = ce_buy_df      # updated to new df after roll

    # Nifty strikes are in 50-pt increments
    new_ce_buy_stk = round((ce_sell_strike - ce_adj_value) / 50) * 50

    window_adj_realised = 0.0

    # CE parachute (identical to baseline)
    emer_active   = False
    emer_strike   = None
    emer_entry    = 0.0
    emer_ltp      = 0.0
    emer_df       = None
    emer_attempts = 0

    sl_hit_ts     = None
    sl_hit_reason = None

    window = nifty_1m[
        (nifty_1m.index > from_ts) & (nifty_1m.index <= to_ts)
    ]

    for ts, row in window.iterrows():
        spot = float(row['close'])
        vix  = get_1min_value(vix_1m, ts, 'close')

        # Update all LTPs
        v = get_option_price(ce_sell_df, ts, 'close')
        if v is not None: running_ce_sell = v
        v = get_option_price(ce_buy_df_active, ts, 'close')
        if v is not None: running_ce_buy = v
        v = get_option_price(pe_sell_df, ts, 'close')
        if v is not None: running_pe_sell = v
        v = get_option_price(pe_buy_df, ts, 'close')
        if v is not None: running_pe_buy = v
        if ce_wing_df is not None:
            v = get_option_price(ce_wing_df, ts, 'close')
            if v is not None: running_ce_wing = v
        if pe_wing_df is not None:
            v = get_option_price(pe_wing_df, ts, 'close')
            if v is not None: running_pe_wing = v

        # -----------------------------------------------------------------
        # CE buy roll adjustment
        # Trigger : spot <= pe_sell_strike - ce_adj_trigger
        # Action  : close CE buy at ce_sell_strike, open at ce_sell_strike - ce_adj_value
        # -----------------------------------------------------------------
        if not adj_fired and adj_attempts < 1 and buy_expiry_end is not None and opt_df_cache is not None:
            if spot <= pe_sell_strike - ce_adj_trigger:
                new_key = (buy_expiry_end, new_ce_buy_stk, 'ce')
                if new_key not in opt_df_cache:
                    opt_df_cache[new_key] = load_option_data(
                        buy_expiry_end, new_ce_buy_stk, 'ce')
                new_df        = opt_df_cache[new_key]
                new_price_raw = get_option_price(new_df, ts, 'close')

                if new_price_raw is not None and new_price_raw > 0:
                    adj_old_close_price  = apply_slippage(running_ce_buy, is_buy=False)
                    adj_new_ce_buy_entry = apply_slippage(new_price_raw, is_buy=True)
                    adj_new_ce_buy_stk   = new_ce_buy_stk

                    # Lock P&L from old CE buy leg into realised
                    window_adj_realised += adj_old_close_price - ce_buy_entry

                    # Switch active CE buy to new leg
                    ce_buy_entry_eff = adj_new_ce_buy_entry
                    ce_buy_df_active = new_df
                    running_ce_buy   = new_price_raw

                    adj_fired    = True
                    adj_attempts = 1
                    adj_ts       = ts

                    logger.info(
                        f"  [CE-ADJ ] Roll CE buy {ce_sell_strike}→{adj_new_ce_buy_stk} "
                        f"| close {adj_old_close_price:.1f} open {adj_new_ce_buy_entry:.1f} "
                        f"at {ts} | spot={spot:.0f}")
                else:
                    logger.warning(
                        f"  [CE-ADJ ] No price for CE {new_ce_buy_stk} at {ts}, skipping")
                    adj_attempts = 1  # don't retry

        # -----------------------------------------------------------------
        # CE parachute — baseline spot logic (unchanged)
        # -----------------------------------------------------------------
        if ENABLE_EMERGENCY_HEDGE and buy_expiry_end is not None and opt_df_cache is not None:
            if not emer_active and emer_attempts < EMERGENCY_MAX_ATTEMPTS:
                if spot >= ce_sell_strike - EMERGENCY_TRIGGER_OFFSET:
                    stk, pr = select_strike(
                        spot, buy_expiry_end, ts, 'ce', opt_df_cache, EMERGENCY_HEDGE_DELTA)
                    if stk:
                        emer_strike   = stk
                        emer_entry    = apply_slippage(pr, is_buy=True)
                        emer_ltp      = pr
                        emer_df       = opt_df_cache.get((buy_expiry_end, stk, 'ce'))
                        emer_active   = True
                        emer_attempts += 1
                        logger.info(
                            f"  [CE-CHUTE] Entry spot-trigger {emer_strike} "
                            f"@ {emer_entry:.1f} at {ts} | spot={spot:.0f}")

            if emer_active:
                v = get_option_price(emer_df, ts, 'close')
                if v is not None: emer_ltp = v
                if spot <= ce_sell_strike + EMERGENCY_EXIT_OFFSET:
                    exit_pr = apply_slippage(emer_ltp, is_buy=False)
                    realised_ce = round(exit_pr - emer_entry, 2)
                    window_adj_realised += realised_ce
                    logger.info(
                        f"  [CE-CHUTE] Exit spot-reversal {emer_strike} "
                        f"@ {exit_pr:.1f} at {ts} | P&L: {realised_ce:.1f}")
                    emer_active = False
                    emer_entry  = 0.0
                    emer_ltp    = 0.0

        # -----------------------------------------------------------------
        # P&L and peak tracking
        # -----------------------------------------------------------------
        ce_unrealised_pl = calc_strategy_pl(
            ce_sell_entry, running_ce_sell,
            ce_buy_entry_eff, running_ce_buy,
            ce_wing_entry, running_ce_wing,
            emer_entry, emer_ltp)
        pe_unrealised_pl = calc_strategy_pl(
            pe_sell_entry, running_pe_sell,
            pe_buy_entry,  running_pe_buy,
            pe_wing_entry, running_pe_wing)
        combined_unrealised_pl = round(ce_unrealised_pl + pe_unrealised_pl, 2)
        cumulative_pl = round(
            running_realised_pl + window_adj_realised + combined_unrealised_pl, 2)

        if cumulative_pl > running_peak_pl:
            running_peak_pl = cumulative_pl

        trade_log.append(build_snapshot(
            ts, spot, vix,
            ce_sell_strike, pe_sell_strike,
            running_ce_sell, running_ce_buy,
            running_pe_sell, running_pe_buy,
            ce_sell_entry, ce_buy_entry_eff,
            pe_sell_entry, pe_buy_entry,
            total_net_debit, max_theoretical_profit,
            running_realised_pl=(running_realised_pl + window_adj_realised),
            ce_wing_ltp=running_ce_wing, ce_wing_entry=ce_wing_entry,
            pe_wing_ltp=running_pe_wing, pe_wing_entry=pe_wing_entry,
            ce_wing_strike=ce_wing_strike, pe_wing_strike=pe_wing_strike,
            emer_strike=emer_strike, emer_entry=emer_entry, emer_ltp=emer_ltp,
        ))

        # -----------------------------------------------------------------
        # Exit condition checks (identical to baseline)
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

    # Close CE parachute at window end if still open
    if emer_active:
        exit_pr     = apply_slippage(emer_ltp, is_buy=False)
        realised_ce = round(exit_pr - emer_entry, 2)
        window_adj_realised += realised_ce
        logger.info(
            f"  [CE-CHUTE] Final closure {emer_strike} @ {exit_pr:.1f} | P&L: {realised_ce:.1f}")
        emer_entry = 0.0
        emer_ltp   = 0.0

    return (
        running_ce_sell, running_ce_buy, running_pe_sell, running_pe_buy,   # 0-3
        sl_hit_ts, sl_hit_reason,                                            # 4-5
        running_peak_pl,                                                     # 6
        None, None,                                                          # 7-8
        running_ce_wing, running_pe_wing,                                   # 9-10
        round(window_adj_realised, 2),                                       # 11
        emer_strike, emer_entry, emer_ltp,                                   # 12-14
        adj_fired,                                                           # 15
        adj_new_ce_buy_stk,                                                  # 16
        round(adj_new_ce_buy_entry, 2),                                      # 17
        round(adj_old_close_price, 2),                                       # 18
        round(ce_buy_entry_eff, 2),                                          # 19
        ce_buy_df_active,                                                    # 20
        adj_ts,                                                              # 21
    )


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------

def run_backtest_ce_adj(nifty_1m: pd.DataFrame, vix_1m: pd.DataFrame,
                        contracts_df: pd.DataFrame,
                        holidays_df: pd.DataFrame,
                        ce_adj_trigger: int = CE_ADJ_TRIGGER,
                        ce_adj_value: int = CE_ADJ_VALUE,
                        opt_df_cache: dict = None,
                        save_logs: bool = True) -> list:
    if opt_df_cache is None:
        opt_df_cache = {}
    os.makedirs(ADJ_DATA_DIR, exist_ok=True)

    all_trades    = []
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
            logger.info(
                f"  Progress: {expiry_idx}/{len(all_expiry_dates)} | Trades: {trade_counter}")

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
        pe_wing_raw    = 0.0
        if ENABLE_SAFETY_WINGS:
            pe_wing_strike, pe_wing_raw = select_strike(
                spot, buy_expiry_end, entry_ts, 'pe', opt_df_cache, SAFETY_WING_DELTA)

        ce_sell_df = opt_df_cache.get(
            (sell_expiry_end, ce_sell_strike, 'ce'),
            load_option_data(sell_expiry_end, ce_sell_strike, 'ce'))
        pe_sell_df = opt_df_cache.get(
            (sell_expiry_end, pe_sell_strike, 'pe'),
            load_option_data(sell_expiry_end, pe_sell_strike, 'pe'))

        for key, exp, stk, opt in [
            ((buy_expiry_end, ce_buy_strike, 'ce'), buy_expiry_end, ce_buy_strike, 'ce'),
            ((buy_expiry_end, pe_buy_strike, 'pe'), buy_expiry_end, pe_buy_strike, 'pe'),
        ]:
            if key not in opt_df_cache:
                opt_df_cache[key] = load_option_data(exp, stk, opt)
        ce_buy_df = opt_df_cache[(buy_expiry_end, ce_buy_strike, 'ce')]
        pe_buy_df = opt_df_cache[(buy_expiry_end, pe_buy_strike, 'pe')]

        pe_wing_df = None
        if ENABLE_SAFETY_WINGS and pe_wing_strike:
            pe_wing_key = (buy_expiry_end, pe_wing_strike, 'pe')
            if pe_wing_key not in opt_df_cache:
                opt_df_cache[pe_wing_key] = load_option_data(
                    buy_expiry_end, pe_wing_strike, 'pe')
            pe_wing_df = opt_df_cache[pe_wing_key]

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
        if ENABLE_SAFETY_WINGS and pe_wing_df is not None:
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
        # Scan window
        # -----------------------------------------------------------------
        trade_log  = []
        scan_start = entry_ts
        scan_end   = sell_expiry_end

        (ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
         sl_ts, sl_reason, running_peak_pl,
         _r0, _r1,
         ce_wing_ltp, pe_wing_ltp,
         window_adj_realised,
         emer_strike, emer_entry, emer_exit,
         adj_fired, adj_new_ce_buy_stk, adj_new_ce_buy_entry,
         adj_old_close_price, ce_buy_entry_eff, ce_buy_df_active,
         adj_ts) = _scan_window_ce_adj(
            scan_start, scan_end,
            nifty_1m, vix_1m,
            ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
            ce_sell_strike, pe_sell_strike,
            ce_sell_entry, ce_buy_entry,
            pe_sell_entry, pe_buy_entry,
            total_net_debit, max_theoretical_profit,
            spot, elm_time, trade_log,
            ce_sell_entry, ce_buy_entry, pe_sell_entry, pe_buy_entry,
            running_realised_pl=0.0, running_peak_pl=0.0,
            entry_time=entry_ts, sell_expiry_end=sell_expiry_end,
            ce_wing_df=None, pe_wing_df=pe_wing_df,
            ce_wing_entry=ce_wing_entry, pe_wing_entry=pe_wing_entry,
            last_ce_wing_ltp=ce_wing_entry, last_pe_wing_ltp=pe_wing_entry,
            ce_wing_strike=ce_wing_strike, pe_wing_strike=pe_wing_strike,
            opt_df_cache=opt_df_cache, buy_expiry_end=buy_expiry_end,
            ce_adj_trigger=ce_adj_trigger, ce_adj_value=ce_adj_value)

        running_realised_pl = window_adj_realised

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
        ce_buy_exit,  _ = get_exit_price(ce_buy_df_active, ce_buy_ltp, is_buy=False)
        pe_sell_exit, _ = get_exit_price(pe_sell_df, pe_sell_ltp, is_buy=True)
        pe_buy_exit,  _ = get_exit_price(pe_buy_df, pe_buy_ltp, is_buy=False)

        pe_wing_exit = 0.0
        if pe_wing_df is not None:
            pe_wing_exit, _ = get_exit_price(pe_wing_df, pe_wing_ltp, is_buy=False)

        ce_pl   = _calc_exit_pl(ce_sell_entry, ce_sell_exit, ce_buy_entry_eff, ce_buy_exit)
        pe_pl   = _calc_exit_pl(pe_sell_entry, pe_sell_exit, pe_buy_entry, pe_buy_exit)
        wing_pl = pe_wing_exit - pe_wing_entry
        base_pl = round(ce_pl + pe_pl + wing_pl, 2)
        total_pl = round(running_realised_pl + base_pl, 2)

        logger.info(
            f"  ADJ EXIT {sl_reason:20s} | {exit_ts} | "
            f"base={base_pl:+.1f} realised={running_realised_pl:+.1f} "
            f"total={total_pl:+.1f} pts ({total_pl * LOT_SIZE:+,.0f}) | "
            f"ce_adj={'Y' if adj_fired else 'N'}")

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
            untouched_net_val = round(u_buy - u_sell, 2) \
                if u_buy is not None and u_sell is not None else None
        else:
            sl_triggered_side_val = 'none'
            sl_trigger_spot_val   = None
            sl_trigger_day_val    = None
            days_remaining_val    = None
            u_sell = u_buy = None
            untouched_net_val = None

        trade_counter += 1

        record = build_trade_record(
            entry_ts, spot, entry_vix,
            sell_expiry_date, buy_expiry_date,
            ce_sell_strike, pe_sell_strike,
            ce_sell_entry, ce_buy_entry_eff,
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
            adjustment_made=adj_fired,
            ce_wing_strike=ce_wing_strike, pe_wing_strike=pe_wing_strike,
            ce_wing_entry=ce_wing_entry, pe_wing_entry=pe_wing_entry,
            ce_wing_exit=0.0, pe_wing_exit=pe_wing_exit,
            emer_strike=emer_strike, emer_entry=emer_entry,
            emer_exit=emer_exit, emer_pl=0.0,
            realised_pl=running_realised_pl,
        )

        record.update({
            'ce_adj_triggered':       adj_fired,
            'ce_adj_ts':              adj_ts,
            'ce_adj_orig_strike':     ce_sell_strike,
            'ce_adj_new_strike':      adj_new_ce_buy_stk,
            'ce_adj_old_close_price': adj_old_close_price,
            'ce_adj_new_entry_price': adj_new_ce_buy_entry,
            'ce_adj_roll_realised':   round(adj_old_close_price - ce_buy_entry, 2)
                                      if adj_fired else 0.0,
        })

        all_trades.append(record)
        if save_logs:
            _save_trade_log_ce_adj(trade_counter, entry_ts, trade_log)

    logger.info("Skip reason summary:")
    for reason, count in skip_counts.items():
        if count > 0:
            logger.info(f"  {reason:30s}: {count}")
    logger.info(f"Backtest complete. Total trades: {len(all_trades)}")
    return all_trades


# ---------------------------------------------------------------------------
# Summary save
# ---------------------------------------------------------------------------

def save_summary_ce_adj(all_trades: list):
    if not all_trades:
        logger.info("No trades generated.")
        return

    df = pd.DataFrame(all_trades)
    df.to_csv(ADJ_SUMMARY, index=False)

    total    = len(df)
    winners  = df[df['total_pl_points'] > 0]
    losers   = df[df['total_pl_points'] <= 0]
    win_rate = len(winners) / total * 100 if total > 0 else 0
    avg_win  = winners['total_pl_points'].mean() if len(winners) else 0
    avg_loss = losers['total_pl_points'].mean()  if len(losers)  else 0
    total_rs = df['total_pl_rupees'].sum()
    n_adj    = int(df['ce_adj_triggered'].sum())

    logger.info("=" * 60)
    logger.info("ATHENA CE ADJ BACKTEST SUMMARY")
    logger.info(f"  Trigger : spot <= pe_sell_strike - {CE_ADJ_TRIGGER} pts")
    logger.info(f"  Action  : CE buy rolled down {CE_ADJ_VALUE} pts")
    logger.info("=" * 60)
    logger.info(f"  Total trades       : {total}")
    logger.info(f"  Winners            : {len(winners)} ({win_rate:.1f}%)")
    logger.info(f"  Losers             : {len(losers)}")
    logger.info(f"  Avg winner (pts)   : {avg_win:.2f}")
    logger.info(f"  Avg loser  (pts)   : {avg_loss:.2f}")
    if avg_loss != 0:
        logger.info(f"  Reward:Risk        : {abs(avg_win / avg_loss):.2f}")
    logger.info(f"  Total P&L (₹)      : {total_rs:+,.0f}")
    logger.info(f"  CE adj triggered   : {n_adj} trades")
    logger.info("  Exit breakdown:")
    for reason, count in df['exit_reason'].value_counts().items():
        logger.info(f"    {reason:25s}: {count}")

    if os.path.exists(BASELINE_SUMMARY):
        df_base = pd.read_csv(BASELINE_SUMMARY)
        base_total  = df_base['total_pl_rupees'].sum()
        improvement = total_rs - base_total
        imp_pct     = (improvement / abs(base_total) * 100) if base_total != 0 else 0
        logger.info(f"\n  Baseline P&L (₹)   : {base_total:+,.0f}")
        logger.info(f"  CE Adj P&L (₹)     : {total_rs:+,.0f}")
        logger.info(f"  Improvement        : {improvement:+,.0f} ({imp_pct:+.1f}%)")

    logger.info(f"  Saved to: {ADJ_SUMMARY}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Pre-committed decision gate — stated before results are seen
    BASELINE_WR = 57.9
    BASELINE_RR = 1.66
    BASELINE_PL = 149_130

    CELLS = [(50, 100), (150, 100), (100, 50), (100, 150)]

    logging.getLogger().setLevel(logging.WARNING)

    print("=== Athena CE Adj 4-Cell Cross Sweep ===", flush=True)
    print(f"Pre-committed gate: WR >= {BASELINE_WR:.1f}%  R:R >= {BASELINE_RR:.2f}  P&L >= ₹{BASELINE_PL:,}", flush=True)
    print(f"Cells (trigger, value): {CELLS}", flush=True)
    print("", flush=True)

    print("Loading data ...", flush=True)
    nifty_1m, vix_1m = load_index_data()

    holidays_path = os.path.join(REPO_ROOT, 'data_pipeline', 'config', 'holidays.csv')
    holidays_df   = pd.read_csv(holidays_path, parse_dates=['date'])
    holidays_df['date'] = pd.to_datetime(holidays_df['date']).dt.date

    contracts_df = load_contracts(holidays_df)
    print(f"Data loaded — {len(contracts_df)} expiries.", flush=True)
    print("", flush=True)

    os.makedirs(ADJ_DATA_DIR, exist_ok=True)
    shared_cache = {}
    results      = []

    for trigger, value in CELLS:
        print(f"Running (trigger={trigger}, value={value}) ...", flush=True)

        all_trades = run_backtest_ce_adj(
            nifty_1m, vix_1m, contracts_df, holidays_df,
            ce_adj_trigger=trigger, ce_adj_value=value,
            opt_df_cache=shared_cache, save_logs=False,
        )

        if not all_trades:
            print("  No trades.", flush=True)
            continue

        df       = pd.DataFrame(all_trades)
        cell_tag = f"t{trigger}_v{value}"
        cell_csv = os.path.join(ADJ_DATA_DIR, f"trade_summary_ce_adj_{cell_tag}.csv")
        df.to_csv(cell_csv, index=False)

        total    = len(df)
        winners  = df[df['total_pl_points'] > 0]
        losers   = df[df['total_pl_points'] <= 0]
        wr       = len(winners) / total * 100 if total else 0.0
        avg_win  = winners['total_pl_points'].mean() if len(winners) else 0.0
        avg_loss = losers['total_pl_points'].mean()  if len(losers)  else 0.0
        rr       = abs(avg_win / avg_loss) if avg_loss else float('inf')
        total_rs = df['total_pl_rupees'].sum()
        n_adj    = int(df['ce_adj_triggered'].sum())

        gate = wr >= BASELINE_WR and rr >= BASELINE_RR and total_rs >= BASELINE_PL

        print(f"  Trades={total}  Fired={n_adj}  WR={wr:.1f}%  R:R={rr:.2f}  "
              f"P&L=₹{total_rs:+,.0f}  Gate={'PASS' if gate else 'FAIL'}", flush=True)
        print(f"  Saved: {cell_csv}", flush=True)
        print("", flush=True)

        results.append(dict(trigger=trigger, value=value, total=total,
                            n_adj=n_adj, wr=round(wr, 1), rr=round(rr, 2),
                            total_rs=round(total_rs), gate=gate))

    print("=" * 72, flush=True)
    print(f"{'trig':>6} {'val':>5} {'trades':>7} {'fired':>6} "
          f"{'WR%':>6} {'R:R':>5} {'P&L (₹)':>12}  gate", flush=True)
    for r in results:
        gate_str = 'PASS' if r['gate'] else 'FAIL'
        print(f"{r['trigger']:>6} {r['value']:>5} {r['total']:>7} {r['n_adj']:>6} "
              f"{r['wr']:>6.1f} {r['rr']:>5.2f} {r['total_rs']:>12,.0f}  {gate_str}", flush=True)
