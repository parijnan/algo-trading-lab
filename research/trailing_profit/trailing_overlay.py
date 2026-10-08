"""
Trailing-profit overlay on the DECIDED exits of Selene (2.5, SL 3.0%), Helios (3.5, SL 1.6%) and Typhon (3.0, SL 0.8%, target 15%).

Rules, in the engine's own long frame (a short is reflected about its entry so one piece of code serves both):
  none        the decided stop (and target) only: must reproduce each module's own simulator, asserted on every trade
  trail t     the stop is max(decided stop, running peak x (1 - t%)), on from entry
  act a, t    the same trail, switched on once the peak has gained a%
  be a        the stop moves up to the entry price once the peak has gained a% (breakeven)
Fills follow the repo convention: a stop fills at its level or at the bar's open on an adverse gap, a target at its level or the open on a favourable gap, the
stop wins a same-minute tie, a session's first minute is exempt, an unresolved trade exits at its trend flip. Intrabar order is unknown at one-minute
resolution, so the peak is updated with the bar's high BEFORE its low is tested against the trail (the pessimistic reading: a bar that makes a new high and
then falls to the new trail is stopped). Results per ONE lot, in percent of entry price (price levels moved a lot over the years).

    python research/trailing_profit/trailing_overlay.py
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trail_configs as cfg  # noqa: E402

DISABLED = 1000.0


def _stops(h: np.ndarray, e: float, sl_level: float, rule: tuple, peak: np.ndarray) -> np.ndarray:
    """The stop in force at each bar, in the long frame, given the bars' highs and their running peak."""
    stop = np.full(len(h), sl_level)
    kind = rule[0]
    if kind == 'trail':
        stop = np.maximum(stop, peak * (1 - rule[1] / 100))
    elif kind in ('act', 'be'):
        reached = h >= e * (1 + rule[1] / 100)
        if reached.any():
            i = int(reached.argmax())
            stop[i:] = np.maximum(stop[i:], peak[i:] * (1 - rule[2] / 100) if kind == 'act' else e)
    return stop


def simulate_trail(trade: dict, p, sl_pct: float, target_pct, rule: tuple):
    """Per-lot result in points (long-frame sign: profit positive) and the exit reason, or None if the trade is unresolved."""
    e, bull = trade['entry'], trade['bull']
    lo, end = trade['lo'], trade['end']
    o, h, l = p.open[lo:end], p.high[lo:end], p.low[lo:end]
    if not bull:                                            # reflect about the entry: a short becomes a long
        o, h, l = 2 * e - o, 2 * e - l, 2 * e - h
    fp = trade['flip_price'] if bull else 2 * e - trade['flip_price']
    active = ~p.guarded[lo:end]
    n = len(o)
    sl_level = e * (1 - sl_pct / 100)
    peak = np.maximum.accumulate(h)                         # includes the bar's own high (pessimistic)
    stop = _stops(h, e, sl_level, rule, peak)
    h_prev = np.concatenate(([-np.inf], h[:-1]))            # the stop as it stood at each bar's OPEN: built from the bars before it only
    stop_open = _stops(h_prev, e, sl_level, rule, np.maximum.accumulate(h_prev))
    hit_stop = (l <= stop) & active
    i_s = int(hit_stop.argmax()) if hit_stop.any() else n
    i_t = n
    tgt = None
    if target_pct is not None and target_pct < DISABLED:
        tgt = e * (1 + target_pct / 100)
        hit_t = (h >= tgt) & active
        i_t = int(hit_t.argmax()) if hit_t.any() else n
    if i_s < n and i_s <= i_t:                              # the stop wins a same-bar tie
        fill = o[i_s] if o[i_s] < stop_open[i_s] else stop[i_s]      # a gap through the stop in force at the open fills at the open; a level the bar's own high created fills at the level
        return fill - e, 'stop'
    if i_t < n:
        return max(o[i_t], tgt) - e, 'target'
    if np.isnan(fp):
        return None
    return fp - e, 'flip'


def run(trades: list, p, sl_pct: float, target_pct, rule: tuple) -> pd.DataFrame:
    rows = []
    for t in trades:
        r = simulate_trail(t, p, sl_pct, target_pct, rule)
        if r is None:
            continue
        rows.append((t['trade_id'], t['entry_ts'], r[0], r[0] / t['entry'] * 100, r[1]))
    return pd.DataFrame(rows, columns=['trade_id', 'entry_ts', 'pts', 'pct', 'reason'])


def metrics(sim: pd.DataFrame) -> dict:
    if sim.empty:
        return {'n': 0, 'total_pct': 0.0, 'dd_pct': 0.0, 'calmar_pct': float('nan'), 'win_pct': float('nan')}
    cum = sim['pct'].cumsum()
    dd = float((cum - cum.cummax().clip(lower=0)).min())
    total = float(sim['pct'].sum())
    return {'n': len(sim), 'total_pct': round(total, 1), 'dd_pct': round(dd, 1), 'calmar_pct': round(total / abs(dd), 2) if dd else float('nan'),
            'win_pct': round(float((sim['pct'] > 0).mean() * 100), 1)}


def rules() -> list:
    out = [('none',)]
    out += [('trail', t) for t in cfg.TRAIL_GRID]
    out += [('act', a, t) for a in cfg.ACT_GRID for t in cfg.ACT_TRAIL_GRID]
    out += [('be', a, None) for a in cfg.ACT_GRID]
    return out


def label(rule: tuple) -> str:
    if rule[0] == 'none':
        return 'decided exits only'
    if rule[0] == 'trail':
        return f'trail {rule[1]}% from entry'
    if rule[0] == 'act':
        return f'trail {rule[2]}% after +{rule[1]}%'
    return f'breakeven after +{rule[1]}%'


