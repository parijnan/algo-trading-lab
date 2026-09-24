"""
Selene - per-trade minute-by-minute logs for the production-parity backtest.

One CSV per trade under data_sweep/parity_trade_logs/, one row per 1-minute bar of the contract actually held,
from entry to exit inclusive. A trade that rolled contracts (fallback roll or forced roll) is one file with a
`leg_no`/`contract` column showing the hand-over. Column meanings:
  unrealised_pts   total P&L of the trade so far, in points per lot: legs already closed (realised) plus the
                   current leg marked to the bar's close
  running_mae/mfe  worst / best total P&L reached so far, using each bar's adverse / favourable extreme
                   (low/high for a long, high/low for a short) -- for a single-leg trade this is the usual
                   excursion from entry; across a roll it carries the realised P&L of the earlier legs
  sl_px            the current leg's stop level
  event            'entry' on the first row of a leg, 'exit:<reason>' on its last
Also adds final_mae, final_mfe and hold_hours to parity_trades.csv (same three columns as the Phase 2 sweep).
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selene_configs as configs
from parity_backtest_selene import load_contracts

LOGS_DIR = os.path.join(configs.DATA_SWEEP_DIR, 'parity_trade_logs')


def write_trade_logs(legs: pd.DataFrame, contracts: dict = None) -> pd.DataFrame:
    """Writes the per-trade CSVs; returns one summary row per trade (trade_id, final_mae, final_mfe, hold_hours)."""
    contracts = contracts or load_contracts(configs.SYMBOL, configs.DATA_START, configs.PARITY_END_EXTENDED)
    os.makedirs(LOGS_DIR, exist_ok=True)
    for f in os.listdir(LOGS_DIR):            # stale files from an earlier run with a different trade sequence
        if f.endswith('.csv'):
            os.remove(os.path.join(LOGS_DIR, f))

    legs = legs.copy()
    for c in ('entry_ts', 'exit_ts'):
        legs[c] = pd.to_datetime(legs[c])
    summaries = []

    for tid, g in legs.sort_values(['trade_id', 'leg_no']).groupby('trade_id'):
        direction = g['direction'].iloc[0]
        s = 1.0 if direction == 'bullish' else -1.0
        realised, run_mae, run_mfe = 0.0, 0.0, 0.0
        frames = []
        for _, leg in g.iterrows():
            c = contracts[pd.Timestamp(leg['contract']).date()]
            lo = c.idx.searchsorted(leg['entry_ts'], side='left')
            hi = c.idx.searchsorted(leg['exit_ts'], side='right')
            if hi <= lo:
                realised += leg['pnl_pts']
                continue
            o, h, l, cl = c.o[lo:hi], c.h[lo:hi], c.l[lo:hi], c.c[lo:hi]
            entry = float(leg['entry_px'])
            worst, best = (l, h) if s > 0 else (h, l)
            adverse = np.minimum(realised + s * (worst - entry), 0.0)       # <= 0
            favour = np.maximum(realised + s * (best - entry), 0.0)         # >= 0
            mae = np.maximum.accumulate(np.maximum(-adverse, run_mae))
            mfe = np.maximum.accumulate(np.maximum(favour, run_mfe))
            run_mae, run_mfe = float(mae[-1]), float(mfe[-1])
            ev = np.full(len(o), '', dtype=object)
            ev[0] = 'entry'
            ev[-1] = f"exit:{leg['exit_reason']}"
            frames.append(pd.DataFrame({
                'ts': c.idx[lo:hi], 'contract': str(leg['contract']), 'leg_no': int(leg['leg_no']), 'direction': direction,
                'mins_since_entry': ((c.idx[lo:hi] - g['entry_ts'].iloc[0]).total_seconds() // 60).astype(int),
                'open': o, 'high': h, 'low': l, 'close': cl,
                'unrealised_pts': np.round(realised + s * (cl - entry), 2),
                'running_mae': np.round(mae, 2), 'running_mfe': np.round(mfe, 2),
                'sl_px': round(float(leg['sl_px']), 2), 'event': ev}))
            realised += leg['pnl_pts']
        if not frames:
            continue
        log = pd.concat(frames, ignore_index=True)
        first = g.iloc[0]
        letter = 'B' if direction == 'bullish' else 'S'
        log.to_csv(os.path.join(LOGS_DIR, f"trade_{int(tid):04d}_{first['entry_ts']:%Y-%m-%d_%H%M}_{letter}.csv"), index=False)
        summaries.append({'trade_id': int(tid), 'final_mae': round(run_mae, 2), 'final_mfe': round(run_mfe, 2),
                          'hold_hours': round((g['exit_ts'].iloc[-1] - first['entry_ts']).total_seconds() / 3600, 2)})
    return pd.DataFrame(summaries)


def main():
    legs = pd.read_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_legs.csv'))
    # contract column was written as a date string; contracts are keyed by date
    legs['contract'] = pd.to_datetime(legs['contract']).dt.date
    summ = write_trade_logs(legs)
    tp = os.path.join(configs.DATA_SWEEP_DIR, 'parity_trades.csv')
    trades = pd.read_csv(tp).drop(columns=['final_mae', 'final_mfe', 'hold_hours'], errors='ignore')
    trades = trades.merge(summ, on='trade_id', how='left')
    trades.to_csv(tp, index=False)
    print(f'{len(summ)} trade logs written to {LOGS_DIR}')


if __name__ == '__main__':
    main()
