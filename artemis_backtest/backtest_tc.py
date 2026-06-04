"""
backtest_tc.py — Artemis TRIPLE_CONFIRM Parallel Backtest

Parallel research track. Does NOT modify any committed files.

Identical to backtest.py except: when TRIPLE_CONFIRM fires against an open
spread leg, that leg is exited pre-emptively (before index_sl or option_sl
can trigger). The surviving leg is then adjusted via the same adjust_spread()
path used by the SL-triggered case.

TC signal detection uses the pre-computed TRIPLE_CONFIRM_sensex_excursions.csv
(avoids the iris_backtest/configs.py vs artemis_backtest/configs.py module
collision at import time). Re-run validate_sensex.py if Sensex data has
been updated.

Output (never overwrites committed baseline):
  artemis_backtest/data_tc/trade_summary_tc.csv
  artemis_backtest/data_tc/trade_logs_tc/

Usage (from repo root):
  python artemis_backtest/backtest_tc.py
"""

import os
import sys
import logging
import warnings
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

warnings.filterwarnings('ignore')

# ---------------------------------------------------------------------------
# Import strategy helpers from committed backtest.py — nothing there changes
# ---------------------------------------------------------------------------
from backtest import (
    get_vix_band, compute_dte,
    make_spread, set_sl, check_sl,
    select_sell_strike,
    load_spread_dfs, execute_spread_at, update_ltps,
    exit_spread_at, exit_spread_at_expiry,
    adjust_spread,
    handle_elm,
    _make_log_row, _build_summary_record,
    INDEX_FILE, INSTRUMENT, LOT_SIZE, LOT_COUNT, HEDGE_POINTS,
    VIX_THRESHOLD, BACKTEST_START_DATE, BACKTEST_END_DATE,
    CONTRACTS_FILE, EXPIRY_FALLBACK_PRICE, ENABLE_TRADE_LOGS,
    VIX_INDEX_FILE,
)
from data_loader import load_index_data, load_vix_daily, get_index_price

# ---------------------------------------------------------------------------
# TC-specific output paths (isolated from committed data/)
# ---------------------------------------------------------------------------
_DIR            = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT       = os.path.dirname(_DIR)
TC_DATA_DIR     = os.path.join(_DIR, 'data_tc')
TC_LOGS_DIR     = os.path.join(TC_DATA_DIR, f'trade_logs_tc_{INSTRUMENT}')
TC_SUMMARY      = os.path.join(TC_DATA_DIR, f'trade_summary_tc_{INSTRUMENT}.csv')
_TC_SIGNALS_FNAME = ('TRIPLE_CONFIRM_excursions.csv' if INSTRUMENT == 'nifty'
                     else 'TRIPLE_CONFIRM_sensex_excursions.csv')
TC_SIGNALS_CSV  = os.path.join(REPO_ROOT, 'iris_backtest', 'data', _TC_SIGNALS_FNAME)
BASELINE_SUMMARY = os.path.join(_DIR, 'data', f'trade_summary_{INSTRUMENT}_rerun.csv')