def study(name: str, trades: list, p, sl_pct: float, target_pct, reference) -> pd.DataFrame:
    split = pd.Timestamp(cfg.SPLIT_DATE)
    base = run(trades, p, sl_pct, target_pct, ('none',))
    ref = np.array([reference(t) for t in trades if reference(t) is not None])
    assert len(ref) == len(base) and np.allclose(ref, base['pts'].to_numpy(), atol=1e-6), f'{name}: baseline does not reproduce the module simulator'
    rows = []
    for rule in rules():
        sim = base if rule == ('none',) else run(trades, p, sl_pct, target_pct, rule)
        full, pre, post = metrics(sim), metrics(sim[sim['entry_ts'] < split]), metrics(sim[sim['entry_ts'] >= split])
        rows.append({'engine': name, 'rule': label(rule), 'kind': rule[0], 'act': rule[1] if len(rule) > 1 else None, 'trail': rule[2] if len(rule) > 2 else (rule[1] if rule[0] == 'trail' else None),
                     **{f'{k}': v for k, v in full.items()}, **{f'pre_{k}': v for k, v in pre.items() if k in ('total_pct', 'calmar_pct')},
                     **{f'post_{k}': v for k, v in post.items() if k in ('total_pct', 'calmar_pct')}})
    return pd.DataFrame(rows)


def load_all() -> dict:
    import exit_calib_selene as sel
    import exit_calib_helios as hel
    import exit_calib_typhon as typ
    import selene_configs, helios_configs, typhon_configs
    from selene_data_loader import load_futures_1min as sel_load
    from helios_data_loader import load_futures_1min as hel_load
    from typhon_data_loader import back_adjust, compute_roll_gaps, load_futures_1min as typ_load
    out = {}
    p = sel.PriceSeries(sel_load())
    tr = sel.load_trades(selene_configs.DECIDED_MULTIPLIER, p)
    out['Selene'] = (tr, p, selene_configs.DECIDED_SL_PCT, None,
                     lambda t, p=p: (lambda r: None if r is None else r[0])(sel.simulate(t, p, selene_configs.DECIDED_SL_PCT, DISABLED, DISABLED)))
    p = hel.PriceSeries(hel_load())
    tr = hel.load_trades(helios_configs.DECIDED_MULTIPLIER, p)
    out['Helios'] = (tr, p, helios_configs.DECIDED_SL_PCT, None,
                     lambda t, p=p: (lambda r: None if r is None else r[0][0])(hel.simulate(t, p, helios_configs.DECIDED_SL_PCT, [DISABLED])))
    p = typ.PriceSeries(back_adjust(typ_load(), compute_roll_gaps()))
    tr = typ.load_trades(typhon_configs.DECIDED_MULTIPLIER, p)
    out['Typhon'] = (tr, p, typhon_configs.DECIDED_SL_PCT, typhon_configs.DECIDED_TARGET_PCT,
                     lambda t, p=p: (lambda r: None if r is None else r[0][0])(typ.simulate(t, p, typhon_configs.DECIDED_SL_PCT, [typhon_configs.DECIDED_TARGET_PCT])))
    return out


def main():
    os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)
    frames = []
    for name, (trades, p, sl, tgt, ref) in load_all().items():
        print(f'{name}: {len(trades)} trades', flush=True)
        frames.append(study(name, trades, p, sl, tgt, ref))
    res = pd.concat(frames, ignore_index=True)
    res.to_csv(os.path.join(cfg.OUTPUT_DIR, 'trailing_results.csv'), index=False)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    for name, g in res.groupby('engine', sort=False):
        b = g[g['kind'] == 'none'].iloc[0]
        print(f"\n=== {name}: decided exits only: {b['n']} trades, win {b['win_pct']}%, total {b['total_pct']}% , DD {b['dd_pct']}%, Calmar {b['calmar_pct']} | "
              f"before 2026 {b['pre_total_pct']}% / {b['pre_calmar_pct']}, 2026 {b['post_total_pct']}% / {b['post_calmar_pct']}")
        o = g[g['kind'] != 'none']
        both = o[(o['pre_calmar_pct'] > b['pre_calmar_pct']) & (o['post_calmar_pct'] > b['post_calmar_pct']) & (o['total_pct'] >= b['total_pct'])]
        print(f"rules tested {len(o)}; beat the baseline on Calmar % in BOTH halves with total at least equal: {len(both)}; beat it on whole-window Calmar %: {(o['calmar_pct'] > b['calmar_pct']).sum()}")
        cols = ['rule', 'n', 'win_pct', 'total_pct', 'dd_pct', 'calmar_pct', 'pre_total_pct', 'pre_calmar_pct', 'post_total_pct', 'post_calmar_pct']
        print('best 5 by whole-window Calmar %:'); print(o.sort_values('calmar_pct', ascending=False).head(5)[cols].to_string(index=False))
        print('best 3 by total %:'); print(o.sort_values('total_pct', ascending=False).head(3)[cols].to_string(index=False))
        pick = o.loc[o['pre_calmar_pct'].idxmax()]
        print(f"walk-forward: best rule on pre-2026 only = '{pick['rule']}' (pre Calmar {pick['pre_calmar_pct']}); its 2026 result: total {pick['post_total_pct']}% / Calmar {pick['post_calmar_pct']} "
              f"against the baseline's 2026 {b['post_total_pct']}% / {b['post_calmar_pct']}")


if __name__ == '__main__':
    main()
