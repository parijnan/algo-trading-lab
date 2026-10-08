"""
Builds the Hestia performance tracker from a read-only snapshot of Delos (pull_snapshot.py): the dataset (JSON) and the artifact page (HTML).

    python hestia_performance/build_tracker.py --snapshot snapshot.json --out-dir /some/dir

Only LIVE trades are counted: each engine's first live trade id comes from tracker_config.json (Prometheus from the 2.5 supertrend multiplier going live,
the others from their first real broker order; paper trades and Prometheus 2.0 trades are left out). P&L is the engines' own recorded figure, gross of
brokerage and charges. "rs_unit" is rupees for ONE unit; "rs_actual" is rs_unit times the units the trade really ran at.

Pure functions below take plain data and return plain data, so the tests need no Delos and no files.
"""

import argparse
import csv
import io
import json
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, 'tracker_config.json')
DEFAULT_TEMPLATE = os.path.join(HERE, 'tracker_template.html')
TOLERANCE_RS = 1.0                     # a trade whose rupees disagree with points x lot value by more than this is reported as a warning


def load_config(path=DEFAULT_CONFIG):
    with open(path) as f:
        return json.load(f)


def _num(text):
    text = (text or '').strip()
    return float(text) if text else None


def _ts(text):
    return datetime.fromisoformat(text.strip()) if text and text.strip() else None


def parse_trades(csv_text, engine, ecfg):
    """Every closed trade in the engine's CSV (live or not), as plain dicts. A trade has one exit (single lot group) or two (Prometheus lot 1 and lot 2)."""
    out = []
    for row in csv.DictReader(io.StringIO(csv_text or '')):
        exits = []
        for lot in (1, 2):
            ts = _ts(row.get(f'lot{lot}_exit_ts'))
            if ts is None:
                continue
            exits.append({'lot': lot, 'ts': ts, 'price': _num(row[f'lot{lot}_exit_price']), 'reason': row[f'lot{lot}_exit_reason'],
                          'pts': _num(row[f'lot{lot}_pnl_points']), 'rs': _num(row[f'lot{lot}_pnl_rs'])})
        if not exits:
            continue
        units = int(row['units'])
        entry_ts = _ts(row['entry_ts'])
        final = max(x['ts'] for x in exits)
        rs_unit = _num(row['total_pnl_rs'])
        out.append({
            'engine': engine, 'trade_id': int(row['trade_id']), 'instrument': ecfg['instrument'], 'expiry': row['contract_expiry'],
            'direction': row['direction'], 'units': units, 'lots': units * ecfg['lots_per_unit'],
            'entry_ts': entry_ts, 'entry_price': _num(row['entry_price']), 'signal_close': _num(row['signal_close']), 'slippage': _num(row['entry_slippage_points']),
            'exits': exits, 'exit_ts': final, 'reasons': '/'.join(dict.fromkeys(x['reason'] for x in exits)),
            'pts': _num(row['total_pnl_points']), 'rs_unit': rs_unit, 'rs_actual': rs_unit * units,
            'hold_h': (final - entry_ts).total_seconds() / 3600.0,
        })
    return out


def split_live(trades, ecfg):
    """(live, excluded): live means trade id at or after the engine's first live trade."""
    first = ecfg['first_live_trade_id']
    return [t for t in trades if t['trade_id'] >= first], [t for t in trades if t['trade_id'] < first]


def check_rupees(t, ecfg):
    """None if the recorded rupees match points x lot value, else a warning string. Guards against a mis-set lot value in tracker_config.json."""
    per_lot = ecfg['rs_per_point_per_lot']
    lots_each = ecfg['lots_per_unit'] / len(t['exits'])
    expected = sum(x['pts'] for x in t['exits']) * per_lot * lots_each
    if abs(expected - t['rs_unit']) > TOLERANCE_RS:
        return f"{t['engine']} #{t['trade_id']}: recorded {t['rs_unit']:.1f} Rs per unit but points x lot value gives {expected:.1f}"
    return None


