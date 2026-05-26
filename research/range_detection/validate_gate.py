"""
validate_gate.py — §7 validation gate for PA range detection.

Two decisive metrics:
  1. Duration distribution — bar_count P10/P25/P50/P75/P90 for established episodes.
     Close-basis hold rate collapses to this: within a PA episode the key level cannot
     be breached on a close (by construction — that would start a new episode). So
     "holds for h bars post-commitment, close-basis" is identical to bar_count ≥ h+3.

  2. Wick-breach rate — fraction of established episodes where the key level is
     touched intraday (low < range_low for up / high > range_high for down) within
     the first 5 bars after commitment, using the key level frozen at commitment time
     (not the evolving or final bounds). This is the options-relevant metric: a strike
     touch matters even if price closes back inside.

Pre-declared kill thresholds (set BEFORE seeing results):
  Kill (Artemis h=3) : fraction of established directional episodes with bar_count≥6 < 50%
  Kill (Athena  h=5) : fraction of established directional episodes with bar_count≥8 < 30%
  Kill (duration)    : P50 bar_count across established directional episodes < 7
  Kill (wick)        : wick breach rate (any key-level touch in first 5 post-commit bars) > 70%

Parameters used (as concluded in plans/range-detection-research.md §5):
  breakout_confirm = 2   (winner from §5 comparison)
  min_range_bars   = 5   (established = bar_count ≥ 5)
  timeframe        = daily_extended (full history 2019→2026 from 1-min data)

Run:
    python research/range_detection/validate_gate.py
"""

import os
import sys
import numpy as np
import pandas as pd

_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
_OUT_DIR   = os.path.join(_THIS_DIR, 'outputs')
os.makedirs(_OUT_DIR, exist_ok=True)

sys.path.insert(0, _THIS_DIR)

from resample import load_daily_extended                   # noqa: E402
from range_detector_pa import compute_pa_ranges            # noqa: E402

BREAKOUT_CONFIRM = 2
MIN_RANGE_BARS   = 5
START_DATE       = '2019-01-28'   # first available date in 1-min data

# ── Kill thresholds (pre-declared — do not change after seeing results) ───────
KILL_ARTEMIS_HOLD_RATE = 0.50   # h=3 hold rate (bar_count≥6) below this → kill
KILL_ATHENA_HOLD_RATE  = 0.30   # h=5 hold rate (bar_count≥8) below this → kill
KILL_P50_BARCOUNT      = 7      # median bar_count below this → kill
KILL_WICK_BREACH_RATE  = 0.70   # wick breach rate above this → kill


def freeze_key_levels(df: pd.DataFrame, episodes: list,
                      breakout_confirm: int) -> dict:
    """
    For each established directional episode, return the key level frozen at
    commitment time (start_idx + breakout_confirm).

    - up  episode: key level = range_low at commitment (key support)
    - down episode: key level = range_high at commitment (key resistance)

    Uses result['range_low'/'range_high'] at the commitment bar, NOT the final
    episode bounds. This is what a trader would know when they commit to the range.
    """
    key_levels = {}
    for ep in episodes:
        if ep['is_transient'] or ep['direction'] == 'initial':
            continue
        commit_idx = ep['start_idx'] + breakout_confirm
        if commit_idx >= len(df):
            continue

        if ep['direction'] == 'up':
            # key support = range_low at commitment time
            key_levels[ep['episode_id']] = df['range_low_bar'].iloc[commit_idx]
        else:
            # key resistance = range_high at commitment time
            key_levels[ep['episode_id']] = df['range_high_bar'].iloc[commit_idx]

    return key_levels


