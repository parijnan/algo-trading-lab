"""
backtest_wing_reactive.py — Reactive PE Wing Backtest

Parallel research track. Does NOT modify any committed files.
Output: athena_backtest/data_wing_reactive/

Strategy:
  - No wing at entry.
  - entry_spot = spot at 10:30 entry. Fixed reference for the entire trade.
  - BUY: spot < entry_spot - WING_TRIGGER_OFFSET and wing not active
      → buy 0.05d monthly PE at current candle close.
  - SELL: wing active and spot > entry_spot
      → sell wing at current candle close.
  No overnight lock, no forced EOD buy. Wing held naturally through overnight/weekends
  until spot recovers above entry_spot.

Compare against baseline (always-on wing, -455.05 pts over 114 trades).

Usage (from repo root):
  python athena_backtest/backtest_wing_reactive.py
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
    _calc_exit_pl,
    check_spread_sl, check_index_sl, check_option_sl,
    check_trail_stop, check_profit_target,
    last_trading_day_before,
)
from configs import (
    BACKTEST_START_DATE, BACKTEST_END_DATE,
    NIFTY_INDEX_FILE, VIX_INDEX_FILE,
    NIFTY_OPTIONS_PATH, CONTRACT_LIST_FILE,
    ENTRY_TIME, STRIKE_STEP, BUY_LEG_MIN_DTE,
    VIX_DELTA_BANDS,
    ENABLE_VIX_FILTER, VIX_FILTER_LOW, VIX_FILTER_HIGH,
    PE_WING_DELTA,
    ENABLE_EMERGENCY_HEDGE, EMERGENCY_HEDGE_DELTA,
    EMERGENCY_TRIGGER_OFFSET, EMERGENCY_EXIT_OFFSET, EMERGENCY_MAX_ATTEMPTS,
    ELM_EXIT_TIME, SLIPPAGE_POINTS, LOT_SIZE, RISK_FREE_RATE,
    REPO_ROOT,
)

# ---------------------------------------------------------------------------
# Paths (set at runtime in main() based on --offset/--pct; defaults for offset=150)
# ---------------------------------------------------------------------------
_DIR        = os.path.dirname(os.path.abspath(__file__))
OUT_DIR     = os.path.join(_DIR, 'data_wing_reactive')
LOGS_DIR    = os.path.join(OUT_DIR, 'trade_logs')
SUMMARY_CSV = os.path.join(OUT_DIR, 'trade_summary_wing_reactive.csv')

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
WING_TRIGGER_OFFSET = 150    # overridden by --offset CLI arg (fixed points)
WING_TRIGGER_PCT    = None   # overridden by --pct CLI arg (% of entry_spot); takes precedence
WING_DELTA          = PE_WING_DELTA  # 0.05

# 1.0 = apple-to-apple vs baseline; 0.50 = realistic sensitivity
WING_SLIPPAGE = 1.0

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def _wing_buy_price(raw: float) -> float:
    return raw + WING_SLIPPAGE


def _wing_sell_price(raw: float) -> float:
    return max(raw - WING_SLIPPAGE, 0.0)


# ---------------------------------------------------------------------------
# Per-trade log helpers
# ---------------------------------------------------------------------------

def _build_snapshot(
    ts, spot, vix,
    entry_spot,
    trigger_offset,
    ce_sell_strike, pe_sell_strike,
    ce_sell_entry, ce_buy_entry,
    pe_sell_entry, pe_buy_entry,
    ce_sell_ltp, ce_buy_ltp,
    pe_sell_ltp, pe_buy_ltp,
    wing_active, wing_strike, wing_entry, wing_ltp,
    wing_realised_pl,
    emer_active, emer_strike, emer_entry, emer_ltp,
    emer_realised_pl,
) -> dict:
    ce_unreal = round((ce_sell_entry - ce_sell_ltp) + (ce_buy_ltp - ce_buy_entry), 2)
    pe_unreal = round((pe_sell_entry - pe_sell_ltp) + (pe_buy_ltp - pe_buy_entry), 2)
    wing_unreal = round(wing_ltp - wing_entry, 2) if wing_active else 0.0
    emer_unreal = round(emer_ltp - emer_entry, 2) if emer_active else 0.0
    combined = round(ce_unreal + pe_unreal + wing_unreal + emer_unreal, 2)
    cumulative = round(wing_realised_pl + emer_realised_pl + combined, 2)
    return {
        'time_stamp':           ts,
        'spot':                 round(spot, 2),
        'vix':                  round(vix, 2) if vix is not None else None,
        'entry_spot':           round(entry_spot, 2),
        'wing_trigger_level':   round(entry_spot - trigger_offset, 2),
        'ce_sell_strike':       ce_sell_strike,
        'pe_sell_strike':       pe_sell_strike,
        'ce_sell_ltp':          round(ce_sell_ltp, 2),
        'ce_buy_ltp':           round(ce_buy_ltp,  2),
        'pe_sell_ltp':          round(pe_sell_ltp, 2),
        'pe_buy_ltp':           round(pe_buy_ltp,  2),
        'wing_active':          wing_active,
        'wing_strike':          wing_strike,
        'wing_entry':           round(wing_entry, 2) if wing_active else None,
        'wing_ltp':             round(wing_ltp, 2)   if wing_active else None,
        'wing_unrealised_pl':   wing_unreal,
        'wing_realised_pl':     round(wing_realised_pl, 2),
        'emer_active':          emer_active,
        'emer_strike':          emer_strike,
        'emer_ltp':             round(emer_ltp, 2) if emer_active else None,
        'ce_unrealised_pl':     ce_unreal,
        'pe_unrealised_pl':     pe_unreal,
        'combined_unrealised_pl': combined,
        'cumulative_pl':        cumulative,
    }


def _save_trade_log(trade_counter: int, entry_ts: pd.Timestamp, trade_log: list):
    if not trade_log:
        return
    filename = f"trade_{trade_counter:04d}_{entry_ts.strftime('%Y-%m-%d')}.csv"
    filepath = os.path.join(LOGS_DIR, filename)
    pd.DataFrame(trade_log).to_csv(filepath, index=False)
    logger.debug(f'  Trade log saved: {filename} ({len(trade_log)} rows)')


# ---------------------------------------------------------------------------
# Per-trade 1-min scanner — reactive wing only
# ---------------------------------------------------------------------------

def _scan_reactive(
    scan_start, scan_end,
    nifty_1m, vix_1m,
    ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
    ce_sell_strike, pe_sell_strike,
    ce_sell_entry, ce_buy_entry,
    pe_sell_entry, pe_buy_entry,
    total_net_debit, max_theoretical_profit,
    entry_spot,
    trigger_offset,
    elm_time,
    buy_expiry_end,
    opt_df_cache,
    trade_log: list,
):
    # Main leg LTPs
    running_ce_sell = ce_sell_entry
    running_ce_buy  = ce_buy_entry
    running_pe_sell = pe_sell_entry
    running_pe_buy  = pe_buy_entry

    # Emergency hedge state
    emer_active      = False
    emer_strike      = None
    emer_entry_val   = 0.0
    emer_ltp_val     = 0.0
    emer_df          = None
    emer_attempts    = 0
    window_emer_realised = 0.0

    # Reactive wing state
    wing_active   = False
    wing_df       = None
    wing_strike_v = None
    wing_entry_v  = 0.0
    wing_ltp_v    = 0.0

    # Wing P&L accumulators
    wing_total_pl    = 0.0
    wing_tx_count    = 0
    wing_slippage_paid = 0.0

    sl_hit_ts     = None
    sl_hit_reason = None
    running_peak_pl = 0.0

    window = nifty_1m[
        (nifty_1m.index > scan_start) & (nifty_1m.index <= scan_end)
    ]

    for ts, row in window.iterrows():
        spot = float(row['close'])

        # ---- Update main leg LTPs ----
        v = get_option_price(ce_sell_df, ts, 'close')
        if v is not None: running_ce_sell = v
        v = get_option_price(ce_buy_df,  ts, 'close')
        if v is not None: running_ce_buy  = v
        v = get_option_price(pe_sell_df, ts, 'close')
        if v is not None: running_pe_sell = v
        v = get_option_price(pe_buy_df,  ts, 'close')
        if v is not None: running_pe_buy  = v

        if wing_active and wing_df is not None:
            v = get_option_price(wing_df, ts, 'close')
            if v is not None: wing_ltp_v = v

        # ---- Emergency hedge ----
        if ENABLE_EMERGENCY_HEDGE and buy_expiry_end is not None and opt_df_cache is not None:
            if not emer_active and emer_attempts < EMERGENCY_MAX_ATTEMPTS:
                if spot >= ce_sell_strike - EMERGENCY_TRIGGER_OFFSET:
                    stk, pr = select_strike(spot, buy_expiry_end, ts, 'ce',
                                            opt_df_cache, EMERGENCY_HEDGE_DELTA)
                    if stk:
                        emer_strike    = stk
                        emer_entry_val = apply_slippage(pr, is_buy=True)
                        emer_ltp_val   = pr
                        emer_df        = opt_df_cache.get((buy_expiry_end, stk, 'ce'))
                        emer_active    = True
                        emer_attempts += 1
                        logger.info(f'  [EMER] Bought CE {emer_strike} @ {emer_entry_val:.1f} '
                                    f'at {ts} | spot={spot:.0f}')
            if emer_active:
                v = get_option_price(emer_df, ts, 'close')
                if v is not None: emer_ltp_val = v
                if spot <= ce_sell_strike + EMERGENCY_EXIT_OFFSET:
                    exit_pr = apply_slippage(emer_ltp_val, is_buy=False)
                    realised = round(exit_pr - emer_entry_val, 2)
                    window_emer_realised += realised
                    logger.info(f'  [EMER] Sold CE {emer_strike} @ {exit_pr:.1f} '
                                f'at {ts} | pl={realised:.1f} | spot={spot:.0f}')
                    emer_active    = False
                    emer_entry_val = 0.0
                    emer_ltp_val   = 0.0

        # ---- Reactive wing buy ----
        if not wing_active and spot < entry_spot - trigger_offset:
            stk, pr = select_strike(spot, buy_expiry_end, ts, 'pe',
                                    opt_df_cache, WING_DELTA)
            if stk:
                key = (buy_expiry_end, stk, 'pe')
                if key not in opt_df_cache:
                    opt_df_cache[key] = load_option_data(buy_expiry_end, stk, 'pe')
                wing_df       = opt_df_cache[key]
                wing_entry_v  = _wing_buy_price(pr)
                wing_ltp_v    = pr
                wing_strike_v = stk
                wing_active   = True
                wing_tx_count      += 1
                wing_slippage_paid += WING_SLIPPAGE
                logger.debug(f'  [WING-BUY] {wing_strike_v} @ {wing_entry_v:.2f} '
                             f'at {ts} | spot={spot:.0f} entry={entry_spot:.0f}')

        # ---- Reactive wing exit ----
        if wing_active and spot > entry_spot:
            raw_exit = get_option_price(wing_df, ts, 'close') or wing_ltp_v
            exit_px  = _wing_sell_price(raw_exit)
            net_pl   = round(exit_px - wing_entry_v, 2)
            wing_total_pl      += net_pl
            wing_slippage_paid += WING_SLIPPAGE
            logger.debug(f'  [WING-SELL] {wing_strike_v} @ {exit_px:.2f} | '
                         f'pl={net_pl:.2f} | spot={spot:.0f}')
            wing_active   = False
            wing_df       = None
            wing_strike_v = None
            wing_entry_v  = 0.0
            wing_ltp_v    = 0.0

        # ---- P&L and exit checks ----
        ce_unrealised   = (ce_sell_entry - running_ce_sell) + (running_ce_buy - ce_buy_entry)
        pe_unrealised   = (pe_sell_entry - running_pe_sell) + (running_pe_buy - pe_buy_entry)
        emer_unrealised = emer_ltp_val - emer_entry_val
        wing_unrealised = wing_ltp_v - wing_entry_v if wing_active else 0.0
        combined_unrealised = round(
            ce_unrealised + pe_unrealised + emer_unrealised + wing_unrealised, 2)
        cumulative_pl = round(
            window_emer_realised + wing_total_pl + combined_unrealised, 2)

        if cumulative_pl > running_peak_pl:
            running_peak_pl = cumulative_pl

        vix_val = get_1min_value(vix_1m, ts, 'close')
        trade_log.append(_build_snapshot(
            ts, spot, vix_val,
            entry_spot,
            trigger_offset,
            ce_sell_strike, pe_sell_strike,
            ce_sell_entry, ce_buy_entry,
            pe_sell_entry, pe_buy_entry,
            running_ce_sell, running_ce_buy,
            running_pe_sell, running_pe_buy,
            wing_active, wing_strike_v, wing_entry_v, wing_ltp_v,
            wing_total_pl,
            emer_active, emer_strike, emer_entry_val, emer_ltp_val,
            window_emer_realised,
        ))

        if elm_time is not None and ts >= elm_time:
            sl_hit_ts, sl_hit_reason = ts, 'pre_expiry'; break
        if check_spread_sl(combined_unrealised):
            sl_hit_ts, sl_hit_reason = ts, 'spread_sl'; break
        if check_index_sl(spot, ce_sell_strike, pe_sell_strike):
            sl_hit_ts, sl_hit_reason = ts, 'index_sl'; break
        if check_option_sl(running_ce_sell, ce_sell_entry, running_pe_sell, pe_sell_entry):
            sl_hit_ts, sl_hit_reason = ts, 'option_sl'; break
        if check_trail_stop(combined_unrealised, running_peak_pl):
            sl_hit_ts, sl_hit_reason = ts, 'trail_stop'; break
        if check_profit_target(combined_unrealised, total_net_debit):
            sl_hit_ts, sl_hit_reason = ts, 'profit_target'; break

    if emer_active:
        exit_pr = apply_slippage(emer_ltp_val, is_buy=False)
        window_emer_realised += round(exit_pr - emer_entry_val, 2)
        emer_entry_val = 0.0; emer_ltp_val = 0.0

    return (
        running_ce_sell, running_ce_buy, running_pe_sell, running_pe_buy,
        sl_hit_ts, sl_hit_reason, running_peak_pl,
        round(window_emer_realised, 2),
        emer_strike, emer_entry_val, emer_ltp_val,
        wing_active, wing_df, wing_strike_v, wing_entry_v, wing_ltp_v,
        round(wing_total_pl, 2),
        wing_tx_count,
        round(wing_slippage_paid, 2),
    )


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------

def run_backtest(nifty_1m, vix_1m, contracts_df, holidays_df=None):
    os.makedirs(LOGS_DIR, exist_ok=True)

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

    logger.info(f'Sell expiries in scope: {len(all_expiry_dates)}')

    entry_ts_str = f' {ENTRY_TIME}:00'
    skip_counts = {k: 0 for k in (
        'no_entry_day', 'no_spot', 'vix_filtered', 'no_buy_expiry',
        'expiry_not_in_contracts', 'strike_failed', 'missing_price')}

    for expiry_idx, sell_expiry_date in enumerate(all_expiry_dates, 1):

        if expiry_idx % 50 == 0 or expiry_idx == len(all_expiry_dates):
            logger.info(f'  Progress: {expiry_idx}/{len(all_expiry_dates)} expiries | '
                        f'Trades: {trade_counter}')

        prior_expiry_date = get_prior_expiry(sell_expiry_date, contracts_df)
        if prior_expiry_date is None:
            skip_counts['no_entry_day'] += 1; continue

        entry_date = compute_entry_date(prior_expiry_date, holidays_set)
        if entry_date is None:
            skip_counts['no_entry_day'] += 1; continue

        entry_ts = pd.Timestamp(f'{entry_date}{entry_ts_str}')

        spot = get_1min_value(nifty_1m, entry_ts, 'close')
        if spot is None:
            skip_counts['no_spot'] += 1; continue

        entry_vix = get_1min_value(vix_1m, entry_ts, 'close')
        if ENABLE_VIX_FILTER:
            if entry_vix is None or not (VIX_FILTER_LOW <= entry_vix <= VIX_FILTER_HIGH):
                skip_counts['vix_filtered'] += 1; continue

        sell_expiry_end = get_end_date(sell_expiry_date, contracts_df)
        elm_time        = get_elm_time(sell_expiry_date, contracts_df)

        buy_expiry_date = select_buy_expiry(entry_date, sell_expiry_date, contracts_df)
        if buy_expiry_date is None:
            skip_counts['no_buy_expiry'] += 1; continue

        buy_expiry_end = get_end_date(buy_expiry_date, contracts_df)
        if sell_expiry_end is None or buy_expiry_end is None:
            skip_counts['expiry_not_in_contracts'] += 1; continue

        target_delta_used = get_target_delta(entry_vix) if entry_vix is not None \
            else VIX_DELTA_BANDS[-1][1]

        pe_sell_strike, pe_sell_raw = select_strike(
            spot, sell_expiry_end, entry_ts, 'pe', opt_df_cache, target_delta_used)
        ce_sell_strike, ce_sell_raw = select_strike(
            spot, sell_expiry_end, entry_ts, 'ce', opt_df_cache, target_delta_used)

        if ce_sell_strike is None or pe_sell_strike is None:
            skip_counts['strike_failed'] += 1
            logger.info(f'  {sell_expiry_date}: Strike selection failed — '
                        f'entry={entry_date} spot={spot:.0f} — skipping')
            continue

        ce_buy_strike = ce_sell_strike
        pe_buy_strike = pe_sell_strike

        for key, expiry, strike, otype in [
            ((sell_expiry_end, ce_sell_strike, 'ce'), sell_expiry_end, ce_sell_strike, 'ce'),
            ((sell_expiry_end, pe_sell_strike, 'pe'), sell_expiry_end, pe_sell_strike, 'pe'),
            ((buy_expiry_end,  ce_buy_strike,  'ce'), buy_expiry_end,  ce_buy_strike,  'ce'),
            ((buy_expiry_end,  pe_buy_strike,  'pe'), buy_expiry_end,  pe_buy_strike,  'pe'),
        ]:
            if key not in opt_df_cache:
                opt_df_cache[key] = load_option_data(expiry, strike, otype)

        ce_sell_df = opt_df_cache[(sell_expiry_end, ce_sell_strike, 'ce')]
        pe_sell_df = opt_df_cache[(sell_expiry_end, pe_sell_strike, 'pe')]
        ce_buy_df  = opt_df_cache[(buy_expiry_end,  ce_buy_strike,  'ce')]
        pe_buy_df  = opt_df_cache[(buy_expiry_end,  pe_buy_strike,  'pe')]

        ce_buy_raw = get_option_price(ce_buy_df, entry_ts, 'open')
        pe_buy_raw = get_option_price(pe_buy_df, entry_ts, 'open')

        if any(v is None for v in [ce_sell_raw, ce_buy_raw, pe_sell_raw, pe_buy_raw]):
            skip_counts['missing_price'] += 1
            logger.info(f'  {entry_date}: Missing option price — skipping')
            continue

        ce_sell_entry = apply_slippage(ce_sell_raw, is_buy=False)
        ce_buy_entry  = apply_slippage(ce_buy_raw,  is_buy=True)
        pe_sell_entry = apply_slippage(pe_sell_raw, is_buy=False)
        pe_buy_entry  = apply_slippage(pe_buy_raw,  is_buy=True)

        net_debit_ce    = round(ce_buy_entry - ce_sell_entry, 2)
        net_debit_pe    = round(pe_buy_entry - pe_sell_entry, 2)
        total_net_debit = round(net_debit_ce + net_debit_pe, 2)

        sell_dte      = max((sell_expiry_date - entry_date).days, 0.5)
        ce_sell_delta = compute_delta(spot, ce_sell_strike, sell_dte, ce_sell_raw, 'ce')
        pe_sell_delta = compute_delta(spot, pe_sell_strike, sell_dte, pe_sell_raw, 'pe')

        max_theoretical_profit = compute_max_theoretical_profit(
            spot, ce_sell_strike, pe_sell_strike,
            sell_expiry_end, buy_expiry_end, entry_ts,
            ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
            ce_sell_entry, pe_sell_entry, ce_buy_entry, pe_buy_entry)

        trigger_offset = (spot * WING_TRIGGER_PCT / 100.0) if WING_TRIGGER_PCT is not None \
                         else WING_TRIGGER_OFFSET

        logger.info(f'  ENTRY {entry_date} | Spot: {spot:.0f} | '
                    f'CE sell {ce_sell_strike} @ {ce_sell_entry:.1f} | '
                    f'PE sell {pe_sell_strike} @ {pe_sell_entry:.1f} | '
                    f'Net debit: {total_net_debit:.1f} | '
                    f'Sell exp: {sell_expiry_date} | Buy exp: {buy_expiry_date} | '
                    f'Wing trigger: -{trigger_offset:.1f} pts')

        trade_log = []
        (ce_sell_ltp, ce_buy_ltp, pe_sell_ltp, pe_buy_ltp,
         sl_ts, sl_reason, running_peak_pl,
         emer_realised_pl,
         emer_strike, emer_entry, emer_exit,
         wing_active, wing_df, wing_strike, wing_entry, wing_ltp,
         wing_total_pl, wing_tx_count, wing_slippage_paid) = _scan_reactive(
            entry_ts, sell_expiry_end,
            nifty_1m, vix_1m,
            ce_sell_df, pe_sell_df, ce_buy_df, pe_buy_df,
            ce_sell_strike, pe_sell_strike,
            ce_sell_entry, ce_buy_entry,
            pe_sell_entry, pe_buy_entry,
            total_net_debit, max_theoretical_profit,
            spot,
            trigger_offset,
            elm_time, buy_expiry_end, opt_df_cache,
            trade_log,
        )

        if sl_ts is None:
            sl_ts     = sell_expiry_end
            sl_reason = 'pre_expiry'

        if sl_reason == 'pre_expiry':
            exit_ts  = elm_time if elm_time is not None else sell_expiry_end
            use_col  = 'close'
            slip     = False
        else:
            exit_ts  = sl_ts + pd.Timedelta(minutes=1)
            use_col  = 'open'
            slip     = True

        def _exit(opt_df, ltp_fallback, is_buy):
            raw = get_option_price(opt_df, exit_ts, use_col) or ltp_fallback
            if slip:
                return apply_slippage(raw, is_buy=is_buy)
            return raw

        ce_sell_exit = _exit(ce_sell_df, ce_sell_ltp, is_buy=True)
        ce_buy_exit  = _exit(ce_buy_df,  ce_buy_ltp,  is_buy=False)
        pe_sell_exit = _exit(pe_sell_df, pe_sell_ltp, is_buy=True)
        pe_buy_exit  = _exit(pe_buy_df,  pe_buy_ltp,  is_buy=False)

        ce_pl = _calc_exit_pl(ce_sell_entry, ce_sell_exit, ce_buy_entry, ce_buy_exit)
        pe_pl = _calc_exit_pl(pe_sell_entry, pe_sell_exit, pe_buy_entry, pe_buy_exit)

        # Close open wing at final exit
        final_wing_pl = 0.0
        if wing_active and wing_df is not None:
            raw_exit = get_option_price(wing_df, exit_ts, use_col) or wing_ltp
            if slip:
                exit_px = _wing_sell_price(raw_exit)
                wing_slippage_paid += WING_SLIPPAGE
            else:
                exit_px = raw_exit
            final_wing_pl = round(exit_px - wing_entry, 2)
            wing_total_pl += final_wing_pl
            logger.debug(f'  [WING-FINAL] {wing_strike} @ {exit_px:.2f} | '
                         f'pl={final_wing_pl:.2f}')

        emer_exit_val = apply_slippage(emer_exit, is_buy=False) if (slip and emer_exit) \
                        else (emer_exit or 0.0)
        emer_pl = round((emer_exit_val - emer_entry) + emer_realised_pl, 2) \
                  if emer_entry else round(emer_realised_pl, 2)

        total_pl = round(ce_pl + pe_pl + wing_total_pl + emer_pl, 2)
        total_rs = round(total_pl * LOT_SIZE, 2)

        exit_spot = get_1min_value(nifty_1m, exit_ts, 'close') or spot

        trade_counter += 1
        _save_trade_log(trade_counter, entry_ts, trade_log)

        record = {
            'entry_time':       entry_ts,
            'entry_spot':       round(spot, 2),
            'entry_vix':        round(entry_vix, 2) if entry_vix else None,
            'sell_expiry':      sell_expiry_date,
            'buy_expiry':       buy_expiry_date,
            'ce_sell_strike':   ce_sell_strike,
            'pe_sell_strike':   pe_sell_strike,
            'ce_sell_entry':    round(ce_sell_entry, 2),
            'ce_buy_entry':     round(ce_buy_entry,  2),
            'pe_sell_entry':    round(pe_sell_entry, 2),
            'pe_buy_entry':     round(pe_buy_entry,  2),
            'ce_sell_delta':    round(ce_sell_delta, 4) if ce_sell_delta else None,
            'pe_sell_delta':    round(pe_sell_delta, 4) if pe_sell_delta else None,
            'target_delta':     round(target_delta_used, 4),
            'net_debit':        total_net_debit,
            'exit_time':        exit_ts,
            'exit_spot':        round(exit_spot, 2),
            'exit_reason':      sl_reason,
            'ce_sell_exit':     round(ce_sell_exit, 2),
            'ce_buy_exit':      round(ce_buy_exit,  2),
            'pe_sell_exit':     round(pe_sell_exit, 2),
            'pe_buy_exit':      round(pe_buy_exit,  2),
            'ce_pl_points':     round(ce_pl, 2),
            'pe_pl_points':     round(pe_pl, 2),
            'emer_strike':      emer_strike,
            'emer_entry':       round(emer_entry, 2) if emer_entry else 0.0,
            'emer_pl':          round(emer_pl, 2),
            'wing_total_pl':    round(wing_total_pl, 2),
            'wing_tx_count':    wing_tx_count,
            'wing_slippage':    round(wing_slippage_paid, 2),
            'total_pl_points':  total_pl,
            'total_pl_rupees':  total_rs,
        }
        all_trades.append(record)

    return all_trades


# ---------------------------------------------------------------------------
# Summary and save
# ---------------------------------------------------------------------------

def save_and_summarise(all_trades):
    if not all_trades:
        logger.info('No trades generated.')
        return

    df = pd.DataFrame(all_trades)
    df.to_csv(SUMMARY_CSV, index=False)

    total   = len(df)
    winners = df[df['total_pl_points'] > 0]
    losers  = df[df['total_pl_points'] <= 0]
    wr      = len(winners) / total * 100 if total > 0 else 0
    avg_win  = winners['total_pl_points'].mean() if len(winners) else 0
    avg_loss = losers['total_pl_points'].mean()  if len(losers)  else 0

    streak = max_streak = 0
    for pl in df['total_pl_points']:
        if pl <= 0:
            streak += 1; max_streak = max(max_streak, streak)
        else:
            streak = 0

    total_pl_rs = df['total_pl_rupees'].sum()
    w_total   = df['wing_total_pl'].sum()
    w_tx      = df['wing_tx_count'].sum()
    w_slip    = df['wing_slippage'].sum()
    no_wing   = df['total_pl_points'].sum() - w_total

    logger.info('=' * 62)
    logger.info('REACTIVE WING BACKTEST SUMMARY')
    logger.info('=' * 62)
    logger.info(f'  Total trades       : {total}')
    logger.info(f'  Winners            : {len(winners)} ({wr:.1f}%)')
    logger.info(f'  Losers             : {len(losers)}')
    logger.info(f'  Avg winner (pts)   : {avg_win:.2f}')
    logger.info(f'  Avg loser  (pts)   : {avg_loss:.2f}')
    if avg_loss != 0:
        logger.info(f'  Reward:Risk        : {abs(avg_win / avg_loss):.2f}')
    logger.info(f'  Max consec losses  : {max_streak}')
    logger.info(f'  Total P&L (₹)      : {total_pl_rs:+,.0f}')
    logger.info('  --')
    logger.info(f'  Wing total P&L (pts): {w_total:+.2f}  '
                f'(baseline: -455.05 pts over 114 trades)')
    logger.info(f'  Wing transactions   : {w_tx} buys '
                f'({w_tx / total:.2f} avg/trade)')
    logger.info(f'  Wing slippage total : {w_slip:.1f} pts '
                f'(WING_SLIPPAGE={WING_SLIPPAGE})')
    logger.info(f'  --')
    logger.info(f'  Base (CE+PE+emer) total: {no_wing:+.2f} pts  '
                f'(baseline without wing: ~2294.35 pts)')
    logger.info('=' * 62)
    logger.info(f'  Saved: {SUMMARY_CSV}')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Reactive PE wing backtest')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--offset', type=int, default=None,
                       help='Fixed pts below entry_spot to trigger wing buy')
    group.add_argument('--pct', type=float, default=None,
                       help='%% of entry_spot below which wing is triggered (e.g. 1.5)')
    args = parser.parse_args()

    global WING_TRIGGER_OFFSET, WING_TRIGGER_PCT, OUT_DIR, LOGS_DIR, SUMMARY_CSV

    if args.pct is not None:
        WING_TRIGGER_PCT = args.pct
        pct_tag = f'pct_{int(args.pct * 100):03d}'
        OUT_DIR     = os.path.join(_DIR, f'data_wing_reactive_{pct_tag}')
        LOGS_DIR    = os.path.join(OUT_DIR, 'trade_logs')
        SUMMARY_CSV = os.path.join(OUT_DIR, f'trade_summary_wing_reactive_{pct_tag}.csv')
        trigger_desc = f'{args.pct}% of entry_spot (spot-normalised)'
    else:
        offset = args.offset if args.offset is not None else 150
        WING_TRIGGER_OFFSET = offset
        OUT_DIR     = os.path.join(_DIR, f'data_wing_reactive_{offset:03d}')
        LOGS_DIR    = os.path.join(OUT_DIR, 'trade_logs')
        SUMMARY_CSV = os.path.join(OUT_DIR, f'trade_summary_wing_reactive_{offset:03d}.csv')
        trigger_desc = f'{offset} pts below entry_spot (fixed)'

    logger.info('=== Reactive Wing Backtest ===')
    logger.info(f'  WING_SLIPPAGE   = {WING_SLIPPAGE} pts/transaction')
    logger.info(f'  Wing trigger    : {trigger_desc}')
    logger.info(f'  Exit trigger    : spot > entry_spot (no overnight lock)')
    logger.info(f'  Output dir      : {OUT_DIR}')

    nifty_1m, vix_1m = load_index_data()

    holidays_df  = None
    holiday_path = os.path.join(REPO_ROOT, 'data_pipeline', 'config', 'holidays.csv')
    if os.path.exists(holiday_path):
        holidays_df = pd.read_csv(holiday_path, parse_dates=['date'])
        holidays_df['date'] = holidays_df['date'].dt.date

    contracts_df = load_contracts(holidays_df)
    all_trades   = run_backtest(nifty_1m, vix_1m, contracts_df, holidays_df)
    save_and_summarise(all_trades)
    logger.info('=== Reactive Wing Backtest complete ===')


if __name__ == '__main__':
    main()
