"""
Helios - Phase 3 exit calibration: SL% and N staged target% grids, for a
20-lot unit split into N EQUAL tranches (plan §4e). Generalizes Prometheus's/
Selene's fixed-2-lot simulator (exit_calib_p3.py / exit_calib_selene.py) to
an arbitrary tranche count N -- nothing in this repo went past 2 lots before.

Same staged, one-variable-at-a-time methodology and fill conventions as
Prometheus/Selene (SL grid -> T1 grid -> T2 grid -> ... -> TN grid, each stage
picking the best Calmar with the others pinned; stop wins a same-minute tie
against a target; targets fill at the level or the bar's open on a favourable
gap-through, the stop at the level or the bar's open on an adverse gap-through;
the trade's own trend-flip exit is the fallback for any tranche not yet closed
when the flip fires; a session's first 1-min bar is exempt from SL/target
checks).

Candidates compared, per the user's explicit instruction (plan §4e) not to
assume scale-out wins:
  - SL-only: 1 tranche of UNIT_LOTS lots, NO target -- trend-flip is the only
    profit exit (Selene's own decided design, modelled here as N=1 with its
    target pinned at DISABLED_PCT, same trick Selene's exit_structures_selene.py
    used for its own "raw" row).
  - N = 1, 2, 3, 4: UNIT_LOTS split into N equal tranches, each with its own
    staged, grid-searched target.
All compared by Calmar (percent-of-entry-price, primary -- see summarize()) on
the same trade set per multiplier.

Output (data_sweep/, gitignored):
  exit_calib_detail.csv   -- one row per (multiplier, tranche_count, stage, param value)
  exit_calib_winners.csv  -- one row per (multiplier, tranche_count): chosen SL/targets and stats
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helios_configs as configs
from helios_data_loader import load_futures_1min

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
        end = hi - 1 if hi - lo > 1 else lo   # final logged bar excluded -- exits at its open
        out.append({
            'trade_id': int(r['trade_id']), 'entry_ts': r['entry_ts'], 'bull': r['direction'] == 'bullish',
            'entry': float(r['entry_price']), 'lo': lo, 'end': end,
            'flip_price': float(r['exit_price']) if pd.notna(r['exit_price']) else np.nan,
        })
    return out


def simulate(trade: dict, p: PriceSeries, sl_pct: float, target_pcts: list):
    """target_pcts: list of N target percentages (ascending, distinct tranches -- not required to
    be sorted by the caller, each is resolved independently). Returns a list of N (pts, reason)
    tuples in the same order as target_pcts, or None if the trade is still unresolved (some
    tranche has neither hit its target/stop nor has a flip price to fall back on)."""
    e, bull = trade['entry'], trade['bull']
    s = 1.0 if bull else -1.0
    sl = e - s * e * sl_pct / 100
    targets = [e + s * e * tp / 100 for tp in target_pcts]

    lo, end = trade['lo'], trade['end']
    o, h, l = p.open[lo:end], p.high[lo:end], p.low[lo:end]
    active = ~p.guarded[lo:end]
    if bull:
        sl_hit = (l <= sl) & active
        t_hits = [(h >= t) & active for t in targets]
    else:
        sl_hit = (h >= sl) & active
        t_hits = [(l <= t) & active for t in targets]

    n = len(o)
    i_sl = int(sl_hit.argmax()) if sl_hit.any() else n
    i_ts = [int(th.argmax()) if th.any() else n for th in t_hits]

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

    results = [resolve(i_ts[k], targets[k], f'target{k + 1}') for k in range(len(targets))]
    if any(pt is None for pt, _ in results):
        if np.isnan(trade['flip_price']):
            return None
        results = [(trade['flip_price'], 'trend_flip') if pt is None else (pt, r) for pt, r in results]
    return [(s * (pt - e), r) for pt, r in results]


def run_variant(trades: list, p: PriceSeries, sl: float, target_pcts: list, n_tranches: int) -> pd.DataFrame:
    """n_tranches may exceed len(target_pcts) when some later tranches are pinned DISABLED and
    collapsed into a single effective target (not used here -- every call passes exactly
    n_tranches target_pcts, one per tranche). Lot weight per tranche = configs.UNIT_LOTS / n_tranches."""
    lots_each = configs.UNIT_LOTS / n_tranches
    rows = []
    for t in trades:
        r = simulate(t, p, sl, target_pcts)
        if r is None:
            continue
        total_pts_weighted = sum(pts * lots_each for pts, _ in r)   # Rs, since LOT_SIZE=1 for GOLDPETAL
        total_pct = sum((pts / t['entry'] * 100) for pts, _ in r) / len(r)   # avg tranche %, for calmar_pct
        reasons = [reason for _, reason in r]
        rows.append((t['trade_id'], t['entry_ts'], total_pts_weighted, total_pct, reasons))
    return pd.DataFrame(rows, columns=['trade_id', 'entry_ts', 'pnl_rs', 'pnl_pct', 'reasons'])


def _calmar(series: pd.Series):
    cum = series.cumsum()
    dd = (cum - cum.cummax()).min()
    return round(series.sum() / abs(dd), 2) if dd else float('nan'), dd


def summarize(sim: pd.DataFrame, mult, n_tranches, stage, param, value) -> dict:
    """calmar_pct (Rs P&L expressed as a %-of-entry-price average across tranches, same
    convention Selene's own plan settled on given multi-year price drift) is PRIMARY -- it is
    what picks winners. calmar (raw Rs) is shown alongside as a cross-check, not used to pick."""
    n = len(sim)
    calmar, dd = _calmar(sim['pnl_rs'])
    calmar_pct, dd_pct = _calmar(sim['pnl_pct'])
    sl_count = int(sum(1 for reasons in sim['reasons'] for r in reasons if r == 'stop_loss')) if n else 0
    target_hits = int(sum(1 for reasons in sim['reasons'] for r in reasons if r.startswith('target'))) if n else 0
    return {
        'multiplier': mult, 'tranche_count': n_tranches, 'stage': stage, 'param': param, 'value': value,
        'n_trades': n, 'win_rate_pct': round(float((sim['pnl_rs'] > 0).mean()) * 100, 1) if n else float('nan'),
        'total_pnl_rs': round(sim['pnl_rs'].sum(), 0), 'max_drawdown_rs': round(dd, 0), 'calmar': calmar,
        'sum_pct_move': round(sim['pnl_pct'].sum(), 2), 'max_dd_pct': round(dd_pct, 2), 'calmar_pct': calmar_pct,
        'sl_leg_count': sl_count, 'target_leg_hits': target_hits,
    }


def _best(rows: list) -> dict:
    return max(rows, key=lambda r: r['calmar_pct'] if pd.notna(r['calmar_pct']) else float('-inf'))


def calibrate_sl_only(mult: float, p: PriceSeries, trades: list) -> tuple:
    """N=1 tranche, target pinned DISABLED -- trend-flip is the only profit exit. Still grid-
    searches SL (the one free parameter). Selene's own decided design, modelled this way."""
    detail = [summarize(run_variant(trades, p, sl, [configs.DISABLED_PCT], 1), mult, 'sl_only', 'sl_grid', 'sl_pct', sl)
              for sl in configs.SL_GRID]
    best_sl = _best(detail)['value']
    sim = run_variant(trades, p, best_sl, [configs.DISABLED_PCT], 1)
    winner = summarize(sim, mult, 'sl_only', 'final', 'combo', f'sl{best_sl}_notarget')
    winner['sl_pct'] = best_sl
    winner['target_pcts'] = ()
    return detail, winner


def calibrate_n_tranche(mult: float, p: PriceSeries, trades: list, n: int) -> tuple:
    """Staged: SL grid (all N targets disabled) -> T1 grid (T2..TN disabled) -> T2 grid
    (T1 pinned, T3..TN disabled) -> ... -> TN grid (T1..T(N-1) pinned)."""
    detail = []
    disabled = [configs.DISABLED_PCT] * n

    stage_sl = [summarize(run_variant(trades, p, sl, disabled, n), mult, n, 'sl_grid', 'sl_pct', sl)
                for sl in configs.SL_GRID]
    detail += stage_sl
    best_sl = _best(stage_sl)['value']

    chosen_targets = []
    for k in range(n):
        # DISABLED_PCT is always an explicit candidate, not just a numeric grid value -- lets a
        # stage legitimately conclude "this tranche should ride to the trend-flip, not a target"
        # instead of being forced to pick a real (and possibly practically-unreachable, at the
        # widened grid's upper end) number. Found necessary 2026-09-29: without this, later
        # stages at wide multipliers either crashed (grid exhausted above the previous winner) or
        # picked erratic near-arbitrary values among functionally-tied "basically never reached"
        # candidates -- both symptoms of forcing a real target where none was warranted.
        candidates = [tv for tv in configs.TARGET_GRID if not chosen_targets or tv > chosen_targets[-1]]
        candidates.append(configs.DISABLED_PCT)
        stage = []
        for tv in candidates:
            targets = chosen_targets + [tv] + [configs.DISABLED_PCT] * (n - k - 1)
            stage.append(summarize(run_variant(trades, p, best_sl, targets, n), mult, n,
                                    f'target{k + 1}_grid', f'target{k + 1}_pct', tv))
        detail += stage
        chosen_targets.append(_best(stage)['value'])

    sim = run_variant(trades, p, best_sl, chosen_targets, n)
    winner = summarize(sim, mult, n, 'final', 'combo', f"sl{best_sl}_t{'_'.join(str(t) for t in chosen_targets)}")
    winner['sl_pct'] = best_sl
    winner['target_pcts'] = tuple(chosen_targets)
    return detail, winner


def main():
    os.makedirs(SWEEP_DIR, exist_ok=True)
    print('Loading 1-min series...')
    p = PriceSeries(load_futures_1min())

    all_detail, winners = [], []
    for mult in configs.CALIBRATION_MULTIPLIERS:
        trades = load_trades(mult, p)
        print(f'\n=== multiplier {mult} ({len(trades)} trades) ===')

        if configs.SL_ONLY_CANDIDATE:
            d, w = calibrate_sl_only(mult, p, trades)
            all_detail += d
            winners.append(w)
            print(f"  SL-only: SL={w['sl_pct']}%  no target  Calmar%={w['calmar_pct']}  "
                  f"Calmar_Rs={w['calmar']}  P&L={w['total_pnl_rs']:,.0f} Rs  n={w['n_trades']}")

        for n in configs.TRANCHE_COUNTS:
            d, w = calibrate_n_tranche(mult, p, trades, n)
            all_detail += d
            winners.append(w)
            targets_str = ','.join(f'{t}%' for t in w['target_pcts'])
            print(f"  N={n}: SL={w['sl_pct']}%  targets=[{targets_str}]  Calmar%={w['calmar_pct']}  "
                  f"Calmar_Rs={w['calmar']}  P&L={w['total_pnl_rs']:,.0f} Rs  n={w['n_trades']}")

    pd.DataFrame(all_detail).to_csv(DETAIL_FILE, index=False)
    wdf = pd.DataFrame(winners)
    wdf.to_csv(WINNERS_FILE, index=False)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    print('\n' + wdf[['multiplier', 'tranche_count', 'sl_pct', 'target_pcts', 'n_trades', 'win_rate_pct',
                       'total_pnl_rs', 'max_drawdown_rs', 'calmar', 'calmar_pct']].to_string(index=False))


if __name__ == '__main__':
    main()
