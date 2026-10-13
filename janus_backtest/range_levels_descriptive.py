"""
Descriptive pass for the range-based Camarilla levels (plans/janus-range-levels.md, step 3). No P&L: how different are the levels, per instrument.

For every traded session it compares the range_setter levels with the previous-session (control) levels: the share of sessions whose High/Low differ, how much wider
R becomes, how old the range is, how often the previous session was a setter, the stop distance (R3 to R4, 0.275 R) as a share of the previous close under each
source, and how much of it rests on back-month history (setter before the contract's first traded session), with the levels rebuilt from traded sessions only.

    python janus_backtest/range_levels_descriptive.py
"""

import os

import numpy as np
import pandas as pd

import janus_configs as configs
import janus_data as data
from janus_levels import camarilla_levels, range_states


def _q(a, qs=(25, 50, 75, 90, 100)):
    a = np.asarray(a, float)
    return [round(float(np.percentile(a, q)), 3) for q in qs] if len(a) else []


def analyse(symbol: str) -> dict:
    sess = data.load_sessions(symbol, level_source='range_setter')
    first_traded = {}
    for s in sess:
        first_traded.setdefault(s['expiry'], s['date'])
    with_rng = [s for s in sess if s['range'] is not None]
    fallback = len(sess) - len(with_rng)
    differ = [s for s in with_rng if (s['range']['high'], s['range']['low']) != (s['prev']['high'], s['prev']['low'])]
    ratio = [s['levels']['R'] / s['levels_prev']['R'] for s in differ]
    age = [s['range']['age'] for s in with_rng]
    setter_prev = sum(1 for s in with_rng if s['range']['age'] == 1)
    stop_prev = [0.275 * s['levels_prev']['R'] / s['prev']['close'] * 100 for s in with_rng]
    stop_rng = [0.275 * s['levels']['R'] / s['prev']['close'] * 100 for s in with_rng]
    # sanity: whenever the previous session is itself the setter, the range levels equal the control levels
    for s in with_rng:
        if s['range']['age'] == 1:
            assert s['levels'] == s['levels_prev'], f"{symbol} {s['date']}: setter day levels differ from control"
    back = [s for s in with_rng if s['range']['setter_date'] < first_traded[s['expiry']]]
    # rebuild each contract's range from its TRADED sessions only and compare the levels it would give
    by_exp = {}
    for s in sess:
        by_exp.setdefault(s['expiry'], []).append(s)
    changed, shifts = 0, []
    for e, ss in by_exp.items():
        daily = [{'date': s['date'], 'high': float(s['bars']['high'].max()), 'low': float(s['bars']['low'].min()), 'close': float(s['bars']['close'].iloc[-1])} for s in ss]
        states = {r['date']: r for r in range_states(daily)}
        for k, s in enumerate(ss):
            if s['range'] is None:
                continue
            prev_state = states.get(s['prev']['date'])
            if prev_state is None:                     # first traded session: its previous session is a back-month day, so no traded-only range exists yet
                wo = s['levels_prev']
            else:
                wo = camarilla_levels(prev_state['high'], prev_state['low'], s['prev']['close'])
            if (round(wo['R'], 9)) != round(s['levels']['R'], 9):
                changed += 1
                shifts.append(abs(wo['R'] / s['levels']['R'] - 1) * 100)
    return {'symbol': symbol, 'contracts': len(first_traded), 'sessions': len(sess), 'fallback_to_control': fallback, 'range_sessions': len(with_rng),
            'differ': len(differ), 'differ_pct': round(len(differ) / max(1, len(with_rng)) * 100, 1), 'R_ratio_q25_50_75_90_max': _q(ratio),
            'range_age_q25_50_75_90_max': _q(age), 'prev_is_setter_pct': round(setter_prev / max(1, len(with_rng)) * 100, 1),
            'stop_pct_control_q25_50_75': _q(stop_prev, (25, 50, 75)), 'stop_pct_range_q25_50_75': _q(stop_rng, (25, 50, 75)),
            'rests_on_back_month': len(back), 'rests_on_back_month_pct': round(len(back) / max(1, len(with_rng)) * 100, 1),
            'levels_change_without_back_month': changed, 'median_R_shift_pct_without_back_month': round(float(np.median(shifts)), 2) if shifts else 0.0}


def main():
    os.makedirs(configs.OUTPUT_DIR, exist_ok=True)
    rows = [analyse(sym) for sym in configs.SYMBOLS]
    lines = []
    for r in rows:
        lines.append(f"== {r['symbol']}: {r['contracts']} contracts, {r['sessions']} traded sessions ({r['fallback_to_control']} fell back to the control levels: previous session not in the range history)")
        lines.append(f"  levels differ from the control on {r['differ']} sessions ({r['differ_pct']}%); the previous session was itself a setter on {r['prev_is_setter_pct']}%")
        lines.append(f"  R(range)/R(control) on the sessions that differ, q25/50/75/90/max: {r['R_ratio_q25_50_75_90_max']}")
        lines.append(f"  range age in sessions (setter = 1), q25/50/75/90/max: {r['range_age_q25_50_75_90_max']}")
        lines.append(f"  stop distance (0.275 R) as % of the previous close, q25/50/75: control {r['stop_pct_control_q25_50_75']}, range {r['stop_pct_range_q25_50_75']}")
        lines.append(f"  range rests on back-month history (setter before the contract's first traded session): {r['rests_on_back_month']} sessions ({r['rests_on_back_month_pct']}%); "
                     f"levels change on {r['levels_change_without_back_month']} sessions if the range is rebuilt from traded sessions only (median R shift {r['median_R_shift_pct_without_back_month']}%)")
    text = '\n'.join(lines)
    print(text)
    with open(os.path.join(configs.OUTPUT_DIR, 'range_levels_descriptive.txt'), 'w') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
