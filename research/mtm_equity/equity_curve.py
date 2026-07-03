"""
equity_curve.py — Build portfolio-level MTM equity curve and compute
drawdown metrics.

The portfolio equity at any timestamp = cumulative realised P&L from all
closed trades + unrealised MTM of the currently open trade.

Since leto_backtest validates no overlapping trades, at most one trade is
active at any timestamp — the merge is a concat with baseline offset.
"""

import pandas as pd
import numpy as np


def build_portfolio_equity(all_trades_mtm: list[pd.DataFrame],
                           roster: pd.DataFrame) -> pd.DataFrame:
    """
    Build the 1-min portfolio MTM equity curve.

    Parameters
    ----------
    all_trades_mtm : list of DataFrames, each with columns:
        ts, trade_id, strategy, mtm_rs
    roster : leto_trade_log entered trades with entry_ts, exit_ts, pl_rs

    Returns
    -------
    DataFrame with columns: ts, trade_id, strategy, equity, mtm_rs
    The equity column is the running portfolio equity (cumulative realised
    + current unrealised MTM).
    """
    if not all_trades_mtm:
        return pd.DataFrame(columns=['ts', 'trade_id', 'strategy', 'equity', 'mtm_rs'])

    # Sort roster by entry_ts to establish trade ordering
    roster = roster.sort_values('entry_ts').reset_index(drop=True)

    # Build a lookup: trade_id → cumulative realised P&L before this trade
    cumulative_before = {}
    running = 0.0
    for _, row in roster.iterrows():
        tid = str(row.name)  # trade_id is the roster row index
        cumulative_before[tid] = running
        running += float(row['pl_rs'])

    # Concat all trade MTM DataFrames
    combined = pd.concat(all_trades_mtm, ignore_index=True)
    combined = combined.sort_values('ts').reset_index(drop=True)

    # Apply baseline offset
    combined['equity'] = combined.apply(
        lambda r: cumulative_before.get(r['trade_id'], 0.0) + r['mtm_rs'],
        axis=1
    )

    return combined[['ts', 'trade_id', 'strategy', 'equity', 'mtm_rs']]


def compute_drawdown(equity_df: pd.DataFrame) -> dict:
    """
    Compute drawdown metrics from the portfolio equity curve.

    Returns dict with:
        max_drawdown_rs   : float — deepest peak-to-trough decline (₹)
        max_drawdown_pct  : float — as % of peak
        max_dd_duration   : str — duration from peak to recovery
        calmar_mtm        : float — total P&L / |max_drawdown|
        peak_equity       : float
        trough_equity     : float
        peak_ts           : datetime
        trough_ts         : datetime
    """
    eq = equity_df['equity'].values
    ts = equity_df['ts'].values

    running_max = np.maximum.accumulate(eq)
    drawdown = eq - running_max

    # Find the max drawdown point
    trough_idx = np.argmin(drawdown)
    max_dd = drawdown[trough_idx]
    trough_eq = eq[trough_idx]
    trough_ts = ts[trough_idx]

    # Find the peak before the trough
    peak_idx = np.argmax(eq[:trough_idx + 1])
    peak_eq = eq[peak_idx]
    peak_ts = ts[peak_idx]

    # Duration from peak to recovery (or to end if not recovered)
    peak_val = eq[peak_idx]
    recovery_mask = eq[trough_idx:] >= peak_val
    if recovery_mask.any():
        recovery_offset = np.argmax(recovery_mask)
        recovery_ts = ts[trough_idx + recovery_offset]
        duration = str(pd.to_datetime(recovery_ts) - pd.to_datetime(peak_ts))
    else:
        recovery_ts = None
        duration = f"{pd.to_datetime(ts[-1]) - pd.to_datetime(peak_ts)} (not recovered)"

    total_pl = eq[-1] - eq[0] if len(eq) > 0 else 0.0
    calmar = total_pl / abs(max_dd) if max_dd != 0 else float('nan')

    max_dd_pct = (max_dd / peak_eq * 100) if peak_eq != 0 else 0.0

    return {
        'max_drawdown_rs': float(max_dd),
        'max_drawdown_pct': float(max_dd_pct),
        'max_dd_duration': duration,
        'calmar_mtm': float(calmar),
        'peak_equity': float(peak_eq),
        'trough_equity': float(trough_eq),
        'peak_ts': pd.to_datetime(peak_ts),
        'trough_ts': pd.to_datetime(trough_ts),
        'total_pl': float(total_pl),
    }


def compute_realized_drawdown(roster: pd.DataFrame) -> dict:
    """
    Compute the realized-P&L drawdown (the baseline metric from leto_backtest).
    This is the cumulative sum of pl_rs per trade, measured at trade boundaries.
    """
    trades = roster.sort_values('entry_ts').reset_index(drop=True)
    cumpl = trades['pl_rs'].astype(float).cumsum()
    running_max = cumpl.cummax()
    drawdown = cumpl - running_max

    max_dd = drawdown.min() if len(drawdown) > 0 else 0.0
    total_pl = trades['pl_rs'].astype(float).sum()
    calmar = total_pl / abs(max_dd) if max_dd != 0 else float('nan')

    trough_idx = drawdown.idxmin() if len(drawdown) > 0 else 0
    peak_idx = cumpl.iloc[:trough_idx + 1].idxmax() if trough_idx > 0 else 0

    return {
        'max_drawdown_rs': float(max_dd),
        'calmar_realized': float(calmar),
        'total_pl': float(total_pl),
        'n_trades': len(trades),
        'peak_ts': trades.iloc[peak_idx]['exit_ts'] if len(trades) > 0 else None,
        'trough_ts': trades.iloc[trough_idx]['exit_ts'] if len(trades) > 0 else None,
    }
