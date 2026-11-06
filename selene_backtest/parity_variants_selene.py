"""
Selene - production-parity state machine for a chosen (multiplier, stop), 2026-11-06. parity_backtest_selene.simulate reads configs.DECIDED_MULTIPLIER
(per-contract Supertrend) and configs.DECIDED_SL_PCT (stop) at call time, so a variant is run by setting those two attributes in this process only, then
restoring them; the committed values in selene_configs.py are never edited, and nothing is written to the parity_*.csv files that
tests/test_selene_engine_replay.py and the hestia-backtest ledger read. Outputs: data_sweep/parity_variants/m<mult>_sl<stop>_{legs,trades}.csv.

Usage: python parity_variants_selene.py <end YYYY-MM-DD> <multiplier> <stop_pct>      (stop 1000 = no stop)
The run for the DECIDED pair (2.5, 3.0) must reproduce data_sweep/parity_trades.csv when given the same end date; compare_to_oracle() does that check.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selene_configs as configs
import parity_backtest_selene as pb


def label(mult: float, sl: float) -> str:
    return f'm{mult:.1f}_sl{"none" if sl >= configs.DISABLED_PCT else sl}'


def run_variant(end: str, mult: float, sl: float) -> tuple:
    saved = (configs.DECIDED_MULTIPLIER, configs.DECIDED_SL_PCT)
    configs.DECIDED_MULTIPLIER, configs.DECIDED_SL_PCT = mult, sl
    try:
        legs = pb.simulate(configs.DATA_START, end)
    finally:
        configs.DECIDED_MULTIPLIER, configs.DECIDED_SL_PCT = saved
    return legs, pb.to_trades(legs)


def compare_to_oracle(trades: pd.DataFrame) -> str:
    """Trade-for-trade comparison with the committed-config parity output (entry time, direction, points)."""
    o = pd.read_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_trades.csv'), parse_dates=['entry_ts', 'exit_ts'])
    a = trades.sort_values('entry_ts').reset_index(drop=True)
    b = o.sort_values('entry_ts').reset_index(drop=True)
    if len(a) != len(b):
        return f'DIFFERENT trade counts: variant {len(a)}, oracle {len(b)}'
    same = (a['entry_ts'].values == b['entry_ts'].values).all() and (a['direction'].values == b['direction'].values).all() and \
        abs(a['pnl_pts'].values - b['pnl_pts'].values).max() < 0.01
    return f'{len(a)} trades, identical to the oracle' if same else 'trade lists DIFFER from the oracle'


def main():
    end, mult, sl = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    os.makedirs(configs.PARITY_VARIANTS_DIR, exist_ok=True)
    legs, trades = run_variant(end, mult, sl)
    base = os.path.join(configs.PARITY_VARIANTS_DIR, label(mult, sl))
    legs.to_csv(base + '_legs.csv', index=False)
    trades.to_csv(base + '_trades.csv', index=False)
    print(label(mult, sl), pb.metrics(trades.sort_values('exit_ts').reset_index(drop=True)))
    print('roll events:', legs.attrs['stats'], '| open at end:', legs.attrs['open_at_end'])
    if (mult, sl) == (configs.DECIDED_MULTIPLIER, configs.DECIDED_SL_PCT):
        print('oracle check:', compare_to_oracle(trades))


if __name__ == '__main__':
    main()
