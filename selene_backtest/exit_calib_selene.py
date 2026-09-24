"""
Selene - Phase 3 exit calibration: SL%, lot1-target%, lot2-target% grid
sweep for each shortlisted ST multiplier (selene_configs.CALIBRATION_MULTIPLIERS).

Same staged, one-variable-at-a-time methodology and fill conventions as
prometheus_backtest/phase3/exit_calib_p3.py (SL grid -> T1 grid -> T2 grid, each
stage picking the best Calmar with the others pinned; stop wins a same-minute
tie against a target; targets fill at the level or the bar's open on a
favourable gap-through, the stop at the level or the bar's open on an adverse
gap-through; the trade's own trend-flip exit is the fallback and its final
logged bar is excluded from the intrabar check; a session's first 1-min bar is
exempt from SL/target checks). Differences, none of which change a fill:
  * vectorised per trade with numpy straight off the 1-minute series instead of
    walking per-trade log CSVs bar by bar (the full-history logs would be ~1.2M
    rows per multiplier; equivalence against Prometheus's own loop verified on
    real trades before use);
  * combos where T1 >= T2 are skipped rather than asserting.

Two Calmars are reported. `calmar` is the Prometheus convention (cumulative P&L
in points / max drawdown in points, trades in id order) and picks the winners.
`calmar_pct` is the same ratio with each trade's P&L as a percentage of its
entry price, because the price rose ~4x over the window and the points version
is dominated by 2026 (plan §11) -- shown alongside so a disagreement is visible,
not to pick with. Winners are also broken out before/after
selene_configs.WALKFORWARD_SPLIT_DATE.

Output (data_sweep/, gitignored):
  exit_calib_detail.csv   -- one row per (multiplier, stage, param value)
  exit_calib_winners.csv  -- one row per multiplier: chosen SL/T1/T2 and its stats
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selene_configs as configs
from selene_data_loader import load_futures_1min

SWEEP_DIR = configs.DATA_SWEEP_DIR
DETAIL_FILE = os.path.join(SWEEP_DIR, 'exit_calib_detail.csv')
WINNERS_FILE = os.path.join(SWEEP_DIR, 'exit_calib_winners.csv')


class PriceSeries:
    """The 1-minute series as numpy arrays plus a per-bar 'first bar of its
    session' mask (the NO_EXIT_BEFORE_BUFFER_MIN guard)."""

    def __init__(self, df_1m: pd.DataFrame):
        self.index = df_1m.index
        self.open = df_1m['open'].to_numpy(float)
        self.high = df_1m['high'].to_numpy(float)
        self.low = df_1m['low'].to_numpy(float)
        day = df_1m.index.normalize()
        first_ts = pd.Series(df_1m.index, index=df_1m.index).groupby(day).transform('min')
        self.guarded = (df_1m.index == first_ts.to_numpy())


def load_trades(mult: float, prices: PriceSeries) -> list:
    """One dict per trade with its slice bounds, ready for simulation."""
    t = pd.read_csv(os.path.join(SWEEP_DIR, f'mult_{mult:.1f}', 'trade_summary.csv'),
                    parse_dates=['entry_ts', 'exit_ts'])
    out = []
    for _, r in t.iterrows():
        still_open = pd.isna(r['exit_ts'])
        lo = prices.index.searchsorted(r['entry_ts'], side='left')
        hi = len(prices.index) if still_open else prices.index.searchsorted(r['exit_ts'], side='right')
        if hi <= lo:
            continue
        # final logged bar is where the position already exits at its open -- excluded
        end = hi - 1 if hi - lo > 1 else lo
        out.append({
            'trade_id': int(r['trade_id']), 'entry_ts': r['entry_ts'], 'bull': r['direction'] == 'bullish',
            'entry': float(r['entry_price']), 'lo': lo, 'end': end,
            'flip_price': float(r['exit_price']) if pd.notna(r['exit_price']) else np.nan,
        })
    return out


def simulate(trade: dict, p: PriceSeries, sl_pct: float, t1_pct: float, t2_pct: float):
    """Returns (lot1_pts, lot1_reason, lot2_pts, lot2_reason) or None if the
    trade is still unresolved (no SL/target hit and no flip yet)."""
    e, bull = trade['entry'], trade['bull']
    s = 1.0 if bull else -1.0
    sl, t1, t2 = e - s * e * sl_pct / 100, e + s * e * t1_pct / 100, e + s * e * t2_pct / 100

    lo, end = trade['lo'], trade['end']
    o, h, l = p.open[lo:end], p.high[lo:end], p.low[lo:end]
    active = ~p.guarded[lo:end]
    if bull:
        sl_hit, t1_hit, t2_hit = (l <= sl) & active, (h >= t1) & active, (h >= t2) & active
    else:
        sl_hit, t1_hit, t2_hit = (h >= sl) & active, (l <= t1) & active, (l <= t2) & active

    n = len(o)
    i_sl = int(sl_hit.argmax()) if sl_hit.any() else n
    i_t1 = int(t1_hit.argmax()) if t1_hit.any() else n
    i_t2 = int(t2_hit.argmax()) if t2_hit.any() else n

    def target_fill(level, bar_open):   # favourable gap-through fills at the open
        return bar_open if (bar_open > level if bull else bar_open < level) else level

    def stop_fill(level, bar_open):     # adverse gap-through fills at the open
        return bar_open if (bar_open < level if bull else bar_open > level) else level

    def resolve(i_t, level, tag):
        if i_t < i_sl:                  # stop wins a same-bar tie
            return target_fill(level, o[i_t]), tag
        if i_sl < n:
            return stop_fill(sl, o[i_sl]), 'stop_loss'
        return None, None

    p1, r1 = resolve(i_t1, t1, 'target1')
    p2, r2 = resolve(i_t2, t2, 'target2')
    if p1 is None or p2 is None:
        if np.isnan(trade['flip_price']):
            return None
        if p1 is None:
            p1, r1 = trade['flip_price'], 'trend_flip'
        if p2 is None:
            p2, r2 = trade['flip_price'], 'trend_flip'
    return s * (p1 - e), r1, s * (p2 - e), r2


