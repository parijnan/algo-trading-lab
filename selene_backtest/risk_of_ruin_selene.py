"""
Selene - risk-of-ruin Monte Carlo on the production-parity trades (plan §16).

Method as prometheus_backtest/phase3/risk_of_ruin_p3.py: bootstrap-resample the real trades with replacement, build
2-year paths at the backtest's own trade pace, and call a path RUINED if its max drawdown passes the threshold at any
point AND equity never recovers to that pre-drawdown peak by the end of the horizon.

What differs, on purpose:
* Trades enter as a PERCENTAGE of entry price (pnl_pct), not points. Silver's price rose ~4x over the window, so points
  are not stationary; a percentage carries the same edge at any price level.
* Sizing is the strategy's own: units = capital // (price * lot / DIVISOR * MULTIPLIER), i.e. capital is always deployed at
  a notional of DIVISOR/MULTIPLIER x capital (8/4 = 2x). Each trade therefore multiplies equity by (1 + L * pct/100),
  L = 2 -- the continuous form of the compounding rule (integer-unit rounding ignored). That is the primary scenario
  ("dynamic"). A "fixed-units" variant, additive rather than compounding, is the same L applied to the starting
  capital, as Prometheus's fixed 50-unit run did.
* Leverage sensitivity: L = 1 ... 4 (L = 4 is the margin formula's `x 2` variant, L = 2 is `x 4`).
* Clustering: an i.i.d. bootstrap destroys losing streaks, so a block bootstrap (blocks of RISK_BLOCK_LEN consecutive
  trades) is reported next to it.
* Cost drag: the parity trades are gross of costs; a per-trade cost in bps of price is subtracted in one variant.
Slippage from the participation model (§15) is size-dependent and not stationary, so it is represented here only by
that flat cost drag. Gap risk is whatever the 2,471 real trades contain (the 2026-02-20 gap loss of 14.5% is one of them).
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selene_configs as configs


def paths(returns: np.ndarray, lev: float, n_trades: int, rng, block: int = 1, mode: str = 'dynamic') -> np.ndarray:
    """Equity paths (n_paths x n_trades), starting equity 1.0. returns = per-trade fractional moves of price."""
    n = len(returns)
    if block == 1:
        idx = rng.integers(0, n, size=(configs.RISK_N_PATHS, n_trades))
    else:
        n_blocks = -(-n_trades // block)
        starts = rng.integers(0, n - block + 1, size=(configs.RISK_N_PATHS, n_blocks))
        idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(configs.RISK_N_PATHS, -1)[:, :n_trades]
    r = lev * returns[idx]
    if mode == 'dynamic':
        return np.cumprod(1.0 + r, axis=1)
    return 1.0 + np.cumsum(r, axis=1)


def evaluate(eq: np.ndarray) -> dict:
    peak = np.maximum.accumulate(np.concatenate([np.ones((len(eq), 1)), eq], axis=1), axis=1)[:, 1:]
    dd = eq / peak - 1.0
    min_dd = dd.min(axis=1)
    trough = dd.argmin(axis=1)
    breach = min_dd <= configs.RISK_RUIN_DD_PCT
    ruined = np.zeros(len(eq), dtype=bool)
    for i in np.flatnonzero(breach):
        pre_peak = peak[i, trough[i]]
        ruined[i] = not (eq[i, trough[i]:] >= pre_peak).any()
    return {
        'p_dd_30': (min_dd <= -0.30).mean() * 100, 'p_dd_40': breach.mean() * 100, 'p_dd_50': (min_dd <= -0.50).mean() * 100,
        'p_ruin': ruined.mean() * 100, 'p_below_half': (eq.min(axis=1) < 0.5).mean() * 100,
        'p_negative': (eq.min(axis=1) <= 0).mean() * 100,
        'dd_p50': np.percentile(-min_dd, 50) * 100, 'dd_p90': np.percentile(-min_dd, 90) * 100, 'dd_p99': np.percentile(-min_dd, 99) * 100,
        'median_terminal_x': float(np.median(eq[:, -1])), 'p5_terminal_x': float(np.percentile(eq[:, -1], 5)),
        'p_loss_at_end': (eq[:, -1] < 1.0).mean() * 100,
    }


def main():
    t = pd.read_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_trades.csv'), parse_dates=['entry_ts', 'exit_ts'])
    base = t['pnl_pct'].to_numpy() / 100.0
    days = (t['entry_ts'].max() - t['entry_ts'].min()).days
    per_year = len(t) / days * 365.25
    n_trades = int(round(configs.RISK_HORIZON_YEARS * per_year))
    lev0 = configs.MARGIN_CONTRACT_VALUE_DIVISOR / configs.MARGIN_SIZING_MULTIPLIER
    print(f'{len(t)} trades over {days} days = {per_year:.0f}/yr; {configs.RISK_N_PATHS:,} paths x {n_trades} trades '
          f'({configs.RISK_HORIZON_YEARS} yrs); ruin = drawdown > {abs(configs.RISK_RUIN_DD_PCT):.0%} and unrecovered at the end; '
          f'strategy leverage L = {configs.MARGIN_CONTRACT_VALUE_DIVISOR}/{configs.MARGIN_SIZING_MULTIPLIER} = {lev0:g}x')
    print(f'per-trade % move: mean {base.mean()*100:.3f}%  std {base.std()*100:.3f}%  worst {base.min()*100:.1f}%  best {base.max()*100:.1f}%  '
          f'win rate {(base > 0).mean()*100:.1f}%')
    a = t.sort_values('exit_ts')
    cum = (1 + lev0 * a['pnl_pct'].to_numpy() / 100).cumprod()
    print(f'historical sequence at L={lev0:g}: max drawdown {((cum / np.maximum.accumulate(cum)) - 1).min() * 100:.1f}%\n')

    rows = []
    def run(name, returns, lev, block=1, mode='dynamic'):
        rng = np.random.default_rng(configs.RISK_SEED)
        r = evaluate(paths(returns, lev, n_trades, rng, block, mode))
        r['scenario'] = name
        rows.append(r)

    run(f'primary: iid, dynamic, L={lev0:g}', base, lev0)
    run(f'block {configs.RISK_BLOCK_LEN}, dynamic, L={lev0:g}', base, lev0, block=configs.RISK_BLOCK_LEN)
    run(f'iid, fixed units, L={lev0:g}', base, lev0, mode='fixed')
    for L in configs.RISK_LEVERAGE_GRID:
        if L != lev0:
            run(f'iid, dynamic, L={L:g}', base, L)
    for bps in configs.RISK_COST_DRAG_BPS:
        if bps:
            run(f'iid, dynamic, L={lev0:g}, cost {bps:g} bps/trade', base - bps / 1e4, lev0)
    for L in configs.RISK_LEVERAGE_GRID:
        run(f'block {configs.RISK_BLOCK_LEN}, dynamic, L={L:g}', base, L, block=configs.RISK_BLOCK_LEN) if L != lev0 else None

    d = pd.DataFrame(rows).set_index('scenario').round(2)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    print(d[['p_dd_30', 'p_dd_40', 'p_dd_50', 'p_ruin', 'p_below_half', 'p_loss_at_end', 'dd_p50', 'dd_p90', 'dd_p99',
             'median_terminal_x', 'p5_terminal_x']].to_string())
    d.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'risk_of_ruin.csv'))


if __name__ == '__main__':
    main()
