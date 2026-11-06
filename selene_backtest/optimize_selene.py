"""
Selene - multi-metric exit optimisation, one lot per unit (2026-11-06). Asked by the owner: look at 3.0 again with the live shape (1 lot a unit,
as 2.5 runs), and do not pick on Calmar alone: drawdown percent, number of drawdowns, smoothness of the equity curve, costs.

Method (parameters in selene_configs.py, section "Multi-metric exit optimisation"):
  * Raw trades of each multiplier in OPT_MULTIPLIERS come from the existing sweep (data_sweep/mult_X.X/trade_summary.csv); the exit overlay is the
    same fill convention as exit_calib_selene.simulate (stop wins a same-minute tie, gap-through fills at the open, a session's first 1-min bar is
    exempt, the trend flip is the fallback). One lot, one stop, one optional target. `simulate_one` also returns the exit TIMESTAMP, which the older
    simulator does not, because the equity curve here is on calendar time. It is asserted equal to exit_single_target_selene.run_one_lot.
  * Costs on every cell, gross is only shown for reference: per-order charges from hestia_simulation/sim_config (brokerage, exchange and SEBI fees,
    GST, stamp, CTT) on the order value (price x 1 kg), plus the measured live slippage of Selene (mean shortfall of live trades against their
    backtest twins, comparison.csv). A stress variant adds OPT_STOP_EXTRA_SLIP_BPS on every stop exit.
  * Equity: fixed units, not compounded (the live engine runs a set number of units). A trade's return = (points - cost) / (entry x OPT_MARGIN_FRACTION
    x OPT_CAPITAL_PER_MARGIN), i.e. on the capital held per unit; equity = 1 + the running sum, on calendar days (closed P&L by exit date). Drawdown percent is
    of peak equity. A compounded all-in curve at the house 2x leverage was tried first and gave a -92% drawdown for the 2.5 baseline, which says more about
    compounding at 2x than about the strategy, so it was dropped. Costs: brokerage is per order, so it is split over OPT_UNITS_FOR_COSTS units.
  * Metrics: CAGR, max drawdown %, Calmar (CAGR / max DD), Ulcer index, number of drawdown episodes deeper than 5% and 10%, longest time underwater,
    share of days underwater, R-squared of equity against time, share of positive months, worst month, monthly Sharpe, profit factor, win rate,
    stop count, worst trade.
  * Composite = mean percentile rank over nine of those metrics within one multiplier's grid. Plateau score = the composite averaged over the
    neighbouring stop values at the same target, so an isolated spike (one good cell between bad ones) scores low.
  * Walk-forward at each OPT_SPLITS date: the cell is chosen by the plateau score computed on entries before the date only, then read from the date on.

Output (data_sweep/, gitignored): optimize_grid.csv (every cell, both multipliers), optimize_walkforward.csv.
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hestia_simulation'))
import selene_configs as configs
import sim_config as sc
from selene_data_loader import load_futures_1min
from exit_calib_selene import PriceSeries, load_trades

OFF = configs.DISABLED_PCT
HIGHER = ['calmar_c', 'r2', 'pos_months_pct', 'profit_factor', 'sharpe_m']
LOWER = ['max_dd_pct', 'ulcer', 'longest_uw_days', 'n_dd_10', 'uw_share_pct']


# ---------------------------------------------------------------- costs
def measured_slippage_rs() -> float:
    """Mean Rs per trade by which live Selene fills fell short of their backtest twins (matched trades with the same exit), floored at 0."""
    c = pd.read_csv(configs.OPT_COMPARISON_CSV)
    c = c[(c['engine'] == 'Selene') & (c['status'] == 'matched') & (c['same_exit'] == True)]   # noqa: E712
    return max(0.0, float((c['bt_rs'] - c['live_rs']).mean())) if len(c) else 0.0


def charges_rs(direction_bull: bool, price: float) -> float:
    """Per-order charges of one one-lot Selene trade: an entry order and an exit order on the order value (price x 1 kg). Same formulas as hestia_simulation."""
    value = price * 1.0
    total = 0.0
    brokerage = sc.BROKERAGE / configs.OPT_UNITS_FOR_COSTS            # one order carries all the units
    for leg in ('entry', 'exit'):
        buy = (leg == 'entry') == direction_bull
        exch, sebi = sc.EXCHANGE_FEE * value, sc.SEBI_FEE * value
        total += brokerage + sc.GST * (brokerage + exch + sebi) + exch + sebi
        total += 0.0 if buy else sc.CTT_SELL * value
        total += sc.STAMP_DUTY_BUY * value if buy else 0.0
    return total


# ---------------------------------------------------------------- the exit overlay, with exit timestamps
def prepare(mult: float, p: PriceSeries) -> list:
    trades = load_trades(mult, p)
    raw = pd.read_csv(os.path.join(configs.DATA_SWEEP_DIR, f'mult_{mult:.1f}', 'trade_summary.csv'), parse_dates=['exit_ts']).set_index('trade_id')
    for t in trades:
        lo, end = t['lo'], t['end']
        t['o'], t['h'], t['l'] = p.open[lo:end], p.high[lo:end], p.low[lo:end]
        t['active'] = ~p.guarded[lo:end]
        t['flip_ts'] = raw.loc[t['trade_id'], 'exit_ts']
        t['bars'] = p.index[lo:end]
    return trades


def simulate_one(t: dict, sl_pct: float, tg_pct: float):
    """One lot, one stop, one optional target. Returns (points, reason, exit_ts) or None while unresolved."""
    e, bull = t['entry'], t['bull']
    s = 1.0 if bull else -1.0
    sl, tg = e - s * e * sl_pct / 100, e + s * e * tg_pct / 100
    o, h, l, act = t['o'], t['h'], t['l'], t['active']
    sl_hit = ((l <= sl) if bull else (h >= sl)) & act
    t_hit = ((h >= tg) if bull else (l <= tg)) & act
    n = len(o)
    i_sl = int(sl_hit.argmax()) if sl_hit.any() else n
    i_t = int(t_hit.argmax()) if t_hit.any() else n
    if i_t < i_sl:
        fill = o[i_t] if (o[i_t] > tg if bull else o[i_t] < tg) else tg
        return s * (fill - e), 'target', t['bars'][i_t]
    if i_sl < n:
        fill = o[i_sl] if (o[i_sl] < sl if bull else o[i_sl] > sl) else sl
        return s * (fill - e), 'stop_loss', t['bars'][i_sl]
    if np.isnan(t['flip_price']):
        return None
    return s * (t['flip_price'] - e), 'trend_flip', t['flip_ts']


def run_cell(trades: list, sl: float, tg: float, slip_rs: float, extra_stop_bps: float = 0.0) -> pd.DataFrame:
    rows = []
    for t in trades:
        r = simulate_one(t, sl, tg)
        if r is None:
            continue
        pts, reason, exit_ts = r
        cost = charges_rs(t['bull'], t['entry']) + slip_rs + (t['entry'] * extra_stop_bps / 1e4 if reason == 'stop_loss' else 0.0)
        rows.append((t['trade_id'], t['entry_ts'], exit_ts, t['entry'], pts, cost, reason))
    return pd.DataFrame(rows, columns=['trade_id', 'entry_ts', 'exit_ts', 'entry', 'pnl_pts', 'cost_rs', 'reason'])


# ---------------------------------------------------------------- metrics
def drawdown_runs(dd: pd.Series) -> list:
    """(depth, length in days) of every contiguous stretch below the running peak."""
    out, depth, length = [], 0.0, 0
    for v in dd.to_numpy():
        if v < 0:
            depth, length = min(depth, v), length + 1
        elif length:
            out.append((depth, length))
            depth, length = 0.0, 0
    if length:
        out.append((depth, length))
    return out


def metrics(sim: pd.DataFrame) -> dict:
    n = len(sim)
    nan = float('nan')
    if n < 5:
        return {k: nan for k in ['n_trades', 'cagr_pct', 'max_dd_pct', 'calmar_c', 'ulcer', 'n_dd_5', 'n_dd_10', 'longest_uw_days', 'uw_share_pct', 'r2',
                                 'pos_months_pct', 'worst_month_pct', 'sharpe_m', 'profit_factor', 'win_pct', 'stops', 'worst_trade_pct', 'net_rs', 'gross_rs']}
    d = sim.sort_values('exit_ts')
    net_pts = d['pnl_pts'] - d['cost_rs']                                    # one lot = 1 kg, so points are rupees
    ret = net_pts / (d['entry'] * configs.OPT_MARGIN_FRACTION * configs.OPT_CAPITAL_PER_MARGIN)
    eq = 1.0 + ret.cumsum()
    s = pd.Series(eq.to_numpy(), index=d['exit_ts'].dt.normalize()).groupby(level=0).last()
    start = s.index.min() - pd.Timedelta(days=1)
    s = pd.concat([pd.Series([1.0], index=[start]), s])
    s = s.reindex(pd.date_range(start, s.index.max(), freq='D')).ffill()
    dd = s / s.cummax() - 1.0
    runs = drawdown_runs(dd)
    days = max(1, (s.index[-1] - s.index[0]).days)
    cagr = (s.iloc[-1] - 1.0) * 365.25 / days                              # simple annual return on the capital held (not compounded)
    maxdd = float(dd.min())
    x = np.arange(len(s), dtype=float)
    r2 = float(np.corrcoef(x, s.to_numpy())[0, 1] ** 2)
    m = s.resample('ME').last()
    mret = m.diff()
    mret.iloc[0] = m.iloc[0] - 1.0                                         # monthly P&L as a fraction of the capital held
    wins, losses = net_pts[net_pts > 0].sum(), -net_pts[net_pts < 0].sum()
    return {
        'n_trades': n, 'cagr_pct': 100 * cagr, 'max_dd_pct': 100 * maxdd, 'calmar_c': cagr / abs(maxdd) if maxdd else nan,
        'ulcer': 100 * float(np.sqrt((dd ** 2).mean())),
        'n_dd_5': sum(1 for dep, _ in runs if dep <= -0.05), 'n_dd_10': sum(1 for dep, _ in runs if dep <= -0.10),
        'longest_uw_days': max([ln for _, ln in runs], default=0), 'uw_share_pct': 100 * float((dd < 0).mean()),
        'r2': r2, 'pos_months_pct': 100 * float((mret > 0).mean()), 'worst_month_pct': 100 * float(mret.min()),
        'sharpe_m': float(mret.mean() / mret.std() * np.sqrt(12)) if mret.std() else nan,
        'profit_factor': float(wins / losses) if losses else nan, 'win_pct': 100 * float((net_pts > 0).mean()),
        'stops': int((d['reason'] == 'stop_loss').sum()), 'worst_trade_pct': 100 * float(ret.min()),
        'net_rs': float(net_pts.sum()), 'gross_rs': float(d['pnl_pts'].sum()),
    }


def composite(df: pd.DataFrame) -> pd.Series:
    """Mean percentile rank over the nine ranking metrics, within this frame (1 = best on all)."""
    ranks = [df[c].rank(pct=True) for c in HIGHER] + [(-df[c]).rank(pct=True) for c in LOWER]
    return pd.concat(ranks, axis=1).mean(axis=1)


def plateau(df: pd.DataFrame, col: str = 'composite') -> pd.Series:
    """The composite averaged over neighbouring stop values at the same target (an isolated spike scores low)."""
    stops = configs.OPT_STOP_GRID
    k = configs.OPT_NEIGHBOUR_STEPS
    out = pd.Series(index=df.index, dtype=float)
    key = {(r.sl_pct, r.target_pct): i for i, r in df.iterrows()}
    for i, r in df.iterrows():
        j = stops.index(r.sl_pct)
        vals = [df.at[key[(stops[q], r.target_pct)], col] for q in range(max(0, j - k), min(len(stops), j + k + 1)) if (stops[q], r.target_pct) in key]
        out[i] = float(np.nanmean(vals))
    return out


# ---------------------------------------------------------------- driver
def grid_for(mult: float, trades: list, slip: float, split=None, side=None, extra_bps=0.0) -> pd.DataFrame:
    rows = []
    for sl in configs.OPT_STOP_GRID:
        for tg in configs.OPT_TARGET_GRID:
            sim = run_cell(trades, sl, tg, slip, extra_bps)
            if split is not None:
                sim = sim[sim['entry_ts'] < split] if side == 'pre' else sim[sim['entry_ts'] >= split]
            rows.append({'multiplier': mult, 'sl_pct': sl, 'target_pct': tg, **metrics(sim)})
    df = pd.DataFrame(rows)
    df['composite'] = composite(df)
    df['plateau'] = plateau(df)
    return df


def main():
    os.makedirs(configs.DATA_SWEEP_DIR, exist_ok=True)
    p = PriceSeries(load_futures_1min())
    slip = measured_slippage_rs()
    print(f'measured live slippage {slip:.1f} Rs per trade; charges on a Rs 2.0 lakh order about {charges_rs(True, 200000.0):.1f} Rs per round trip')
    frames, walk = [], []
    for mult in configs.OPT_MULTIPLIERS:
        print(f'multiplier {mult}...', flush=True)
        trades = prepare(mult, p)
        df = grid_for(mult, trades, slip)
        frames.append(df)
        for split in configs.OPT_SPLITS:
            sp = pd.Timestamp(split)
            pre = grid_for(mult, trades, slip, sp, 'pre')
            for shape, mask in (('stop only', pre['target_pct'] >= OFF), ('stop + target', pre['target_pct'] < OFF)):
                best = pre[mask].sort_values('plateau', ascending=False).iloc[0]
                sim = run_cell(trades, best.sl_pct, best.target_pct, slip)
                post = metrics(sim[sim['entry_ts'] >= sp])
                full = df[(df.sl_pct == best.sl_pct) & (df.target_pct == best.target_pct)].iloc[0]
                walk.append({'multiplier': mult, 'split': split, 'shape': shape, 'sl_pct': best.sl_pct, 'target_pct': best.target_pct,
                             'pre_calmar_c': best.calmar_c, 'pre_max_dd_pct': best.max_dd_pct, 'post_n': post['n_trades'], 'post_cagr_pct': post['cagr_pct'],
                             'post_max_dd_pct': post['max_dd_pct'], 'post_calmar_c': post['calmar_c'], 'post_ulcer': post['ulcer'], 'post_r2': post['r2'],
                             'post_pos_months_pct': post['pos_months_pct'], 'full_calmar_c': full.calmar_c, 'full_max_dd_pct': full.max_dd_pct})
    out = pd.concat(frames, ignore_index=True)
    out.to_csv(configs.OPT_GRID_FILE, index=False)
    w = pd.DataFrame(walk)
    w.to_csv(configs.OPT_WALK_FILE, index=False)
    print('wrote', configs.OPT_GRID_FILE, configs.OPT_WALK_FILE)


def shortlist():
    """The OPT_SHORTLIST cells: metrics over each period, with the stop-slippage stress, and a year-by-year table of net Rs per lot and of drawdown."""
    p = PriceSeries(load_futures_1min())
    slip = measured_slippage_rs()
    cache, rows, years = {}, [], []
    for mult, sl, tg in configs.OPT_SHORTLIST:
        if mult not in cache:
            cache[mult] = prepare(mult, p)
        label = f'{mult} sl {"none" if sl >= OFF else sl} tg {"none" if tg >= OFF else tg}'
        for stress, bps in (('base', 0.0), ('stop+10bps', configs.OPT_STOP_EXTRA_SLIP_BPS)):
            sim = run_cell(cache[mult], sl, tg, slip, bps)
            for name, a, b in configs.OPT_PERIODS:
                part = sim[(sim['entry_ts'] >= pd.Timestamp(a)) & (sim['entry_ts'] < pd.Timestamp(b))]
                rows.append({'cell': label, 'stress': stress, 'period': name, **metrics(part)})
        sim = run_cell(cache[mult], sl, tg, slip)
        for y, part in sim.groupby(sim['entry_ts'].dt.year):
            m = metrics(part)
            years.append({'cell': label, 'year': y, 'n_trades': m['n_trades'], 'net_rs': m['net_rs'], 'gross_rs': m['gross_rs'], 'max_dd_pct': m['max_dd_pct'], 'stops': m['stops']})
    pd.DataFrame(rows).to_csv(configs.OPT_SHORTLIST_FILE, index=False)
    pd.DataFrame(years).to_csv(configs.OPT_YEARS_FILE, index=False)
    print('wrote', configs.OPT_SHORTLIST_FILE, configs.OPT_YEARS_FILE)


if __name__ == '__main__':
    shortlist() if '--shortlist' in sys.argv else main()