def stats(trades, key):
    """Summary of closed trades in exit order, on value `key` ('rs_unit' or 'rs_actual'). Drawdown is peak to trough of the cumulative value, starting from 0."""
    trades = sorted(trades, key=lambda t: (t['exit_ts'], t['trade_id']))
    vals = [t[key] for t in trades]
    n = len(vals)
    if n == 0:
        return {'trades': 0, 'wins': 0, 'losses': 0, 'win_rate': None, 'total': 0.0, 'total_pts': 0.0, 'avg_win': None, 'avg_loss': None,
                'profit_factor': None, 'expectancy': None, 'best': None, 'worst': None, 'max_drawdown': 0.0, 'streak': None, 'longest_losing': 0,
                'avg_hold_h': None, 'avg_slippage': None}
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    cum, peak, dd = 0.0, 0.0, 0.0
    for v in vals:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    longest, run = 0, 0
    for v in vals:
        run = run + 1 if v < 0 else 0
        longest = max(longest, run)
    last_sign = 'win' if vals[-1] > 0 else 'loss' if vals[-1] < 0 else 'flat'
    streak = 0
    for v in reversed(vals):
        s = 'win' if v > 0 else 'loss' if v < 0 else 'flat'
        if s != last_sign:
            break
        streak += 1
    gross_loss = abs(sum(losses))
    slips = [t['slippage'] for t in trades if t['slippage'] is not None]
    return {
        'trades': n, 'wins': len(wins), 'losses': len(losses), 'win_rate': len(wins) / n, 'total': sum(vals), 'total_pts': sum(t['pts'] for t in trades),
        'avg_win': sum(wins) / len(wins) if wins else None, 'avg_loss': sum(losses) / len(losses) if losses else None,
        'profit_factor': sum(wins) / gross_loss if gross_loss else None, 'expectancy': sum(vals) / n, 'best': max(vals), 'worst': min(vals),
        'max_drawdown': dd, 'streak': {'kind': last_sign, 'count': streak}, 'longest_losing': longest,
        'avg_hold_h': sum(t['hold_h'] for t in trades) / n, 'avg_slippage': sum(slips) / len(slips) if slips else None,
    }


def series(trades):
    """Per-trade points in exit order with cumulative values, for the equity charts."""
    out, cu, ca = [], 0.0, 0.0
    for t in sorted(trades, key=lambda t: (t['exit_ts'], t['engine'], t['trade_id'])):
        cu += t['rs_unit']
        ca += t['rs_actual']
        out.append({'t': t['exit_ts'].isoformat(), 'engine': t['engine'], 'trade_id': t['trade_id'], 'rs_unit': t['rs_unit'], 'rs_actual': t['rs_actual'],
                    'cum_unit': cu, 'cum_actual': ca})
    return out


def daily(trades):
    """Realised value by the calendar date of the final exit, oldest first."""
    days = {}
    for t in trades:
        d = days.setdefault(t['exit_ts'].date().isoformat(), {'date': t['exit_ts'].date().isoformat(), 'rs_unit': 0.0, 'rs_actual': 0.0, 'trades': 0})
        d['rs_unit'] += t['rs_unit']
        d['rs_actual'] += t['rs_actual']
        d['trades'] += 1
    return [days[k] for k in sorted(days)]


def open_position(engine, ecfg, state, last_price):
    """The engine's open position with unrealised value at the last cached close, or None when flat. Unrealised covers only the lots still open (Prometheus
    lot 1 may already be booked); what lot 1 booked is reported separately."""
    if not state or state.get('status') != 'in_trade':
        return None
    sign = 1 if state['direction'] == 'bullish' else -1
    units = int(state['units'])
    per_lot = ecfg['rs_per_point_per_lot']
    two_lot = 'lot1_lots' in state
    if two_lot:
        open_lots = sum(state[f'lot{i}_lots'] for i in (1, 2) if state.get(f'lot{i}_status') == 'open')
    else:
        open_lots = (state.get('lots') or units * ecfg['lots_per_unit']) / units
    targets = []
    if state.get('lot1_target') is not None:
        targets.append({'label': 'Lot 1 target', 'price': state['lot1_target'], 'hit': state.get('lot1_status') == 'booked'})
    if state.get('lot2_target') is not None:
        targets.append({'label': 'Lot 2 target', 'price': state['lot2_target'], 'hit': state.get('lot2_status') == 'booked'})
    if state.get('target_price') is not None:
        targets.append({'label': 'Target', 'price': state['target_price'], 'hit': False})
    booked = state.get('trade_row', {}) if state.get('lot1_status') == 'booked' else {}
    pos = {
        'engine': engine, 'trade_id': state.get('trade_counter'), 'direction': state['direction'], 'units': units,
        'lots': units * ecfg['lots_per_unit'], 'open_lots_per_unit': open_lots, 'entry_ts': state['entry_ts'], 'entry_price': state['entry_price'],
        'stop': state.get('sl_price'), 'targets': targets, 'last_price': None, 'last_ts': None, 'unreal_pts': None, 'unreal_unit': None, 'unreal_actual': None,
        'booked_pts': booked.get('lot1_pnl_points'), 'booked_unit': booked.get('lot1_pnl_rs'),
    }
    if last_price:
        pts = (last_price['close'] - state['entry_price']) * sign
        pos.update({'last_price': last_price['close'], 'last_ts': last_price['ts'], 'unreal_pts': pts, 'unreal_unit': pts * per_lot * open_lots,
                    'unreal_actual': pts * per_lot * open_lots * units})
    return pos