_OPEN_STATUSES = (
    'active', 'adjusted', 'adjusted_additional', 'active_additional',
    'adjusted_elm', 'active_additional_elm', 'adjusted_additional_elm',
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TC signal loading
# ---------------------------------------------------------------------------

def load_tc_signals(path: str) -> dict:
    """
    Load pre-computed TRIPLE_CONFIRM signals from excursions CSV.
    Returns {signal_ts (Timestamp): direction} lookup for O(1) inner-loop access.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"TC signals file not found: {path}\n"
            f"Run: python iris_backtest/research/validate_sensex.py")
    df = pd.read_csv(path, parse_dates=['signal_ts'])
    df['signal_ts'] = pd.to_datetime(df['signal_ts'])
    return dict(zip(df['signal_ts'], df['direction']))


def check_tc(spread: dict, ts: pd.Timestamp, tc_signal_map: dict):
    """
    Check if a TRIPLE_CONFIRM signal threatens this spread at ts.
    Returns 'tc_sl' if TC fires against this spread, None otherwise.
    Mirrors check_sl() from backtest.py — caller handles exit + adjust.
      bullish TC threatens the CE spread (spot rising toward sold CE)
      bearish TC threatens the PE spread (spot falling toward sold PE)
    """
    tc_dir = tc_signal_map.get(ts)
    if tc_dir is None:
        return None
    if tc_dir == 'bullish' and spread['type'] == 'ce':
        return 'tc_sl'
    if tc_dir == 'bearish' and spread['type'] == 'pe':
        return 'tc_sl'
    return None


# ---------------------------------------------------------------------------
# Trade log save (TC-specific path)
# ---------------------------------------------------------------------------

def _save_trade_log_tc(trade_logs: list, expiry_ts: pd.Timestamp, counter: int):
    if not trade_logs or not ENABLE_TRADE_LOGS:
        return
    os.makedirs(TC_LOGS_DIR, exist_ok=True)
    fname = f"trade_{counter:04d}_{pd.Timestamp(expiry_ts).strftime('%Y-%m-%d')}.csv"
    pd.DataFrame(trade_logs).to_csv(os.path.join(TC_LOGS_DIR, fname), index=False)


# ---------------------------------------------------------------------------
# Summary print
# ---------------------------------------------------------------------------

def print_summary_tc(records: list):
    logger.info('=' * 60)
    logger.info('ARTEMIS TC BACKTEST SUMMARY')
    logger.info('=' * 60)
    logger.info(f"  Instrument : {INSTRUMENT.upper()}")

    if not records:
        logger.info("  No contracts found.")
        logger.info('=' * 60)
        return

    df = pd.DataFrame(records)
    traded  = df[df['week_outcome'] == 'traded']
    skipped = df[df['week_outcome'] != 'traded']

    logger.info(f"  Total weeks        : {len(df)}")
    logger.info(f"  Skipped (VIX gate) : {len(skipped)}")
    logger.info(f"  Traded weeks       : {len(traded)}")

    if traded.empty:
        logger.info("  No traded weeks to analyse.")
        logger.info('=' * 60)
        return

    winners  = traded[traded['total_pl_points'] > 0]
    losers   = traded[traded['total_pl_points'] <= 0]
    win_rate = len(winners) / len(traded) * 100

    logger.info(f"  Win rate           : {win_rate:.1f}%")
    if len(winners):
        logger.info(f"  Avg winner (pts)   : {winners['total_pl_points'].mean():.2f}")
    if len(losers):
        logger.info(f"  Avg loser  (pts)   : {losers['total_pl_points'].mean():.2f}")
    logger.info(f"  Total P&L (pts)    : {traded['total_pl_points'].sum():.2f}")
    logger.info(f"  Total P&L (Rs)     : {traded['total_pl_rupees'].sum():,.0f}")
    logger.info(f"  TC-triggered weeks : {int(traded['tc_triggered'].sum())}")
    logger.info("  PE exit breakdown:")
    for reason, count in traded['pe_exit_reason'].value_counts().items():
        logger.info(f"    {reason:25s}: {count}")
    logger.info("  CE exit breakdown:")
    for reason, count in traded['ce_exit_reason'].value_counts().items():
        logger.info(f"    {reason:25s}: {count}")
    logger.info('=' * 60)
    logger.info(f"  Saved to: {TC_SUMMARY}")


# ---------------------------------------------------------------------------
# Baseline vs TC comparison
# ---------------------------------------------------------------------------

def print_comparison():
    if not os.path.exists(BASELINE_SUMMARY):
        logger.info(f"Baseline summary not found at {BASELINE_SUMMARY} — skipping comparison")
        return
    if not os.path.exists(TC_SUMMARY):
        return

    base = pd.read_csv(BASELINE_SUMMARY)
    tc   = pd.read_csv(TC_SUMMARY)

    base_t = base[base['week_outcome'] == 'traded']
    tc_t   = tc[tc['week_outcome'] == 'traded']

    logger.info('=' * 60)
    logger.info('BASELINE vs TC COMPARISON')
    logger.info('=' * 60)
    logger.info(f"  Baseline total P&L (pts) : {base_t['total_pl_points'].sum():.2f}")
    logger.info(f"  TC total P&L (pts)       : {tc_t['total_pl_points'].sum():.2f}")
    delta = tc_t['total_pl_points'].sum() - base_t['total_pl_points'].sum()
    logger.info(f"  Delta (TC − baseline)    : {delta:+.2f}")

    tc_fired = tc_t[tc_t['tc_triggered'] == True]
    if tc_fired.empty:
        logger.info("  No TC-triggered weeks — no per-week comparison to show.")
        logger.info('=' * 60)
        return

    merged = tc_fired.merge(
        base_t[['expiry', 'total_pl_points', 'pe_exit_reason', 'ce_exit_reason']],
        on='expiry', suffixes=('_tc', '_base'), how='left')

    logger.info(f"\n  TC-triggered weeks ({len(tc_fired)}):")
    hdr = (f"  {'Expiry':<14} {'Side':<5} {'Baseline':>10} {'TC':>10} "
           f"{'Delta':>8}  {'Baseline exit (triggered side)'}")
    logger.info(hdr)
    logger.info('  ' + '─' * (len(hdr) - 2))

    for _, row in merged.iterrows():
        side = row.get('tc_side_exited', '?')
        if side == 'ce':
            base_exit = row.get('ce_exit_reason_base', '?')
        else:
            base_exit = row.get('pe_exit_reason_base', '?')
        base_pl = row.get('total_pl_points_base', float('nan'))
        tc_pl   = row.get('total_pl_points_tc',   float('nan'))
        d       = tc_pl - base_pl if pd.notna(tc_pl) and pd.notna(base_pl) else float('nan')
        expiry  = str(row['expiry'])[:10]
        logger.info(
            f"  {expiry:<14} {side:<5} {base_pl:>10.2f} {tc_pl:>10.2f} "
            f"{d:>+8.2f}  {base_exit}")

    tc_wins   = sum(1 for _, r in merged.iterrows()
                    if pd.notna(r.get('total_pl_points_tc'))
                    and pd.notna(r.get('total_pl_points_base'))
                    and r['total_pl_points_tc'] > r['total_pl_points_base'])
    tc_losses = len(merged) - tc_wins
    logger.info(f"\n  TC better than baseline : {tc_wins}/{len(merged)} weeks")
    logger.info(f"  TC worse than baseline  : {tc_losses}/{len(merged)} weeks")
    logger.info('=' * 60)


# ---------------------------------------------------------------------------
# Helpers — TC record augmentation
# ---------------------------------------------------------------------------

def _tc_fields(triggered=False, direction=None, trigger_ts=None, side=None) -> dict:
    return {
        'tc_triggered':   triggered,
        'tc_direction':   direction,
        'tc_trigger_ts':  trigger_ts,
        'tc_side_exited': side,
    }


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------

def run_backtest_tc():
    logger.info('=== Artemis TC Backtest starting ===')
    logger.info(f"  Instrument : {INSTRUMENT.upper()}")
    logger.info(f"  Output     : {TC_SUMMARY}")

    # --- Load TC signals ---
    tc_signal_map = load_tc_signals(TC_SIGNALS_CSV)
    logger.info(f"  TC signals : {len(tc_signal_map)} loaded from pre-computed CSV")

    # --- Load contracts ---
    if not os.path.exists(CONTRACTS_FILE):
        logger.error(f"contracts.csv not found. Run generate_contracts.py first.")
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

    # --- Load index + VIX data ---
    logger.info("Loading index data...")
    index_df = load_index_data(INDEX_FILE)
    vix_df   = load_vix_daily(VIX_INDEX_FILE)
    vix_map  = dict(zip(vix_df['date'], vix_df['vix_open']))
    logger.info(f"  Index rows : {len(index_df):,}")

    os.makedirs(TC_DATA_DIR, exist_ok=True)
    if ENABLE_TRADE_LOGS:
        os.makedirs(TC_LOGS_DIR, exist_ok=True)

    all_records   = []
    trade_counter = 0

    # -----------------------------------------------------------------------
    # Outer loop — one iteration per weekly contract
    # Identical to backtest.py outer loop except output paths and TC state.
    # -----------------------------------------------------------------------
    for _, contract in contracts_df.iterrows():

        expiry_ts    = contract['expiry']
        entry_anchor = contract['entry']
        elm_time     = contract['elm_time']
        cutoff_time  = contract['cutoff_time']

        entry_date = entry_anchor.date()
        logger.info(f"\nWeek: entry {entry_date} | expiry {expiry_ts.date()}")

        # --- VIX gate ---
        entry_vix = vix_map.get(entry_date)
        if entry_vix is None:
            logger.info(f"  VIX data missing for {entry_date} — skipping")
            rec = _build_summary_record(
                contract, None, None, None, 0,
                make_spread('pe'), make_spread('ce'), skipped='skipped_no_vix')
            rec.update(_tc_fields())
            all_records.append(rec)
            continue

        if entry_vix >= VIX_THRESHOLD:
            logger.info(f"  VIX {entry_vix:.1f} >= {VIX_THRESHOLD} — skipping")
            rec = _build_summary_record(
                contract, None, None, entry_vix, 0,
                make_spread('pe'), make_spread('ce'), skipped='skipped_vix')
            rec.update(_tc_fields())
            all_records.append(rec)
            continue

        # --- Entry: spot at 10:30 close, execute at 10:31 open ---
        signal_ts  = entry_anchor
        exec_ts    = signal_ts + pd.Timedelta(minutes=1)
        entry_spot = get_index_price(index_df, signal_ts, col='close')

        if entry_spot is None:
            logger.warning(f"  No index data at {signal_ts} — skipping")
            rec = _build_summary_record(
                contract, None, None, entry_vix, 0,
                make_spread('pe'), make_spread('ce'), skipped='skipped_no_data')
            rec.update(_tc_fields())
            all_records.append(rec)
            continue

        dte      = compute_dte(entry_date, expiry_ts)
        lots     = LOT_COUNT
        pe       = make_spread('pe')
        ce       = make_spread('ce')
        vix_band = get_vix_band(entry_vix)

        # --- Strike selection ---
        pe_sell_strike, _ = select_sell_strike('pe', entry_spot, expiry_ts, signal_ts)
        ce_sell_strike, _ = select_sell_strike('ce', entry_spot, expiry_ts, signal_ts)

        if pe_sell_strike is None or ce_sell_strike is None:
            logger.warning(
                f"  Strike selection failed (PE={pe_sell_strike}, CE={ce_sell_strike}) — skipping")
            rec = _build_summary_record(
                contract, None, None, entry_vix, 0,
                pe, ce, skipped='skipped_no_strikes')
            rec.update(_tc_fields())
            all_records.append(rec)
            continue

        pe['sell_strike'] = pe_sell_strike
        pe['buy_strike']  = pe_sell_strike - HEDGE_POINTS
        ce['sell_strike'] = ce_sell_strike
        ce['buy_strike']  = ce_sell_strike + HEDGE_POINTS

        load_spread_dfs(pe, expiry_ts)
        load_spread_dfs(ce, expiry_ts)

        pe_ok = execute_spread_at(pe, exec_ts, expiry_ts, dte, lots, vix_band)
        ce_ok = execute_spread_at(ce, exec_ts, expiry_ts, dte, lots, vix_band)

        if not pe_ok or not ce_ok:
            logger.warning("  Entry failed — skipping")
            rec = _build_summary_record(
                contract, exec_ts, entry_spot, entry_vix,
                lots, pe, ce, skipped='skipped_entry_failed')
            rec.update(_tc_fields())
            all_records.append(rec)
            continue

        logger.info(
            f"  ENTRY @ {exec_ts} | spot {entry_spot:.0f} | VIX {entry_vix:.1f} | DTE {dte}")
        logger.info(
            f"  PE sell {pe['sell_strike']} @ {pe['sell_entry']:.2f} | "
            f"buy {pe['buy_strike']} @ {pe['buy_entry']:.2f} | "
            f"idx_sl {pe['index_sl']:.0f}")
        logger.info(
            f"  CE sell {ce['sell_strike']} @ {ce['sell_entry']:.2f} | "
            f"buy {ce['buy_strike']} @ {ce['buy_entry']:.2f} | "
            f"idx_sl {ce['index_sl']:.0f}")

        trade_logs         = []
        elm_done           = False
        entry_exec_ts      = exec_ts
        current_loop_date  = None

        # TC state — resets each session
        tc_triggered   = False
        tc_direction   = None
        tc_trigger_ts  = None
        tc_side_exited = None

        # -------------------------------------------------------------------
        # Inner loop — 1-min candles from entry to expiry
        # TC check is inserted before the standard SL checks.
        # All SL handling is byte-for-byte identical to backtest.py.
        # -------------------------------------------------------------------
        week_index = index_df[
            (index_df.index >= exec_ts) &
            (index_df.index <= expiry_ts)
        ]

        for ts, idx_row in week_index.iterrows():
            spot = float(idx_row['close'])

            # Daily DTE refresh (mirrors backtest.py exactly)
            if ts.date() != current_loop_date:
                current_loop_date = ts.date()
                current_dte_daily = compute_dte(ts.date(), expiry_ts)
                if pe['status'] != 'closed' and pe['sell_entry'] is not None:
                    set_sl(pe, current_dte_daily, vix_band)
                if ce['status'] != 'closed' and ce['sell_entry'] is not None:
                    set_sl(ce, current_dte_daily, vix_band)

            if pe['status'] != 'closed':
                update_ltps(pe, ts)
            if ce['status'] != 'closed':
                update_ltps(ce, ts)

            vix_now = vix_map.get(ts.date())

            if ENABLE_TRADE_LOGS:
                trade_logs.append(_make_log_row(ts, spot, vix_now, pe, ce))

            # ---------------------------------------------------------------
            # Expiry check
            # ---------------------------------------------------------------
            if ts >= expiry_ts:
                if pe['status'] != 'closed':
                    exit_spread_at_expiry(pe, expiry_ts, lots, lots // 2)
                    logger.info(f"  PE EXPIRY @ {ts} | pl: {pe['pl']:+.2f}")
                if ce['status'] != 'closed':
                    exit_spread_at_expiry(ce, expiry_ts, lots, lots // 2)
                    logger.info(f"  CE EXPIRY @ {ts} | pl: {ce['pl']:+.2f}")
                break

            # ---------------------------------------------------------------
            # ELM check (Wednesday 15:15 → execute at 15:16 open)
            # ---------------------------------------------------------------
            if not elm_done and ts >= elm_time:
                elm_exec_ts = ts + pd.Timedelta(minutes=1)
                handle_elm(pe, ce, elm_exec_ts, expiry_ts, lots)
                elm_done = True

            # ---------------------------------------------------------------
            # TRIPLE_CONFIRM check — PE spread (mirrors SL check pattern)
            # Fires at most once per session; skipped after ELM.
            # ---------------------------------------------------------------
            if not tc_triggered and not elm_done and pe['status'] != 'closed':
                pe_tc = check_tc(pe, ts, tc_signal_map)
                if pe_tc:
                    tc_exec_ts  = ts + pd.Timedelta(minutes=1)
                    current_dte = compute_dte(ts.date(), expiry_ts)
                    logger.info(
                        f"  PE {pe_tc.upper()} @ {ts} | "
                        f"spot {spot:.0f} | bearish")
                    exit_spread_at(pe, tc_exec_ts, pe_tc, lots, lots // 2)
                    logger.info(f"  PE EXIT | pl: {pe['pl']:+.2f}")
                    if ce['status'] in _OPEN_STATUSES:
                        adjust_spread(ce, spot, tc_exec_ts, expiry_ts,
                                      current_dte, lots, elm_time,
                                      cutoff_time, vix_band)
                    tc_triggered   = True
                    tc_direction   = 'bearish'
                    tc_trigger_ts  = ts
                    tc_side_exited = 'pe'

            # ---------------------------------------------------------------
            # TRIPLE_CONFIRM check — CE spread (mirrors SL check pattern)
            # ---------------------------------------------------------------
            if not tc_triggered and not elm_done and ce['status'] != 'closed':
                ce_tc = check_tc(ce, ts, tc_signal_map)
                if ce_tc:
                    tc_exec_ts  = ts + pd.Timedelta(minutes=1)
                    current_dte = compute_dte(ts.date(), expiry_ts)
                    logger.info(
                        f"  CE {ce_tc.upper()} @ {ts} | "
                        f"spot {spot:.0f} | bullish")
                    exit_spread_at(ce, tc_exec_ts, ce_tc, lots, lots // 2)
                    logger.info(f"  CE EXIT | pl: {ce['pl']:+.2f}")
                    if pe['status'] in _OPEN_STATUSES:
                        adjust_spread(pe, spot, tc_exec_ts, expiry_ts,
                                      current_dte, lots, elm_time,
                                      cutoff_time, vix_band)
                    tc_triggered   = True
                    tc_direction   = 'bullish'
                    tc_trigger_ts  = ts
                    tc_side_exited = 'ce'

            # ---------------------------------------------------------------
            # SL checks — PE spread (unchanged from backtest.py)
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
                    if ce['status'] == 'closed':
                        pass
                    elif ce['status'] in _OPEN_STATUSES:
                        adjust_spread(ce, spot, sl_exec_ts, expiry_ts,
                                      current_dte, lots, elm_time,
                                      cutoff_time, vix_band)

            # ---------------------------------------------------------------
            # SL checks — CE spread (unchanged from backtest.py)
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
                    elif pe['status'] in _OPEN_STATUSES:
                        adjust_spread(pe, spot, sl_exec_ts, expiry_ts,
                                      current_dte, lots, elm_time,
                                      cutoff_time, vix_band)

            if pe['status'] == 'closed' and ce['status'] == 'closed':
                logger.info(f"  Both spreads closed @ {ts} — week ends early")
                break

        # -------------------------------------------------------------------
        # Week complete — save and record
        # -------------------------------------------------------------------
        trade_counter += 1
        _save_trade_log_tc(trade_logs, expiry_ts, trade_counter)

        record = _build_summary_record(
            contract, entry_exec_ts, entry_spot, entry_vix, lots, pe, ce)
        record.update(_tc_fields(tc_triggered, tc_direction, tc_trigger_ts, tc_side_exited))
        all_records.append(record)

        logger.info(
            f"  WEEK DONE | PE pl: {pe['pl']:+.2f} | CE pl: {ce['pl']:+.2f} | "
            f"Total: {record['total_pl_points']:+.4f} pts | "
            f"TC: {tc_triggered}")

    # -----------------------------------------------------------------------
    # Save trade summary, print results, compare vs baseline
    # -----------------------------------------------------------------------
    pd.DataFrame(all_records).to_csv(TC_SUMMARY, index=False)
    print_summary_tc(all_records)
    print_comparison()
    logger.info('=== Artemis TC Backtest complete ===')


if __name__ == '__main__':
    run_backtest_tc()
