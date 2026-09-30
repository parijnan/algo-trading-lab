"""
Typhon - Phase 3 exit calibration (plan Step 3): SL% and staged target% grids, comparing two
candidate unit/tranche shapes head to head rather than sweeping tranche count the way Helios did --
NATGASMINI's own lot economics don't suggest a single "1 unit = N lots" choice (typhon_configs.py's
own module docstring on this):

  - SL-only, unit_lots=1 (Selene's own decided shape): a single lot, NO target, trend-flip is the
    only profit exit. Still grid-searches SL, the one free parameter.
  - N=2 tranches, unit_lots=2 (Prometheus's own shape): 2 lots split into 2 tranches, each with its
    own staged, grid-searched target.

Same fill conventions and staged (one-variable-at-a-time) methodology as
Prometheus/Selene/Helios's own exit calibrators (SL grid -> T1 grid -> T2 grid, each stage picking
the best Calmar% with the others pinned; stop wins a same-minute tie against a target; targets fill
at the level or the bar's open on a favourable gap-through, the stop at the level or the bar's open
on an adverse gap-through; the trade's own trend-flip exit is the fallback for any tranche not yet
closed when the flip fires; a session's first 1-min bar is exempt from SL/target checks).

Runs on the BACK-ADJUSTED price series, same as sweep_typhon.py -- entry/exit prices in
trade_summary.csv are already back-adjusted, so PriceSeries must be built from the same series for
the indices to line up.

Rs conversion multiplies by configs.LOT_SIZE (250) -- Helios's own version could skip this because
GOLDPETAL's LOT_SIZE happens to be 1; NATGASMINI's is not (typhon_configs.py's own module
docstring).

Output (data_sweep/, gitignored):
  exit_calib_detail.csv   -- one row per (multiplier, candidate, stage, param value)
  exit_calib_winners.csv  -- one row per (multiplier, candidate): chosen SL/targets and stats
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import typhon_configs as configs
from typhon_data_loader import back_adjust, compute_roll_gaps, load_futures_1min

SWEEP_DIR = configs.DATA_SWEEP_DIR
DETAIL_FILE = os.path.join(SWEEP_DIR, 'exit_calib_detail.csv')
WINNERS_FILE = os.path.join(SWEEP_DIR, 'exit_calib_winners.csv')


class PriceSeries:
    """The back-adjusted 1-minute series as numpy arrays plus a per-bar 'first bar of its session'
    mask (the NO_EXIT_BEFORE_BUFFER_MIN guard)."""

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
    """target_pcts: list of N target percentages, one per tranche, each resolved independently.
    Returns a list of N (pts, reason) tuples in the same order as target_pcts, or None if the trade
    is still unresolved (some tranche has neither hit its target/stop nor has a flip price to fall
    back on)."""
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


def run_variant(trades: list, p: PriceSeries, sl: float, target_pcts: list, n_tranches: int, unit_lots: int) -> pd.DataFrame:
    """Lot weight per tranche = unit_lots / n_tranches. Points -> Rs multiplies by
    configs.LOT_SIZE (250) -- unlike Helios's GOLDPETAL port, NATGASMINI's LOT_SIZE isn't 1."""
    lots_each = unit_lots / n_tranches
    rows = []
    for t in trades:
        r = simulate(t, p, sl, target_pcts)
        if r is None:
            continue
        total_rs = sum(pts * lots_each * configs.LOT_SIZE for pts, _ in r)
        total_pct = sum((pts / t['entry'] * 100) for pts, _ in r) / len(r)   # avg tranche %, for calmar_pct
        reasons = [reason for _, reason in r]
        rows.append((t['trade_id'], t['entry_ts'], total_rs, total_pct, reasons))
    return pd.DataFrame(rows, columns=['trade_id', 'entry_ts', 'pnl_rs', 'pnl_pct', 'reasons'])


def _calmar(series: pd.Series):
    cum = series.cumsum()
    dd = (cum - cum.cummax()).min()
    return round(series.sum() / abs(dd), 2) if dd else float('nan'), dd


def summarize(sim: pd.DataFrame, mult, candidate, stage, param, value) -> dict:
    """calmar_pct (Rs P&L expressed as a %-of-entry-price average across tranches) is PRIMARY --
    it is what picks winners. calmar (raw Rs) is shown alongside as a cross-check, not used to
    pick."""
    n = len(sim)
    calmar, dd = _calmar(sim['pnl_rs'])
    calmar_pct, dd_pct = _calmar(sim['pnl_pct'])
    sl_count = int(sum(1 for reasons in sim['reasons'] for r in reasons if r == 'stop_loss')) if n else 0
    target_hits = int(sum(1 for reasons in sim['reasons'] for r in reasons if r.startswith('target'))) if n else 0
    return {
        'multiplier': mult, 'candidate': candidate, 'stage': stage, 'param': param, 'value': value,
        'n_trades': n, 'win_rate_pct': round(float((sim['pnl_rs'] > 0).mean()) * 100, 1) if n else float('nan'),
        'total_pnl_rs': round(sim['pnl_rs'].sum(), 0), 'max_drawdown_rs': round(dd, 0), 'calmar': calmar,
        'sum_pct_move': round(sim['pnl_pct'].sum(), 2), 'max_dd_pct': round(dd_pct, 2), 'calmar_pct': calmar_pct,
        'sl_leg_count': sl_count, 'target_leg_hits': target_hits,
    }