def build_dataset(snapshot, config):
    engines_cfg = config['engines']
    live_all, trades_out, excluded, warnings, open_pos = [], [], {}, [], []
    per_engine_stats, per_engine_series = {}, {}
    for e, ecfg in engines_cfg.items():
        snap = snapshot['engines'].get(e, {})
        live, out = split_live(parse_trades(snap.get('trades_csv'), e, ecfg), ecfg)
        excluded[e] = len(out)
        warnings += [w for w in (check_rupees(t, ecfg) for t in live) if w]
        live_all += live
        per_engine_stats[e] = {'unit': stats(live, 'rs_unit'), 'actual': stats(live, 'rs_actual')}
        per_engine_series[e] = series(live)
        pos = open_position(e, ecfg, snap.get('state'), snap.get('last_price'))
        if pos:
            open_pos.append(pos)
    for t in sorted(live_all, key=lambda t: t['exit_ts'], reverse=True):
        trades_out.append({
            'engine': t['engine'], 'trade_id': t['trade_id'], 'instrument': t['instrument'], 'direction': t['direction'], 'units': t['units'], 'lots': t['lots'],
            'entry_ts': t['entry_ts'].isoformat(), 'entry_price': t['entry_price'], 'signal_close': t['signal_close'], 'slippage': t['slippage'],
            'exit_ts': t['exit_ts'].isoformat(), 'reasons': t['reasons'], 'hold_h': t['hold_h'], 'pts': t['pts'], 'rs_unit': t['rs_unit'], 'rs_actual': t['rs_actual'],
            'exits': [{'lot': x['lot'], 'ts': x['ts'].isoformat(), 'price': x['price'], 'reason': x['reason'], 'pts': x['pts'], 'rs': x['rs']} for x in t['exits']],
        })
    return {
        'as_of': snapshot['pulled_at'],
        'engines': {e: {k: c[k] for k in ('name', 'instrument', 'lots_per_unit', 'first_live_trade_id', 'live_since', 'note')} for e, c in engines_cfg.items()},
        'trades': trades_out,
        'stats': {**per_engine_stats, 'portfolio': {'unit': stats(live_all, 'rs_unit'), 'actual': stats(live_all, 'rs_actual')}},
        'series': {**per_engine_series, 'portfolio': series(live_all)},
        'daily': daily(live_all),
        'open': open_pos,
        'excluded': excluded,
        'warnings': warnings,
    }


def render_html(dataset, template_text):
    """The template's `__DATA__` token becomes the dataset as JSON ('</' escaped so it can sit inside a script element)."""
    if '__DATA__' not in template_text:
        raise ValueError('template has no __DATA__ placeholder')
    blob = json.dumps(dataset, separators=(',', ':')).replace('</', '<\\/')
    return template_text.replace('__DATA__', blob)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--snapshot', required=True)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--config', default=DEFAULT_CONFIG)
    ap.add_argument('--template', default=DEFAULT_TEMPLATE)
    args = ap.parse_args(argv)
    with open(args.snapshot) as f:
        snapshot = json.load(f)
    dataset = build_dataset(snapshot, load_config(args.config))
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, 'tracker_data.json'), 'w') as f:
        json.dump(dataset, f, indent=1)
    with open(args.template) as f:
        html = render_html(dataset, f.read())
    path = os.path.join(args.out_dir, 'hestia_performance_tracker.html')
    with open(path, 'w') as f:
        f.write(html)
    p = dataset['stats']['portfolio']['actual']
    print(f"as of {dataset['as_of']}: {p['trades']} live trades, realised {p['total']:+,.1f} Rs, {len(dataset['open'])} open, {len(dataset['warnings'])} warnings")
    for w in dataset['warnings']:
        print('WARNING:', w)
    print(path)


if __name__ == '__main__':
    main()
