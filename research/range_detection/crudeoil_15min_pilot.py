"""
crudeoil_15min_pilot.py — exploratory pilot: does the PA range-detection
method (built/validated on daily Nifty, plans/range-detection-research.md)
say anything useful on CRUDEOILM 15-min bars?

Research only. Does not touch prometheus_production/ or the shared
research/range_detection/*.py modules (imports compute_pa_ranges directly
rather than modifying it) — CLAUDE.md constraint: this directory is a
research module only, not imported by production code, and any production
use needs a dedicated backtest first.

Data: CRUDEOILM front-month-stitched 1-min series (prometheus_backtest's
own data_loader.load_futures_1min — same weekend-drop, opening-bar-fix,
front-month de-dup already relied on by the live signal), resampled to
15-min via the same day-anchored resample_ohlcv used by backtest_p3.py.
Cross-referenced against the live signal's own historical flips
(prometheus_backtest/phase3/data_sweep/mult_2.0/trade_summary.csv,
signal_ts/direction columns — 389 raw ST_15 trend flips over the same
window) to test complementarity, not just run PA in isolation.

Usage: python research/range_detection/crudeoil_15min_pilot.py
"""
import os
import sys

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, 'research', 'range_detection'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'prometheus_backtest'))

from range_detector_pa import compute_pa_ranges          # noqa: E402
import data_loader as dl                                  # noqa: E402

# SYMBOL switch (advisor: CRUDEOIL cross-validation is "one line" once the
# script is parametrized) -- 'CRUDEOILM' (default, primary) or 'CRUDEOIL'
# (full-size contract, cross-validation). ST_15's own reference trade
# summary lives in a parallel phase3_crudeoil/ tree for the latter.
SYMBOL = os.environ.get('PILOT_SYMBOL', 'CRUDEOILM')
_PHASE3_DIR = 'phase3' if SYMBOL == 'CRUDEOILM' else 'phase3_crudeoil'
_OUT_SUFFIX = '' if SYMBOL == 'CRUDEOILM' else '_crudeoil_full'