def run_validation(df_ohlc: pd.DataFrame, episodes: list,
                   result: pd.DataFrame) -> dict:
    """
    df_ohlc : daily OHLC (indexed by date), columns: open, high, low, close
    episodes: list of episode dicts from compute_pa_ranges
    result  : per-bar DataFrame from compute_pa_ranges (range_high, range_low cols)
    Returns a dict of all validation stats.
    """
    # Attach per-bar range bounds to ohlc for easy lookup
    df = df_ohlc.copy()
    df['range_high_bar'] = result['range_high']
    df['range_low_bar']  = result['range_low']

    established_dir = [e for e in episodes
                       if not e['is_transient'] and e['direction'] in ('up', 'down')]
    established_all = [e for e in episodes if not e['is_transient']]

    n_all        = len(episodes)
    n_est_dir    = len(established_dir)
    n_up         = sum(1 for e in established_dir if e['direction'] == 'up')
    n_down       = sum(1 for e in established_dir if e['direction'] == 'down')
    n_initial    = sum(1 for e in established_all if e['direction'] == 'initial')
    n_transient  = sum(1 for e in episodes if e['is_transient'])

    print(f'\n── Episode inventory ──────────────────────────────────────────────────────')
    print(f'  Total episodes     : {n_all}')
    print(f'  Transient          : {n_transient}  ({n_transient/n_all*100:.1f}%)')
    print(f'  Established total  : {len(established_all)}')
    print(f'    ↑ up-biased      : {n_up}')
    print(f'    ↓ down-biased    : {n_down}')
    print(f'    ◆ initial/neutral: {n_initial}')

    # ─── 1. Duration distribution ─────────────────────────────────────────────
    bar_counts    = np.array([e['bar_count'] for e in established_dir])
    bar_counts_up = np.array([e['bar_count'] for e in established_dir
                              if e['direction'] == 'up'])
    bar_counts_dn = np.array([e['bar_count'] for e in established_dir
                              if e['direction'] == 'down'])

    def pct_stats(arr, label):
        if len(arr) == 0:
            return {}
        return {
            'n':   len(arr),
            'p10': int(np.percentile(arr, 10)),
            'p25': int(np.percentile(arr, 25)),
            'p50': int(np.percentile(arr, 50)),
            'p75': int(np.percentile(arr, 75)),
            'p90': int(np.percentile(arr, 90)),
            'max': int(np.max(arr)),
            'mean': round(float(np.mean(arr)), 1),
        }

    dur_all  = pct_stats(bar_counts,    'all directional')
    dur_up   = pct_stats(bar_counts_up, 'up')
    dur_down = pct_stats(bar_counts_dn, 'down')

    print(f'\n── Duration distribution (established directional episodes) ───────────────')
    print(f'  {"":12}  {"n":>5}  {"P10":>4}  {"P25":>4}  {"P50":>4}  {"P75":>4}  '
          f'{"P90":>4}  {"max":>4}  {"mean":>5}')
    for lbl, s in [('all dir', dur_all), ('up-biased', dur_up), ('down-biased', dur_down)]:
        if s:
            print(f'  {lbl:<12}  {s["n"]:>5}  {s["p10"]:>4}  {s["p25"]:>4}  '
                  f'{s["p50"]:>4}  {s["p75"]:>4}  {s["p90"]:>4}  '
                  f'{s["max"]:>4}  {s["mean"]:>5}')

    # ─── 2. Hold rate = duration thresholds ───────────────────────────────────
    # "holds for h bars post-commitment" ≡ bar_count ≥ h + BREAKOUT_CONFIRM + 1
    # (setter + confirm_1 + confirm_2 = 3 bars consumed; h more needed → total ≥ h+3)
    bc = BREAKOUT_CONFIRM

    def hold_rate(arr, h):
        return (arr >= h + bc + 1).mean() if len(arr) > 0 else float('nan')

    print(f'\n── Close-hold rate (= duration gate) ─────────────────────────────────────')
    print(f'  Logic: hold for h bars post-commitment (entry at bar {bc+1})')
    print(f'  ≡ bar_count ≥ h + {bc+1} = h + {bc+1}')
    print(f'\n  {"":12}  {"h=1":>6}  {"h=3 (Artemis)":>14}  {"h=5 (Athena)":>14}  '
          f'{"h=7":>6}  {"h=10":>6}')
    for lbl, arr in [('all dir', bar_counts), ('up-biased', bar_counts_up),
                     ('down-biased', bar_counts_dn)]:
        if len(arr) == 0:
            continue
        r1  = f'{hold_rate(arr,1):.1%}'
        r3  = f'{hold_rate(arr,3):.1%}'
        r5  = f'{hold_rate(arr,5):.1%}'
        r7  = f'{hold_rate(arr,7):.1%}'
        r10 = f'{hold_rate(arr,10):.1%}'
        print(f'  {lbl:<12}  {r1:>6}  {r3:>14}  {r5:>14}  {r7:>6}  {r10:>6}')

    # ─── 3. Wick breach at key level (options-relevant) ───────────────────────
    # Key level frozen at commitment bar (not evolving, not final episode bounds).
    # Measures intraday touches that would threaten a short strike.

    print(f'\n── Wick breach rate (key level frozen at commitment, intraday touch) ──────')

    wick_stats = []  # (direction, episode_id, has_breach_in_5, breach_bars_in_5)
    for ep in established_dir:
        start  = ep['start_idx']
        end    = ep['end_idx']
        commit = start + bc          # the bar on which commitment is known
        entry  = commit + 1          # first bar after commitment is known (act here)

        if entry >= len(df):
            continue

        # Key level at commitment time (frozen — what you'd know then)
        if ep['direction'] == 'up':
            key = df['range_low_bar'].iloc[commit]
        else:
            key = df['range_high_bar'].iloc[commit]

        if pd.isna(key) or key <= 0:
            continue

        # Bars from entry to min(end, entry+4) — first 5 post-commitment bars
        check_end = min(end, entry + 4)
        if entry > check_end:
            continue

        breach_count = 0
        for i in range(entry, check_end + 1):
            if ep['direction'] == 'up':
                # Wick breach: intraday low goes below key support
                if df_ohlc['low'].iloc[i] < key:
                    breach_count += 1
            else:
                # Wick breach: intraday high goes above key resistance
                if df_ohlc['high'].iloc[i] > key:
                    breach_count += 1

        has_any = breach_count > 0
        wick_stats.append({
            'direction'   : ep['direction'],
            'episode_id'  : ep['episode_id'],
            'bar_count'   : ep['bar_count'],
            'has_breach'  : has_any,
            'breach_bars' : breach_count,
        })

    wdf = pd.DataFrame(wick_stats)
    if wdf.empty:
        print('  No eligible episodes for wick analysis.')
    else:
        for dir_filter, lbl in [('all', 'all dir'), ('up', 'up-biased'), ('down', 'down-biased')]:
            sub = wdf if dir_filter == 'all' else wdf[wdf['direction'] == dir_filter]
            if sub.empty:
                continue
            br_any = sub['has_breach'].mean()
            mean_b = sub['breach_bars'].mean()
            print(f'  {lbl:<12}  n={len(sub)}  '
                  f'any_breach_in_5={br_any:.1%}  '
                  f'mean_breach_bars={mean_b:.2f}')

    # ─── 4. Survival function from commitment ─────────────────────────────────
    # At each bar offset after commitment, what fraction of established episodes
    # are still active? Useful for seeing the realistic "range still intact" rate
    # at Artemis and Athena entry/exit horizons.
    bars_post_commit = BREAKOUT_CONFIRM + 1  # first "live" bar offset
    print(f'\n── Survival function (fraction still active at bar h after commit) ────────')
    print(f'  (h=0 = commitment bar; h=1 = first live bar; Artemis exit ≈ h=3; Athena ≈ h=5)')

    max_h = 12
    for dir_filter, lbl in [('up', 'up-biased'), ('down', 'down-biased')]:
        sub = [e for e in established_dir if e['direction'] == dir_filter]
        if not sub:
            continue
        # For each h, fraction with bar_count >= h + bc + 1 (still active at bar h post-commit)
        row = f'  {lbl:<12} '
        header_done = False
        if not header_done:
            h_vals = list(range(max_h + 1))
        survival = [(sum(1 for e in sub if e['bar_count'] >= h + bc + 1) / len(sub))
                    for h in h_vals]
        row += '  '.join(f'h={h}:{s:.0%}' for h, s in zip(h_vals, survival)
                         if h in [0, 1, 2, 3, 4, 5, 6, 7, 10, 12])
        print(row)

    # ─── 5. Pass / kill verdict ───────────────────────────────────────────────
    # Apply pre-declared thresholds
    p50      = dur_all.get('p50', 0) if dur_all else 0
    hr_art   = hold_rate(bar_counts, 3)  # Artemis h=3
    hr_ath   = hold_rate(bar_counts, 5)  # Athena h=5
    wr       = wdf['has_breach'].mean() if not wdf.empty else 0.0

    kill_p50  = p50 < KILL_P50_BARCOUNT
    kill_art  = hr_art < KILL_ARTEMIS_HOLD_RATE
    kill_ath  = hr_ath < KILL_ATHENA_HOLD_RATE
    kill_wick = wr     > KILL_WICK_BREACH_RATE

    print(f'\n══ §7 VERDICT ══════════════════════════════════════════════════════════════')
    print(f'  P50 bar_count = {p50}  (kill threshold < {KILL_P50_BARCOUNT}):  '
          f'{"KILL" if kill_p50 else "PASS"}')
    print(f'  Artemis hold rate (h=3) = {hr_art:.1%}  '
          f'(kill threshold < {KILL_ARTEMIS_HOLD_RATE:.0%}):  '
          f'{"KILL" if kill_art else "PASS"}')
    print(f'  Athena hold rate (h=5)  = {hr_ath:.1%}  '
          f'(kill threshold < {KILL_ATHENA_HOLD_RATE:.0%}):  '
          f'{"KILL" if kill_ath else "PASS"}')
    print(f'  Wick breach rate        = {wr:.1%}  '
          f'(kill threshold > {KILL_WICK_BREACH_RATE:.0%}):  '
          f'{"KILL" if kill_wick else "PASS"}')

    any_kill = kill_p50 or kill_art or kill_ath or kill_wick
    print(f'\n  {"🛑 GATE KILLED — do not proceed to containment use cases." if any_kill else "✓ GATE PASSED — proceed to §8 use cases."}'.replace("🛑 ", "KILL: ").replace("✓ ", "PASS: "))

    return {
        'n_episodes_total'    : n_all,
        'n_established_dir'   : n_est_dir,
        'n_up'                : n_up,
        'n_down'              : n_down,
        'duration_all'        : dur_all,
        'duration_up'         : dur_up,
        'duration_down'       : dur_down,
        'hold_rate_h3'        : round(hr_art, 4),
        'hold_rate_h5'        : round(hr_ath, 4),
        'wick_breach_rate'    : round(wr, 4),
        'verdict_kill_p50'    : kill_p50,
        'verdict_kill_art'    : kill_art,
        'verdict_kill_ath'    : kill_ath,
        'verdict_kill_wick'   : kill_wick,
        'gate_passed'         : not any_kill,
    }