def run_variant(trades: list, p: PriceSeries, sl, t1, t2) -> pd.DataFrame:
    rows = []
    for t in trades:
        r = simulate(t, p, sl, t1, t2)
        if r is None:
            continue
        pts = r[0] + r[2]
        rows.append((t['trade_id'], t['entry_ts'], pts, pts / t['entry'] * 100, r[1], r[3]))
    return pd.DataFrame(rows, columns=['trade_id', 'entry_ts', 'pnl_pts', 'pnl_pct', 'lot1_reason', 'lot2_reason'])


def _calmar(series: pd.Series):
    cum = series.cumsum()
    dd = (cum - cum.cummax()).min()
    return round(series.sum() / abs(dd), 2) if dd else float('nan'), dd


def summarize(sim: pd.DataFrame, mult, stage, param, value) -> dict:
    n = len(sim)
    calmar, dd = _calmar(sim['pnl_pts'] * configs.LOT_SIZE)
    calmar_pct, dd_pct = _calmar(sim['pnl_pct'])
    return {
        'multiplier': mult, 'stage': stage, 'param': param, 'value': value, 'n_trades': n,
        'win_rate_pct': round(float((sim['pnl_pts'] > 0).mean()) * 100, 1) if n else float('nan'),
        'total_pnl_rs': round(sim['pnl_pts'].sum() * configs.LOT_SIZE, 0),
        'max_drawdown_rs': round(dd, 0), 'calmar': calmar,
        'sum_pct_move': round(sim['pnl_pct'].sum(), 1), 'max_dd_pct': round(dd_pct, 1), 'calmar_pct': calmar_pct,
        'lot1_hit_rate_pct': round(float((sim['lot1_reason'] == 'target1').mean()) * 100, 1) if n else float('nan'),
        'lot2_hit_rate_pct': round(float((sim['lot2_reason'] == 'target2').mean()) * 100, 1) if n else float('nan'),
        'stop_loss_count': int((sim['lot1_reason'] == 'stop_loss').sum()),
    }


def _best(rows: list) -> dict:
    return max(rows, key=lambda r: r['calmar'] if pd.notna(r['calmar']) else float('-inf'))


def calibrate(mult: float, p: PriceSeries) -> tuple:
    trades = load_trades(mult, p)
    detail = []

    stage1 = [summarize(run_variant(trades, p, sl, configs.T1_STARTING_DEFAULT, configs.T2_STARTING_DEFAULT),
                        mult, 'sl_grid', 'sl_pct', sl) for sl in configs.SL_GRID]
    detail += stage1
    best_sl = _best(stage1)['value']

    stage2 = [summarize(run_variant(trades, p, best_sl, t1, configs.T2_STARTING_DEFAULT),
                        mult, 'target1_grid', 'target1_pct', t1)
              for t1 in configs.T1_GRID if t1 < configs.T2_STARTING_DEFAULT]
    detail += stage2
    best_t1 = _best(stage2)['value']

    stage3 = [summarize(run_variant(trades, p, best_sl, best_t1, t2), mult, 'target2_grid', 'target2_pct', t2)
              for t2 in configs.T2_GRID if t2 > best_t1]
    detail += stage3
    best_t2 = _best(stage3)['value']

    sim = run_variant(trades, p, best_sl, best_t1, best_t2)
    winner = summarize(sim, mult, 'final', 'combo', f'sl{best_sl}_t1{best_t1}_t2{best_t2}')
    winner.update({'sl_pct': best_sl, 'target1_pct': best_t1, 'target2_pct': best_t2})

    split = pd.Timestamp(configs.WALKFORWARD_SPLIT_DATE)
    for tag, part in (('pre', sim[sim['entry_ts'] < split]), ('post', sim[sim['entry_ts'] >= split])):
        s = summarize(part, mult, tag, 'combo', '')
        winner.update({f'{tag}_trades': s['n_trades'], f'{tag}_win_pct': s['win_rate_pct'],
                       f'{tag}_pnl_rs': s['total_pnl_rs'], f'{tag}_calmar': s['calmar'],
                       f'{tag}_sum_pct': s['sum_pct_move'], f'{tag}_calmar_pct': s['calmar_pct']})
    return detail, winner


def main():
    os.makedirs(SWEEP_DIR, exist_ok=True)
    print('Loading 1-min series...')
    p = PriceSeries(load_futures_1min())

    all_detail, winners = [], []
    for mult in configs.CALIBRATION_MULTIPLIERS:
        print(f'Calibrating multiplier {mult}...')
        detail, w = calibrate(mult, p)
        all_detail += detail
        winners.append(w)
        print(f"  best: SL={w['sl_pct']}%  T1={w['target1_pct']}%  T2={w['target2_pct']}%  Calmar={w['calmar']}  "
              f"calmar_pct={w['calmar_pct']}  P&L={w['total_pnl_rs']:,.0f} Rs  max DD={w['max_drawdown_rs']:,.0f} Rs")

    pd.DataFrame(all_detail).to_csv(DETAIL_FILE, index=False)
    wdf = pd.DataFrame(winners).drop(columns=['stage', 'param', 'value'])
    wdf.to_csv(WINNERS_FILE, index=False)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    print('\n' + wdf.to_string(index=False))


if __name__ == '__main__':
    main()