def _best(rows: list) -> dict:
    return max(rows, key=lambda r: r['calmar_pct'] if pd.notna(r['calmar_pct']) else float('-inf'))


def calibrate_sl_only(mult: float, p: PriceSeries, trades: list) -> tuple:
    """unit_lots=1, N=1 tranche, target pinned DISABLED -- trend-flip is the only profit exit.
    Still grid-searches SL, the one free parameter. Selene's own decided design, modelled this
    way."""
    unit_lots = configs.UNIT_LOTS_SL_ONLY
    detail = [summarize(run_variant(trades, p, sl, [configs.DISABLED_PCT], 1, unit_lots), mult, 'sl_only',
                        'sl_grid', 'sl_pct', sl) for sl in configs.SL_GRID]
    best_sl = _best(detail)['value']
    sim = run_variant(trades, p, best_sl, [configs.DISABLED_PCT], 1, unit_lots)
    winner = summarize(sim, mult, 'sl_only', 'final', 'combo', f'sl{best_sl}_notarget')
    winner['sl_pct'] = best_sl
    winner['target_pcts'] = ()
    winner['unit_lots'] = unit_lots
    return detail, winner


def calibrate_scaleout(mult: float, p: PriceSeries, trades: list) -> tuple:
    """unit_lots=2, N=2 tranches (configs.TRANCHE_COUNTS), staged: SL grid (both targets
    disabled) -> T1 grid (T2 disabled) -> T2 grid (T1 pinned). Prometheus's own shape."""
    n = configs.TRANCHE_COUNTS[0]
    unit_lots = configs.UNIT_LOTS_SCALEOUT
    candidate = f'scaleout_n{n}'
    detail = []
    disabled = [configs.DISABLED_PCT] * n

    stage_sl = [summarize(run_variant(trades, p, sl, disabled, n, unit_lots), mult, candidate, 'sl_grid', 'sl_pct', sl)
                for sl in configs.SL_GRID]
    detail += stage_sl
    best_sl = _best(stage_sl)['value']

    chosen_targets = []
    for k in range(n):
        # DISABLED_PCT is always an explicit candidate, not just a numeric grid value -- lets a
        # stage legitimately conclude "this tranche should ride to the trend-flip, not a target"
        # rather than being forced to pick a real number (same discipline Helios's own
        # calibration needed, plan §4f there).
        candidates = [tv for tv in configs.TARGET_GRID if not chosen_targets or tv > chosen_targets[-1]]
        candidates.append(configs.DISABLED_PCT)
        stage = []
        for tv in candidates:
            targets = chosen_targets + [tv] + [configs.DISABLED_PCT] * (n - k - 1)
            stage.append(summarize(run_variant(trades, p, best_sl, targets, n, unit_lots), mult, candidate,
                                    f'target{k + 1}_grid', f'target{k + 1}_pct', tv))
        detail += stage
        chosen_targets.append(_best(stage)['value'])

    sim = run_variant(trades, p, best_sl, chosen_targets, n, unit_lots)
    winner = summarize(sim, mult, candidate, 'final', 'combo', f"sl{best_sl}_t{'_'.join(str(t) for t in chosen_targets)}")
    winner['sl_pct'] = best_sl
    winner['target_pcts'] = tuple(chosen_targets)
    winner['unit_lots'] = unit_lots
    return detail, winner


def main():
    os.makedirs(SWEEP_DIR, exist_ok=True)
    print('Loading and back-adjusting the 1-min series...')
    df_1m_raw = load_futures_1min()
    gaps = compute_roll_gaps()
    p = PriceSeries(back_adjust(df_1m_raw, gaps))

    all_detail, winners = [], []
    for mult in configs.CALIBRATION_MULTIPLIERS:
        trades = load_trades(mult, p)
        print(f'\n=== multiplier {mult} ({len(trades)} trades) ===')

        if configs.SL_ONLY_CANDIDATE:
            d, w = calibrate_sl_only(mult, p, trades)
            all_detail += d
            winners.append(w)
            print(f"  SL-only (unit_lots={w['unit_lots']}): SL={w['sl_pct']}%  no target  "
                  f"Calmar%={w['calmar_pct']}  Calmar_Rs={w['calmar']}  P&L={w['total_pnl_rs']:,.0f} Rs  n={w['n_trades']}")

        d, w = calibrate_scaleout(mult, p, trades)
        all_detail += d
        winners.append(w)
        targets_str = ','.join(f'{t}%' for t in w['target_pcts'])
        print(f"  {w['candidate']} (unit_lots={w['unit_lots']}): SL={w['sl_pct']}%  targets=[{targets_str}]  "
              f"Calmar%={w['calmar_pct']}  Calmar_Rs={w['calmar']}  P&L={w['total_pnl_rs']:,.0f} Rs  n={w['n_trades']}")

    pd.DataFrame(all_detail).to_csv(DETAIL_FILE, index=False)
    wdf = pd.DataFrame(winners)
    wdf.to_csv(WINNERS_FILE, index=False)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    print('\n' + wdf[['multiplier', 'candidate', 'unit_lots', 'sl_pct', 'target_pcts', 'n_trades', 'win_rate_pct',
                       'total_pnl_rs', 'max_drawdown_rs', 'calmar', 'calmar_pct']].to_string(index=False))


if __name__ == '__main__':
    main()