def main():
    print('Loading daily OHLC from 1-min (daily_extended, 2019+) ...')
    df = load_daily_extended()
    print(f'  {len(df)} trading days  '
          f'({df.index[0].date()} → {df.index[-1].date()})')

    # Find start index
    start_idx = df.index.searchsorted(pd.Timestamp(START_DATE))
    print(f'  Start candle: {df.index[start_idx].date()}  (index {start_idx})')

    print(f'\nRunning PA range detection (breakout_confirm={BREAKOUT_CONFIRM}, '
          f'min_range_bars={MIN_RANGE_BARS}) ...')
    result, episodes = compute_pa_ranges(df, start_idx,
                                         min_range_bars=MIN_RANGE_BARS,
                                         breakout_confirm=BREAKOUT_CONFIRM)

    est   = sum(1 for e in episodes if not e['is_transient'])
    trans = len(episodes) - est
    print(f'  {len(episodes)} episodes — {est} established, {trans} transient')

    # Export episodes CSV (same format as range_detector_pa.py)
    from range_detector_pa import export_episodes_csv  # noqa
    csv_path = os.path.join(_OUT_DIR, 'range_episodes_pa_daily_full.csv')
    export_episodes_csv(episodes, csv_path)

    # Run validation
    stats = run_validation(df, episodes, result)

    # Save summary CSV
    import json
    import numpy as np

    class _NumpyEncoder(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, (np.integer,)):
                return int(o)
            if isinstance(o, (np.floating,)):
                return float(o)
            if isinstance(o, (np.bool_,)):
                return bool(o)
            return super().default(o)

    summary_path = os.path.join(_OUT_DIR, 'gate_validation.json')
    with open(summary_path, 'w') as f:
        json.dump(stats, f, indent=2, cls=_NumpyEncoder)
    print(f'\nOutputs:')
    print(f'  {csv_path}')
    print(f'  {summary_path}')


if __name__ == '__main__':
    main()
