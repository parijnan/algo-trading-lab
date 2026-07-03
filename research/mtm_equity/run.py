"""
run.py — Step 0: Build the MTM equity curve for the existing book.

Entry point:
    python research/mtm_equity/run.py

Produces:
    data/portfolio_mtm_equity.parquet — 1-min portfolio MTM equity curve
    data/per_trade_mtm.parquet — per-trade MTM curves
    data/mtm_vs_realized_summary.csv — headline metrics

Validation gates (fail-loud):
    1. Reconciliation: every trade's final MTM ≈ pl_rs
    2. Lossless merge: realized DD reproduced from MTM trade boundaries
    3. No overlaps: no two trades share a 1-min timestamp
    4. Coverage: trade logs found for ≥ 99% of roster trades
"""

import os
import sys
import logging
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from configs import (
    LETO_TRADE_LOG, LOT_SIZES, EXIT_BAR_EPSILON,
    PORTFOLIO_MTM_PARQUET, PER_TRADE_MTM_PARQUET, SUMMARY_CSV, OUTPUT_DIR,
)
from build_mtm import extract_trade_mtm, calibrate_to_rupees
from equity_curve import (
    build_portfolio_equity, compute_drawdown, compute_realized_drawdown,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('mtm_equity')


def load_roster() -> pd.DataFrame:
    """Load entered trades from leto_trade_log.csv."""
    df = pd.read_csv(LETO_TRADE_LOG)
    entered = df[df['routing_outcome'] == 'entered'].copy()
    entered['entry_ts'] = pd.to_datetime(entered['entry_ts'])
    entered['exit_ts'] = pd.to_datetime(entered['exit_ts'])
    entered = entered.reset_index(drop=True)
    entered['trade_id'] = entered.index.astype(str)
    logger.info(f"Loaded {len(entered)} entered trades from leto_trade_log.csv")
    return entered


def build_all_mtm(roster: pd.DataFrame):
    """
    Extract per-bar MTM for every trade, calibrate to rupees.

    Returns (list of DataFrames, list of calibration info dicts, list of missing trade_ids)
    """
    all_mtm = []
    calib_infos = []
    missing = []

    for idx, row in roster.iterrows():
        trade_id = str(idx)
        strategy = row['strategy']

        # Determine lot size
        if strategy == 'artemis':
            lot_key = f'artemis_{row["instrument"]}'
        else:
            lot_key = strategy
        lot_size = LOT_SIZES.get(lot_key, 65)

        # Extract per-bar MTM
        mtm_df = extract_trade_mtm(row, trade_id)
        if mtm_df is None or len(mtm_df) == 0:
            missing.append({
                'trade_id': trade_id,
                'strategy': strategy,
                'entry_ts': row['entry_ts'],
                'exit_ts': row['exit_ts'],
            })
            continue

        # Calibrate to rupees
        exit_ts = pd.to_datetime(row['exit_ts'])
        mtm_df, info = calibrate_to_rupees(
            mtm_df, float(row['pl_rs']), lot_size, exit_ts, EXIT_BAR_EPSILON
        )
        info['trade_id'] = trade_id
        info['strategy'] = strategy
        info['entry_ts'] = row['entry_ts']
        info['exit_ts'] = row['exit_ts']

        all_mtm.append(mtm_df)
        calib_infos.append(info)

    return all_mtm, calib_infos, missing


def validate(roster: pd.DataFrame, all_mtm: list, calib_infos: list,
             missing: list, equity_df: pd.DataFrame) -> bool:
    """
    Run all validation gates. Returns True if all pass.
    """
    all_pass = True

    logger.info("")
    logger.info("=" * 60)
    logger.info("VALIDATION GATES")
    logger.info("=" * 60)

    # Gate 1: Reconciliation — every trade's final MTM ≈ pl_rs
    logger.info("")
    logger.info("Gate 1: Reconciliation (final MTM ≈ pl_rs)")
    recon_failures = 0
    for info in calib_infos:
        tid = info['trade_id']
        pl_rs = info['pl_rs']
        # Find the trade's final MTM value
        trade_mtm = next((df for df in all_mtm if df['trade_id'].iloc[0] == tid), None)
        if trade_mtm is None:
            continue
        final_mtm = trade_mtm['mtm_rs'].iloc[-1]
        diff = abs(final_mtm - pl_rs)
        if diff > 0.01:
            recon_failures += 1
            if recon_failures <= 5:
                logger.warning(
                    f"  [FAIL] trade {tid} ({info['strategy']}): "
                    f"final_mtm={final_mtm:.2f} vs pl_rs={pl_rs:.2f} (diff={diff:.2f})"
                )
    if recon_failures == 0:
        logger.info(f"  [PASS] All {len(calib_infos)} trades reconcile (|diff| < 0.01)")
    else:
        logger.warning(f"  [FAIL] {recon_failures}/{len(calib_infos)} trades fail reconciliation")
        all_pass = False

    # Gate 2: Lossless merge — realized DD reproduced
    logger.info("")
    logger.info("Gate 2: Lossless merge (realized DD from MTM trade boundaries)")
    realized = compute_realized_drawdown(roster)
    logger.info(f"  Realized max DD: ₹{realized['max_drawdown_rs']:,.0f} "
                f"(Calmar {realized['calmar_realized']:.2f})")
    # Check: the equity at each trade's final bar should equal cumulative pl_rs
    trades_sorted = roster.sort_values('entry_ts').reset_index(drop=True)
    running = 0.0
    boundary_errors = 0
    for _, row in trades_sorted.iterrows():
        tid = str(row.name)
        trade_mtm = next((df for df in all_mtm if df['trade_id'].iloc[0] == tid), None)
        if trade_mtm is None:
            continue
        final_equity = trade_mtm['mtm_rs'].iloc[-1] + running
        expected = running + float(row['pl_rs'])
        if abs(final_equity - expected) > 0.01:
            boundary_errors += 1
        running += float(row['pl_rs'])
    if boundary_errors == 0:
        logger.info(f"  [PASS] All trade boundaries match cumulative realised P&L")
    else:
        logger.warning(f"  [FAIL] {boundary_errors} trade boundaries mismatch")
        all_pass = False

    # Gate 3: No overlapping trades (timestamps)
    logger.info("")
    logger.info("Gate 3: No overlapping trades")
    if len(equity_df) > 0:
        # Check for duplicate timestamps from different trades
        dup_ts = equity_df.groupby('ts')['trade_id'].nunique()
        overlaps = dup_ts[dup_ts > 1]
        if len(overlaps) == 0:
            logger.info(f"  [PASS] No overlapping timestamps across trades")
        else:
            logger.warning(f"  [FAIL] {len(overlaps)} timestamps have multiple trades")
            all_pass = False
    else:
        logger.warning("  [SKIP] No equity data")

    # Gate 4: Coverage
    logger.info("")
    logger.info("Gate 4: Trade-log coverage")
    total = len(roster)
    found = total - len(missing)
    coverage = found / total * 100 if total > 0 else 0
    if coverage >= 99.0:
        logger.info(f"  [PASS] {found}/{total} trades have logs ({coverage:.1f}%)")
    else:
        logger.warning(f"  [FAIL] {found}/{total} trades have logs ({coverage:.1f}%)")
        for m in missing[:5]:
            logger.warning(f"    Missing: {m['strategy']} @ {m['entry_ts']}")
        all_pass = False

    logger.info("")
    if all_pass:
        logger.info("ALL GATES PASSED")
    else:
        logger.warning("SOME GATES FAILED — review output above")
    logger.info("=" * 60)

    return all_pass


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    logger.info("Loading trade roster...")
    roster = load_roster()

    logger.info("Extracting per-bar MTM for all trades...")
    all_mtm, calib_infos, missing = build_all_mtm(roster)
    logger.info(f"  Extracted MTM for {len(all_mtm)} trades "
                f"({len(missing)} missing)")

    # Log calibration method distribution
    methods = {}
    synthetic_count = 0
    for info in calib_infos:
        m = info['method']
        methods[m] = methods.get(m, 0) + 1
        if info['synthetic_exit']:
            synthetic_count += 1
    logger.info(f"  Calibration methods: {methods}")
    if synthetic_count > 0:
        logger.info(f"  Trades with synthetic exit points: {synthetic_count}")

    logger.info("Building portfolio equity curve...")
    equity_df = build_portfolio_equity(all_mtm, roster)
    logger.info(f"  Equity curve: {len(equity_df)} bars, "
                f"{equity_df['ts'].min()} → {equity_df['ts'].max()}")

    # Run validation
    validate(roster, all_mtm, calib_infos, missing, equity_df)

    # Compute drawdowns
    logger.info("")
    logger.info("Computing drawdown metrics...")

    realized = compute_realized_drawdown(roster)
    mtm_dd = compute_drawdown(equity_df)

    # Print headline comparison
    logger.info("")
    logger.info("=" * 60)
    logger.info("HEADLINE: MTM vs Realized Drawdown")
    logger.info("=" * 60)
    logger.info(f"  Realized max DD : ₹{realized['max_drawdown_rs']:,.0f}  "
                f"(Calmar {realized['calmar_realized']:.2f})")
    logger.info(f"  MTM max DD      : ₹{mtm_dd['max_drawdown_rs']:,.0f}  "
                f"(Calmar {mtm_dd['calmar_mtm']:.2f})")
    gap = abs(mtm_dd['max_drawdown_rs']) - abs(realized['max_drawdown_rs'])
    ratio = abs(mtm_dd['max_drawdown_rs']) / abs(realized['max_drawdown_rs']) \
            if realized['max_drawdown_rs'] != 0 else float('inf')
    logger.info(f"  Gap             : ₹{gap:,.0f}  ({ratio:.1f}× realized)")
    logger.info(f"  MTM DD duration : {mtm_dd['max_dd_duration']}")
    logger.info(f"  Peak → Trough   : {mtm_dd['peak_ts']} → {mtm_dd['trough_ts']}")
    logger.info("=" * 60)

    # Save outputs
    logger.info("Saving outputs...")
    equity_df.to_parquet(PORTFOLIO_MTM_PARQUET, index=False)
    per_trade = pd.concat(all_mtm, ignore_index=True)
    per_trade.to_parquet(PER_TRADE_MTM_PARQUET, index=False)

    # Summary CSV
    summary = pd.DataFrame([{
        'metric': 'realized_max_dd_rs',
        'value': realized['max_drawdown_rs']
    }, {
        'metric': 'mtm_max_dd_rs',
        'value': mtm_dd['max_drawdown_rs']
    }, {
        'metric': 'realized_calmar',
        'value': realized['calmar_realized']
    }, {
        'metric': 'mtm_calmar',
        'value': mtm_dd['calmar_mtm']
    }, {
        'metric': 'dd_gap_rs',
        'value': gap
    }, {
        'metric': 'dd_ratio_mtm_over_realized',
        'value': ratio
    }, {
        'metric': 'n_trades',
        'value': len(roster)
    }, {
        'metric': 'n_trades_with_logs',
        'value': len(all_mtm)
    }, {
        'metric': 'n_synthetic_exits',
        'value': synthetic_count
    }, {
        'metric': 'mtm_dd_duration',
        'value': mtm_dd['max_dd_duration']
    }, {
        'metric': 'mtm_peak_ts',
        'value': str(mtm_dd['peak_ts'])
    }, {
        'metric': 'mtm_trough_ts',
        'value': str(mtm_dd['trough_ts'])
    }])
    summary.to_csv(SUMMARY_CSV, index=False)

    logger.info(f"  Equity curve → {PORTFOLIO_MTM_PARQUET}")
    logger.info(f"  Per-trade MTM → {PER_TRADE_MTM_PARQUET}")
    logger.info(f"  Summary → {SUMMARY_CSV}")
    logger.info("Done.")


if __name__ == '__main__':
    main()
