"""
Prometheus - Phase 3 — per-trade 1-minute path CSVs (MFE/MAE analysis).

One row per 1-minute bar from entry to exit, tracking running maximum
favourable/adverse excursion and unrealised P&L at every point along the
trade's life -- this is the actual analytical deliverable of Phase 3: see
how far raw, unmanaged trades run against and in favour of the position
before the signal itself reverses, to inform SL/target choices in a later
phase rather than guessing them first.

A trade still open when the backtest's data ends (no exit_ts, the raw
signal hasn't reversed yet) is NOT skipped -- it's walked from entry all
the way to the last available 1-min bar instead of to a defined exit. This
matters beyond MFE/MAE: exit_calib_p3.py/bespoke_2lot_p3.py reuse these
same per-trade log files to check whether a managed SL/target would have
already closed the trade, which doesn't require the raw signal to have
reversed at all -- skipping the file here silently dropped that trade from
every downstream bespoke/calibration output too, even when its SL/target
outcome was fully determinable from already-available price data (caught
2026-09-11: CRUDEOILM's then-trailing trade had already hit both target1
and target2 hours before data end, yet was absent from every published
bespoke stat). hold_hours for one of these rows reflects time held as of
the last available bar, not a final duration -- it will grow on the next
refresh if the trade is still open then, or lock in once it actually closes.
"""

import os

import pandas as pd


def save_trade_paths_p3(trades: pd.DataFrame, df_1m: pd.DataFrame, logs_dir: str) -> list:
    """
    Returns a list of per-trade summary dicts (trade_id, final_mae,
    final_mfe, hold_hours) built from the same walk that writes each
    per-trade CSV -- callers building an aggregate/summary view should use
    this return value rather than re-parsing the written files.
    """
    os.makedirs(logs_dir, exist_ok=True)
    summaries = []

    for _, t in trades.iterrows():
        entry_ts, exit_ts = t['entry_ts'], t.get('exit_ts')
        still_open = pd.isna(exit_ts)
        path = df_1m.loc[entry_ts:] if still_open else df_1m.loc[entry_ts:exit_ts]
        if path.empty:
            continue

        direction   = t['direction']
        entry_price = float(t['entry_price'])
        running_mae = 0.0
        running_mfe = 0.0
        rows = []

        for ts, bar in path.iterrows():
            if direction == 'bullish':
                adverse   = max(entry_price - bar['low'], 0.0)
                favorable = max(bar['high'] - entry_price, 0.0)
                unrealised_pts = bar['close'] - entry_price
            else:
                adverse   = max(bar['high'] - entry_price, 0.0)
                favorable = max(entry_price - bar['low'], 0.0)
                unrealised_pts = entry_price - bar['close']
            running_mae = max(running_mae, adverse)
            running_mfe = max(running_mfe, favorable)

            rows.append({
                'ts':                ts,
                'mins_since_entry':  int((ts - entry_ts).total_seconds() // 60),
                'open':              bar['open'], 'high': bar['high'],
                'low':               bar['low'],  'close': bar['close'],
                'unrealised_pts':    round(unrealised_pts, 2),
                'running_mae':       round(running_mae, 2),
                'running_mfe':       round(running_mfe, 2),
            })

        letter = 'B' if direction == 'bullish' else 'S'
        filename = f"trade_{int(t['trade_id']):04d}_{pd.Timestamp(entry_ts):%Y-%m-%d_%H%M}_{letter}.csv"
        log_df = pd.DataFrame(rows)
        log_df.insert(1, 'direction', direction)
        log_df.to_csv(os.path.join(logs_dir, filename), index=False)

        end_ts = path.index[-1] if still_open else exit_ts
        hold_hours = (pd.Timestamp(end_ts) - pd.Timestamp(entry_ts)).total_seconds() / 3600
        summaries.append({
            'trade_id': int(t['trade_id']), 'final_mae': round(running_mae, 2),
            'final_mfe': round(running_mfe, 2), 'hold_hours': round(hold_hours, 2),
        })

    return summaries