OUT_DIR = os.path.join(REPO_ROOT, 'research', 'range_detection', 'outputs')
os.makedirs(OUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------

print(f'Loading {SYMBOL} 1-min (front-month stitched)…')
df_1m = dl.load_futures_1min(SYMBOL)
print(f'  {len(df_1m)} 1-min bars  {df_1m.index[0]} -> {df_1m.index[-1]}')

print('Resampling to 15-min (day-anchored, same convention as backtest_p3.py)…')
df_15m = dl.resample_ohlcv(df_1m, '15min')
print(f'  {len(df_15m)} 15-min bars  {df_15m.index[0]} -> {df_15m.index[-1]}')

# ST_15 flip history for cross-reference
trade_summary = pd.read_csv(
    os.path.join(REPO_ROOT, 'prometheus_backtest', _PHASE3_DIR, 'data_sweep', 'mult_2.0', 'trade_summary.csv'),
    parse_dates=['signal_ts'])
print(f'  {len(trade_summary)} ST_15 trend flips (signal_ts) loaded for cross-reference')


# ---------------------------------------------------------------------------
# 3. Run PA at a few parameter combos — 15-min crude bars are a totally
#    different noise/vol regime than daily Nifty bars, so min_range_bars
#    needs its own exploration, not a blind import of the daily winner.
# ---------------------------------------------------------------------------

PARAM_GRID = [
    dict(min_range_bars=5,  breakout_confirm=1),
    dict(min_range_bars=5,  breakout_confirm=2),
    dict(min_range_bars=10, breakout_confirm=1),
    dict(min_range_bars=10, breakout_confirm=2),
    dict(min_range_bars=20, breakout_confirm=2),
    dict(min_range_bars=20, breakout_confirm=3),
]

HORIZONS = [4, 8, 16, 32, 96]   # bars ~ 1h, 2h, 4h, 8h, 24h at 15-min


def close_hold_rate(result, episodes, horizon, min_range_bars):
    """Fraction of ESTABLISHED episodes still ACTIVE (no new range setter
    committed yet -- i.e. episode_id at established_idx+horizon still
    equals this episode's own id) `horizon` bars after the episode BECAME
    established (start_idx + min_range_bars), not from its raw start_idx --
    advisor-caught: measuring from start_idx makes every horizon <=
    min_range_bars tautologically 100% (an episode is trivially "still
    itself" before it's even had a chance to end). Also NOT "is the close
    within the range's own final (already-expanded) bounds," which is
    tautologically 100% by construction. Mirrors §7's validation-gate
    close-hold-rate-by-horizon methodology, corrected for both traps."""
    episode_id_arr = result['episode_id'].values
    n = len(result)
    hits, total = 0, 0
    for ep in episodes:
        if ep['is_transient']:
            continue
        established_idx = ep['start_idx'] + min_range_bars
        check_idx = established_idx + horizon
        if check_idx >= n:
            continue   # ran out of data before this horizon -- inconclusive, excluded
        total += 1
        if episode_id_arr[check_idx] == ep['episode_id']:
            hits += 1
    return (hits / total if total else float('nan')), total


results_summary = []

for params in PARAM_GRID:
    result, episodes = compute_pa_ranges(df_15m, start_idx=0, **params)
    n_ep = len(episodes)
    established = [e for e in episodes if not e['is_transient']]
    n_est = len(established)

    bar_counts = np.array([e['bar_count'] for e in established])
    durations_hrs = np.array([
        (e['end_ts'] - e['start_ts']).total_seconds() / 3600 for e in established
    ])

    up_bias = sum(1 for e in established if e['direction'] == 'up')
    dn_bias = sum(1 for e in established if e['direction'] == 'down')

    hold_rates = {h: close_hold_rate(result, episodes, h, params['min_range_bars']) for h in HORIZONS}

    row = dict(
        min_range_bars=params['min_range_bars'],
        breakout_confirm=params['breakout_confirm'],
        n_episodes=n_ep, n_established=n_est,
        pct_established=round(100 * n_est / n_ep, 1) if n_ep else float('nan'),
        bar_p50=int(np.percentile(bar_counts, 50)) if n_est else None,
        bar_p90=int(np.percentile(bar_counts, 90)) if n_est else None,
        hrs_p50=round(float(np.percentile(durations_hrs, 50)), 1) if n_est else None,
        hrs_p90=round(float(np.percentile(durations_hrs, 90)), 1) if n_est else None,
        up_bias=up_bias, dn_bias=dn_bias,
    )
    for h in HORIZONS:
        rate, n = hold_rates[h]
        row[f'hold_h{h}'] = round(100 * rate, 1) if not np.isnan(rate) else None
        row[f'hold_h{h}_n'] = n
    results_summary.append(row)

    print(f"\n=== min_range_bars={params['min_range_bars']} breakout_confirm={params['breakout_confirm']} ===")
    print(f"  Episodes: {n_ep}  Established: {n_est} ({row['pct_established']}%)")
    if n_est:
        print(f"  Duration (bars): P50={row['bar_p50']} P90={row['bar_p90']}  "
              f"(hrs: P50={row['hrs_p50']} P90={row['hrs_p90']})")
        print(f"  Direction bias: up={up_bias} down={dn_bias}")
        print(f"  Close-hold rate by horizon: " +
              '  '.join(f"h={h}:{row[f'hold_h{h}']}%(n={row[f'hold_h{h}_n']})" for h in HORIZONS))

pd.DataFrame(results_summary).to_csv(os.path.join(OUT_DIR, f'crudeoil_15min_pa_grid{_OUT_SUFFIX}.csv'), index=False)
print(f"\nGrid summary saved: {os.path.join(OUT_DIR, f'crudeoil_15min_pa_grid{_OUT_SUFFIX}.csv')}")


# ---------------------------------------------------------------------------
# 4. Cross-reference against ST_15 flips: does a flip coincide with a
#    genuine PA range break, or does it fire mid-range (chop)?
#    NOTE (advisor-caught lookahead): compute_pa_ranges retroactively
#    rewrites bar_rh/bar_rl/bar_ep for the pending-breakout window on
#    _commit() (lines 225-236) -- so result['episode_id']/
#    close_pct_in_range at a bar inside that window reflect what was only
#    knowable breakout_confirm bars LATER, not what was known in real time
#    at that bar's own close. Since ST_15 flips and PA breakouts are both
#    trend-change detectors on the same 15m closes, they cluster in time --
#    exactly where this contamination bites hardest. Sections 4-6 below
#    report BOTH the naive (contaminated) read against the full-history
#    result AND a point-in-time-correct (walk-forward) read, so the size of
#    the lookahead leak is on record, not silently absorbed into one number.
# ---------------------------------------------------------------------------
MID_PARAMS = dict(min_range_bars=10, breakout_confirm=2)
mid_result, mid_episodes = compute_pa_ranges(df_15m, start_idx=0, **MID_PARAMS)


def _lookup(ts, index):
    if ts in index:
        return index.get_loc(ts) if not isinstance(index.get_loc(ts), slice) else None
    pos = index.searchsorted(ts)
    return pos - 1 if 0 < pos <= len(index) else None


def naive_join(result, episodes):
    """Contaminated read: uses the FULL-HISTORY result, so any bar inside a
    still-pending breakout window (at the time it happened) has already
    been retroactively rewritten by _commit(). Reported ONLY for comparison
    against the point-in-time-correct numbers below."""
    ep_by_id = {e['episode_id']: e for e in episodes}
    ep_arr, pct_arr = result['episode_id'].values, result['close_pct_in_range'].values
    rows = []
    for _, t in trade_summary.iterrows():
        idx = _lookup(t['signal_ts'], result.index)
        if idx is None:
            continue
        ep = ep_by_id.get(ep_arr[idx])
        rows.append(dict(trade_id=t['trade_id'], trade_direction=t['direction'],
                          pnl_points=t['pnl_points'], range_pct=pct_arr[idx],
                          range_direction=(ep['direction'] if ep and not ep['is_transient'] else None),
                          established=(ep is not None and not ep['is_transient'])))
    return pd.DataFrame(rows)


def point_in_time_join(df_15m, trade_summary, min_range_bars, breakout_confirm):
    """Correct read: for each flip at bar i, recompute PA ranges on ONLY
    data through bar i (df_15m.iloc[:i+1]) and read the LAST bar's own
    episode/direction/close_pct_in_range -- exactly what was knowable at
    that bar's own close, with no retroactive rewrite from bars that
    hadn't happened yet. O(389 * mean_bar_index) -- a few seconds."""
    rows = []
    for _, t in trade_summary.iterrows():
        idx = _lookup(t['signal_ts'], df_15m.index)
        if idx is None or idx < min_range_bars:
            continue
        slice_df = df_15m.iloc[:idx + 1]
        res_pit, eps_pit = compute_pa_ranges(slice_df, start_idx=0,
                                             min_range_bars=min_range_bars,
                                             breakout_confirm=breakout_confirm)
        last_ep = eps_pit[-1]
        rows.append(dict(trade_id=t['trade_id'], trade_direction=t['direction'],
                          pnl_points=t['pnl_points'],
                          range_pct=res_pit['close_pct_in_range'].iloc[-1],
                          range_direction=(last_ep['direction'] if not last_ep['is_transient'] else None),
                          established=not last_ep['is_transient']))
    return pd.DataFrame(rows)


def report_position(df, label):
    df = df.dropna(subset=['range_pct', 'pnl_points'])
    edge = (df['range_pct'] <= 15) | (df['range_pct'] >= 85)
    print(f"\n  [{label}] n={len(df)}")
    for name, grp in [('Near edge (<=15%/>=85%)', df[edge]), ('Mid-range (15-85%)', df[~edge])]:
        n = len(grp)
        if n == 0:
            continue
        print(f"    {name:<26} n={n:>3}  win%={(grp['pnl_points']>0).mean()*100:5.1f}  "
              f"avg_pnl={grp['pnl_points'].mean():+7.1f}  median_pnl={grp['pnl_points'].median():+7.1f}  "
              f"total={grp['pnl_points'].sum():+8.0f}")
    corr = np.corrcoef((df['range_pct'] - 50).abs(), df['pnl_points'])[0, 1]
    print(f"    corr(|range_pct-50|, pnl_points) = {corr:+.3f}")


def report_direction(df, label):
    df = df.dropna(subset=['pnl_points'])
    df = df[df['established']]
    print(f"\n  [{label}] {len(df)} flips matched to an established episode")
    for rd in ['up', 'down']:
        for td in ['bullish', 'bearish']:
            grp = df[(df['range_direction'] == rd) & (df['trade_direction'] == td)]
            if grp.empty:
                continue
            agree = (rd == 'up' and td == 'bullish') or (rd == 'down' and td == 'bearish')
            tag = 'AGREES' if agree else 'FIGHTS'
            # advisor-caught: top5/total is nonsense when total is near zero
            # (FIGHTS cells showed 339%/1273%). Use top5 / sum-of-POSITIVE
            # pnl instead -- always well-defined, and answers the actual
            # robustness question ("how much of the WINNING side is a
            # handful of trades").
            pos_sum = grp.loc[grp['pnl_points'] > 0, 'pnl_points'].sum()
            top5_of_wins = (grp.nlargest(5, 'pnl_points')['pnl_points'].clip(lower=0).sum() / pos_sum
                            if pos_sum > 0 else float('nan'))
            print(f"    range={rd:>4} trade={td:>7} ({tag}): n={len(grp):>3}  "
                  f"win%={(grp['pnl_points']>0).mean()*100:5.1f}  avg={grp['pnl_points'].mean():+7.1f}  "
                  f"median={grp['pnl_points'].median():+7.1f}  top5_of_winning_pnl={top5_of_wins:.0%}")


print(f"\n=== Position & direction cross-reference, NAIVE (contaminated) vs. POINT-IN-TIME-CORRECT ===")
print(f"    params: min_range_bars={MID_PARAMS['min_range_bars']} breakout_confirm={MID_PARAMS['breakout_confirm']}")

naive_df = naive_join(mid_result, mid_episodes)
print("\nPosition-vs-P&L:")
report_position(naive_df, 'NAIVE (full-history, contaminated)')

pit_df = point_in_time_join(df_15m, trade_summary, MID_PARAMS['min_range_bars'], MID_PARAMS['breakout_confirm'])
report_position(pit_df, 'POINT-IN-TIME (walk-forward, correct)')

print("\nDirection-vs-P&L:")
report_direction(naive_df, 'NAIVE (full-history, contaminated)')
report_direction(pit_df, 'POINT-IN-TIME (walk-forward, correct)')

naive_df.to_csv(os.path.join(OUT_DIR, f'crudeoil_st15_flip_vs_pa_naive{_OUT_SUFFIX}.csv'), index=False)
pit_df.to_csv(os.path.join(OUT_DIR, f'crudeoil_st15_flip_vs_pa_point_in_time{_OUT_SUFFIX}.csv'), index=False)
print(f"\nSaved: crudeoil_st15_flip_vs_pa_naive.csv, crudeoil_st15_flip_vs_pa_point_in_time.csv")


# ---------------------------------------------------------------------------
# 5. The "replace" test (advisor, priority #1): could PA commit-to-commit
#    direction be its OWN standalone trend-following signal instead of
#    ST_15? Enter at each PA commit in the setter's own direction, exit at
#    the next commit (any direction) -- pure signal quality, no SL/target,
#    same convention as backtest_p3.py's raw ST_15 signal (trade_summary.csv
#    itself: hold-till-flip, fill at next-bar open) -- INCLUDING the same
#    MIN_ENTRY_BUFFER_MIN=15 entry guard ST_15 is subject to (advisor-caught:
#    the first cut let PA trade the day's opening bar, which ST_15's own
#    backtest_p3.py deliberately excludes -- and CRUDEOILM's 09:00 opening
#    bar is the confirmed §11 price-discovery artifact, exactly the kind of
#    bar that manufactures a confirmed-looking whipsaw). Both series also
#    get a 1-tick-per-side cost haircut (tick=1.0 pt on CRUDEOILM, so 2 pts/
#    round-trip) -- not a real slippage model, just parity: neither series
#    had ANY cost modeled before, so an uneven trade count (PA takes far
#    more trades than ST_15) was an uneven advantage.
# ---------------------------------------------------------------------------
print(f"\n=== The 'replace' test: PA commit-to-commit as its own signal vs. ST_15's 389 raw trades ===")
print(f"    (entry-guard parity: MIN_ENTRY_BUFFER_MIN=15 applied to PA too; +2pt/trade cost haircut both sides)")

TICK_COST_PTS_ROUNDTRIP = 2.0   # 1 tick (1.0 pt) each side, CRUDEOILM

first_bar_by_day = pd.Series(df_15m.index, index=df_15m.index.normalize()).groupby(level=0).min()


def _past_entry_guard(ts):
    fb = first_bar_by_day.get(ts.normalize())
    elapsed = (ts - fb).total_seconds() / 60.0 if fb is not None else None
    return elapsed is not None and elapsed >= 15

# NOTE: min_range_bars does NOT affect which breakouts commit (it only
# gates the is_transient classification for established-range use cases
# above) -- so it's a no-op for a "trade every commit" signal. Only
# breakout_confirm changes the actual commit sequence here.
for bc in [0, 1, 2, 3]:
    params = dict(min_range_bars=10, breakout_confirm=bc)
    result, episodes = compute_pa_ranges(df_15m, start_idx=0, **params)
    opens = df_15m['open'].values
    n = len(df_15m)
    pa_trades = []
    n_opening_bar_commits, n_guard_dropped = 0, 0
    # Every commit (episode boundary after the first) is both an exit of the
    # prior PA "position" and an entry of the new one -- same always-in-
    # market structure as ST_15's raw signal. Direction = the NEW episode's
    # setter direction (skip 'initial', which has no directional signal).
    #
    # advisor-caught (2nd consultation, 3rd round): the commit isn't KNOWABLE
    # until breakout_confirm bars after start_idx (the setter bar) have
    # closed still outside the range -- start_idx alone is just where the
    # setter happened, not where it was confirmed. Entering at start_idx+1
    # regardless of bc fills DURING the still-uncertain confirmation window,
    # which is by selection biased toward the breakout direction (that's
    # what "still outside" means) -- an unearned edge that grows with bc.
    # Earliest honest fill is the open of start_idx + bc + 1.
    for k in range(1, len(episodes)):
        ep = episodes[k]
        if ep['direction'] not in ('up', 'down'):
            continue
        entry_idx = ep['start_idx'] + bc + 1   # earliest bar-open after the commit is actually knowable
        if entry_idx >= n:
            continue
        entry_ts = df_15m.index[entry_idx]
        elapsed_at_setter = (df_15m.index[ep['start_idx']] - first_bar_by_day.get(df_15m.index[ep['start_idx']].normalize())).total_seconds() / 60.0
        if elapsed_at_setter == 0:
            n_opening_bar_commits += 1
        if not _past_entry_guard(entry_ts):
            n_guard_dropped += 1
            continue
        exit_idx = episodes[k + 1]['start_idx'] + bc + 1 if k + 1 < len(episodes) else n - 1
        if exit_idx >= n:
            exit_idx = n - 1
        entry_price, exit_price = opens[entry_idx], opens[exit_idx]
        pnl = (exit_price - entry_price) if ep['direction'] == 'up' else (entry_price - exit_price)
        pa_trades.append(pnl)

    pa_trades = np.array(pa_trades)
    st15_pnl = trade_summary['pnl_points'].dropna().values
    pa_trades_net = pa_trades - TICK_COST_PTS_ROUNDTRIP
    st15_pnl_net = st15_pnl - TICK_COST_PTS_ROUNDTRIP
    print(f"\n  min_range_bars={params['min_range_bars']} breakout_confirm={params['breakout_confirm']}:")
    print(f"    ({n_opening_bar_commits} of {len(episodes)-1} commits fired on a day's opening bar; "
          f"{n_guard_dropped} directional commits dropped by the entry guard)")
    print(f"    PA-as-signal   RAW: n={len(pa_trades):>4}  win%={(pa_trades>0).mean()*100:5.1f}  "
          f"avg={pa_trades.mean():+7.1f}  total={pa_trades.sum():+9.0f} pts")
    print(f"    PA-as-signal   NET (after {TICK_COST_PTS_ROUNDTRIP:.0f}pt/trade cost): "
          f"total={pa_trades_net.sum():+9.0f} pts  avg={pa_trades_net.mean():+7.1f}")
    print(f"    ST_15 (ref)    RAW: n={len(st15_pnl):>4}  win%={(st15_pnl>0).mean()*100:5.1f}  "
          f"avg={st15_pnl.mean():+7.1f}  total={st15_pnl.sum():+9.0f} pts")
    print(f"    ST_15 (ref)    NET (after {TICK_COST_PTS_ROUNDTRIP:.0f}pt/trade cost): "
          f"total={st15_pnl_net.sum():+9.0f} pts  avg={st15_pnl_net.mean():+7.1f}")


# ---------------------------------------------------------------------------
# 6. The "bold" test (advisor): the mean-reversion side of the SAME
#    detector. Every established range that HOLDS is a fade opportunity
#    Prometheus structurally cannot take (always-in-market on trend). Enter
#    toward the range midpoint when price closes in the outer 15% of an
#    ESTABLISHED range (PIT-correct, two-pass: cheap naive candidate scan,
#    then a real walk-forward recompute per candidate), exit at the
#    midpoint or when the range itself breaks -- whichever first, capped
#    hold to avoid pathological holds. Sequential positions only (no
#    overlapping fades). Same entry guard + cost haircut as the trend test.
# ---------------------------------------------------------------------------
print(f"\n=== The 'bold' test: fade engine (mean-reversion side of the same detector) ===")

FADE_MAX_HOLD_BARS = 96   # ~1 trading day at 15-min -- avoid pathological holds
closes = df_15m['close'].values
opens_arr = df_15m['open'].values
ts_index = df_15m.index
established_ids = {e['episode_id'] for e in mid_episodes if not e['is_transient']}

cand_idx = np.where(
    ((mid_result['close_pct_in_range'] <= 15) | (mid_result['close_pct_in_range'] >= 85)) &
    (mid_result['episode_id'].isin(established_ids))
)[0]
print(f"  {len(cand_idx)} naive candidate bars (outer 15% of an established range) before PIT verification")

fade_trades = []
n_pit_confirmed = 0
in_position_until = -1

for i in cand_idx:
    if i <= in_position_until or i < MID_PARAMS['min_range_bars']:
        continue
    entry_idx = i + 1
    if entry_idx >= len(df_15m) or not _past_entry_guard(ts_index[entry_idx]):
        continue
    slice_df = df_15m.iloc[:i + 1]
    res_pit, eps_pit = compute_pa_ranges(slice_df, start_idx=0, **MID_PARAMS)
    last_ep = eps_pit[-1]
    pct_pit = res_pit['close_pct_in_range'].iloc[-1]
    if last_ep['is_transient'] or not (pct_pit <= 15 or pct_pit >= 85):
        continue
    n_pit_confirmed += 1

    direction = 'bullish' if pct_pit <= 15 else 'bearish'
    range_mid_at_entry = last_ep['range_mid']
    range_high_at_entry = last_ep['range_high']
    range_low_at_entry = last_ep['range_low']
    entry_price = opens_arr[entry_idx]

    # advisor-caught (2nd consultation, 3rd round): the old exit compared
    # mid_result['episode_id'] (full-history, retroactively rewritten by
    # _commit()) against entry_ep_id. A setter bar that later gets DENIED
    # still shows the OLD id in that full-history read -- so a false
    # breakout that price never really escaped from (in real time) was
    # silently treated as "still inside," letting the fade ride through it
    # for profit it wouldn't have gotten live. Worse, if entry_idx itself
    # was a setter bar that later confirmed, entry_ep_id was the NEW
    # episode, so a fade already sitting on the wrong side of a confirmed
    # breakout would never trigger "broke" until that new episode ended --
    # riding a real breakout against itself for up to FADE_MAX_HOLD_BARS.
    # Fix: exit purely on the PIT range bounds captured at entry (causal,
    # matches the parent plan's slow-in/fast-out convention) -- no
    # episode_id lookup at all.
    exit_idx = None
    for j in range(entry_idx, min(entry_idx + FADE_MAX_HOLD_BARS, len(df_15m))):
        c = closes[j]
        hit_mid = (c >= range_mid_at_entry) if direction == 'bullish' else (c <= range_mid_at_entry)
        broke = (c > range_high_at_entry) or (c < range_low_at_entry)
        if hit_mid or broke:
            exit_idx = j
            break
    if exit_idx is None:
        exit_idx = min(entry_idx + FADE_MAX_HOLD_BARS - 1, len(df_15m) - 1)

    exit_price = closes[exit_idx]
    pnl = (exit_price - entry_price) if direction == 'bullish' else (entry_price - exit_price)
    fade_trades.append(dict(entry_ts=ts_index[entry_idx], direction=direction, pnl_points=pnl,
                             hold_bars=exit_idx - entry_idx))
    in_position_until = exit_idx

fade_df = pd.DataFrame(fade_trades)
print(f"  {n_pit_confirmed} PIT-confirmed candidates -> {len(fade_df)} non-overlapping fade trades taken")
fade_net_total = 0.0
if len(fade_df):
    raw = fade_df['pnl_points'].values
    net = raw - TICK_COST_PTS_ROUNDTRIP
    fade_net_total = net.sum()
    print(f"  Fade engine RAW: n={len(raw)}  win%={(raw>0).mean()*100:.1f}  avg={raw.mean():+.1f}  total={raw.sum():+.0f} pts")
    print(f"  Fade engine NET: total={net.sum():+.0f} pts  avg={net.mean():+.1f}")
    worst5 = fade_df.nsmallest(5, 'pnl_points')['pnl_points'].round(1).tolist()
    print(f"  Worst 5 trades (raw, fat-tail check -- fades in a trend-prone instrument are where these live): {worst5}")
    fade_df.to_csv(os.path.join(OUT_DIR, f'crudeoil_fade_engine_trades{_OUT_SUFFIX}.csv'), index=False)
    print(f"  Saved: crudeoil_fade_engine_trades.csv")
else:
    print("  No fade trades taken -- nothing to report.")


# ---------------------------------------------------------------------------
# 7. Composite: PA-trend engine (breakout_confirm=1, the best replace-test
#    cell) + fade engine together, vs. ST_15 alone. Correlation between the
#    two engines' own weekly P&L streams -- the property that matters if
#    the goal is a genuinely complementary second strategy, not a filter.
# ---------------------------------------------------------------------------
print(f"\n=== Composite: PA-trend + fade engines together, vs. ST_15 alone ===")

params_trend = dict(min_range_bars=10, breakout_confirm=1)
result_t, episodes_t = compute_pa_ranges(df_15m, start_idx=0, **params_trend)
trend_trades = []
for k in range(1, len(episodes_t)):
    ep = episodes_t[k]
    if ep['direction'] not in ('up', 'down'):
        continue
    # Same fix as the replace test: earliest honest fill is start_idx + bc + 1.
    entry_idx = ep['start_idx'] + params_trend['breakout_confirm'] + 1
    if entry_idx >= len(df_15m) or not _past_entry_guard(ts_index[entry_idx]):
        continue
    exit_idx = min(episodes_t[k + 1]['start_idx'] + params_trend['breakout_confirm'] + 1
                    if k + 1 < len(episodes_t) else len(df_15m) - 1,
                    len(df_15m) - 1)
    entry_price, exit_price = opens_arr[entry_idx], opens_arr[exit_idx]
    pnl = (exit_price - entry_price) if ep['direction'] == 'up' else (entry_price - exit_price)
    trend_trades.append(dict(entry_ts=ts_index[entry_idx], pnl_points=pnl - TICK_COST_PTS_ROUNDTRIP))
trend_df = pd.DataFrame(trend_trades)


def weekly_pnl(df):
    s = df.set_index('entry_ts')['pnl_points']
    s.index = pd.DatetimeIndex(s.index)
    return s.resample('W').sum()


trend_weekly = weekly_pnl(trend_df)
fade_weekly = weekly_pnl(fade_df.assign(pnl_points=fade_df['pnl_points'] - TICK_COST_PTS_ROUNDTRIP)) if len(fade_df) else pd.Series(dtype=float)
st15_for_weekly = trade_summary[['signal_ts', 'pnl_points']].dropna().rename(columns={'signal_ts': 'entry_ts'})
st15_for_weekly['pnl_points'] -= TICK_COST_PTS_ROUNDTRIP
st15_weekly = weekly_pnl(st15_for_weekly)

combined_idx = trend_weekly.index.union(fade_weekly.index).union(st15_weekly.index)
trend_w = trend_weekly.reindex(combined_idx, fill_value=0)
fade_w = fade_weekly.reindex(combined_idx, fill_value=0)
st15_w = st15_weekly.reindex(combined_idx, fill_value=0)

corr_trend_fade = np.corrcoef(trend_w, fade_w)[0, 1] if len(fade_df) else float('nan')
corr_trend_st15 = np.corrcoef(trend_w, st15_w)[0, 1]

composite_total = trend_df['pnl_points'].sum() + fade_net_total
st15_net_total = st15_for_weekly['pnl_points'].sum()

print(f"  PA-trend engine (confirm=1) net total: {trend_df['pnl_points'].sum():+9.0f} pts (n={len(trend_df)})")
print(f"  Fade engine net total:                 {fade_net_total:+9.0f} pts (n={len(fade_df)})")
print(f"  Composite (trend+fade) net total:      {composite_total:+9.0f} pts")
print(f"  ST_15 alone net total:                 {st15_net_total:+9.0f} pts (n={len(trade_summary)})")
print(f"  corr(weekly PA-trend P&L, weekly fade P&L)  = {corr_trend_fade:+.3f}")
print(f"  corr(weekly PA-trend P&L, weekly ST_15 P&L) = {corr_trend_st15:+.3f}")

# ---------------------------------------------------------------------------
# 8. Direct overlap check (advisor): the +0.63 weekly correlation is
#    suggestive but indirect. Does a PA-trend commit actually fire within a
#    couple bars of the matching ST_15 flip, bar-for-bar? Uses the SAME
#    honestly-timed commit bar (start_idx + bc + 1) as the replace test.
# ---------------------------------------------------------------------------
print(f"\n=== Direct overlap: does a same-direction PA-trend commit fire within +/-2 bars of each ST_15 flip? ===")

pa_commit_bars = []
for k in range(1, len(episodes_t)):
    ep = episodes_t[k]
    if ep['direction'] not in ('up', 'down'):
        continue
    bar = ep['start_idx'] + params_trend['breakout_confirm'] + 1
    if bar < len(df_15m):
        pa_commit_bars.append((bar, ep['direction']))

overlap_hits = 0
for _, t in trade_summary.iterrows():
    idx = _lookup(t['signal_ts'], df_15m.index)
    if idx is None:
        continue
    want_dir = 'up' if t['direction'] == 'bullish' else 'down'
    if any(abs(bar - idx) <= 2 and d == want_dir for bar, d in pa_commit_bars):
        overlap_hits += 1
print(f"  {overlap_hits} / {len(trade_summary)} ST_15 flips ({100*overlap_hits/len(trade_summary):.1f}%) have a "
      f"same-direction PA-trend (confirm=1) commit within +/-2 bars")


# ---------------------------------------------------------------------------
# 9. Causal co-detection split (advisor, final round): the +/-2-bar overlap
#    above admits PA commits that hadn't happened yet at ST_15's own entry --
#    fine for "is this the same signal," not for "would this work as a
#    filter." Here: a PA commit is CAUSALLY knowable at ST_15's entry only if
#    its confirmation bar (start_idx + bc) closed AT OR BEFORE the ST_15
#    flip's own signal bar (idx) -- ST_15 enters at idx+1's open, so it only
#    knows through idx's close. Window capped at 4 bars back (~1h) so a stale
#    PA commit from hours earlier doesn't count as "concurrent." If PA-trend
#    has value as a CONFIRMATION FILTER on ST_15 (rather than as its own
#    standalone signal, already rejected in Sec 5), unmatched ST_15 flips
#    should be the weak ones.
# ---------------------------------------------------------------------------
print(f"\n=== Causal co-detection split: ST_15 flips WITH vs WITHOUT a causally-knowable same-direction PA commit ===")
print(f"    (reuses pa_commit_bars, confirm={params_trend['breakout_confirm']}, from Sec 7/8 above)")

matched_pnl, unmatched_pnl = [], []
for _, t in trade_summary.iterrows():
    idx = _lookup(t['signal_ts'], df_15m.index)
    if idx is None or pd.isna(t['pnl_points']):
        continue
    want_dir = 'up' if t['direction'] == 'bullish' else 'down'
    is_matched = any((idx - 4) <= (bar - 1) <= idx and d == want_dir for bar, d in pa_commit_bars)
    (matched_pnl if is_matched else unmatched_pnl).append(t['pnl_points'])

matched_pnl, unmatched_pnl = np.array(matched_pnl), np.array(unmatched_pnl)
for name, arr in [('MATCHED  (causal PA co-detection)', matched_pnl), ('UNMATCHED (no causal PA co-detection)', unmatched_pnl)]:
    if len(arr) == 0:
        print(f"  {name:<40} n=0")
        continue
    print(f"  {name:<40} n={len(arr):>3}  win%={(arr>0).mean()*100:5.1f}  "
          f"avg={arr.mean():+7.1f}  total={arr.sum():+8.0f} pts")

print("\nDone.")
