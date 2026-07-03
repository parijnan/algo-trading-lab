"""
replay.py — Stress-window replays for the MTM equity curve.

Replays specific historical windows (COVID 2020, rate-hike 2022, election vol 2024)
to measure intraday MTM drawdown depth and duration during crisis periods,
vs the realized P&L outcome for trades in those windows.

Run from repo root:
    python research/mtm_equity/replay.py
"""

import os
import sys
import logging
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from configs import PORTFOLIO_MTM_PARQUET, LETO_TRADE_LOG, STRESS_WINDOWS
from equity_curve import compute_drawdown

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('replay')


def replay_window(equity_df: pd.DataFrame, roster: pd.DataFrame,
                  label: str, start: str, end: str):
    """Replay a single stress window."""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    # Filter equity curve to window
    window = equity_df[(equity_df['ts'] >= start_ts) & (equity_df['ts'] <= end_ts)]

    if len(window) == 0:
        logger.info(f"\n{'─' * 60}")
        logger.info(f"Window: {label}")
        logger.info(f"  No active trades in this window.")
        return

    # Window drawdown (relative to equity at window start)
    eq = window['equity'].values
    running_max = np.maximum.accumulate(eq)
    drawdown = eq - running_max

    window_peak_idx = np.argmax(running_max[:len(drawdown)])
    window_trough_idx = np.argmin(drawdown)
    window_dd = drawdown[window_trough_idx]
    window_peak = eq[window_peak_idx]
    window_trough = eq[window_trough_idx]

    # Find trades active in this window
    trades_in_window = roster[
        (pd.to_datetime(roster['entry_ts']) <= end_ts) &
        (pd.to_datetime(roster['exit_ts']) >= start_ts)
    ].copy()

    logger.info(f"\n{'─' * 60}")
    logger.info(f"Window: {label}")
    logger.info(f"  Period: {start} → {end}")
    logger.info(f"  Active bars: {len(window)}")
    logger.info(f"  Trades in window: {len(trades_in_window)}")

    if len(trades_in_window) > 0:
        for _, t in trades_in_window.iterrows():
            pl = float(t['pl_rs'])
            logger.info(
                f"    {t['strategy']:<14} entry={str(pd.to_datetime(t['entry_ts']))[:16]} "
                f"exit={str(pd.to_datetime(t['exit_ts']))[:16]} "
                f"pl=₹{pl:+,.0f} ({t['exit_reason']})"
            )
        realized_pl = trades_in_window['pl_rs'].astype(float).sum()
        logger.info(f"  Realized P&L (window trades): ₹{realized_pl:+,.0f}")
    else:
        realized_pl = 0.0

    logger.info(f"  MTM equity range: ₹{eq.min():,.0f} → ₹{eq.max():,.0f}")
    logger.info(f"  MTM peak in window:  ₹{window_peak:,.0f} @ {window['ts'].iloc[window_peak_idx]}")
    logger.info(f"  MTM trough in window: ₹{window_trough:,.0f} @ {window['ts'].iloc[window_trough_idx]}")
    logger.info(f"  Window max DD: ₹{window_dd:,.0f} ({window_dd/window_peak*100:.1f}% from peak)")

    # Duration from peak to trough
    peak_ts = window['ts'].iloc[window_peak_idx]
    trough_ts = window['ts'].iloc[window_trough_idx]
    duration = pd.to_datetime(trough_ts) - pd.to_datetime(peak_ts)
    logger.info(f"  Peak → Trough duration: {duration}")

    # Check if the realized P&L "hid" a dip
    if len(trades_in_window) > 0:
        equity_at_first_entry = eq[0]
        equity_min = eq.min()
        dip_from_start = equity_min - equity_at_first_entry
        logger.info(f"  Deepest dip from window start: ₹{dip_from_start:,.0f}")
        logger.info(f"  Did realized P&L hide a dip? "
                    f"{'YES' if dip_from_start < realized_pl - abs(realized_pl) * 0.1 else 'NO'}")


def main():
    logger.info("Loading portfolio MTM equity curve...")
    equity_df = pd.read_parquet(PORTFOLIO_MTM_PARQUET)
    equity_df['ts'] = pd.to_datetime(equity_df['ts'])

    logger.info("Loading trade roster...")
    roster = pd.read_csv(LETO_TRADE_LOG)
    roster = roster[roster['routing_outcome'] == 'entered'].copy()
    roster['entry_ts'] = pd.to_datetime(roster['entry_ts'])
    roster['exit_ts'] = pd.to_datetime(roster['exit_ts'])

    logger.info(f"\n{'=' * 60}")
    logger.info("STRESS WINDOW REPLAYS")
    logger.info(f"{'=' * 60}")

    for label, start, end in STRESS_WINDOWS:
        replay_window(equity_df, roster, label, start, end)

    # Also replay the overall max DD window (from the main run)
    logger.info(f"\n{'=' * 60}")
    logger.info("OVERALL MAX DD WINDOW")
    logger.info(f"{'=' * 60}")

    full_dd = compute_drawdown(equity_df)
    logger.info(f"  Peak: ₹{full_dd['peak_equity']:,.0f} @ {full_dd['peak_ts']}")
    logger.info(f"  Trough: ₹{full_dd['trough_equity']:,.0f} @ {full_dd['trough_ts']}")
    logger.info(f"  Max DD: ₹{full_dd['max_drawdown_rs']:,.0f}")
    logger.info(f"  Duration: {full_dd['max_dd_duration']}")

    # Replay the max DD window
    dd_start = full_dd['peak_ts']
    dd_end = full_dd['trough_ts'] + pd.Timedelta(days=1)
    replay_window(equity_df, roster, "Overall max DD window",
                  dd_start.strftime('%Y-%m-%d'), dd_end.strftime('%Y-%m-%d'))

    logger.info(f"\n{'=' * 60}")
    logger.info("Done.")


if __name__ == '__main__':
    main()
