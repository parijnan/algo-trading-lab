"""
range_detector_pa.py — Price-action range detector for Nifty.

Identifies consolidation ranges using "range setter" candles:
  - A candle that closes outside the current range becomes the new range setter.
    Its H/L define the new range bounds.
  - Subsequent candles that make a new H or L but fail to close outside the
    current bounds expand the range via wick extension.
  - Gap openings: if the range setter's open is already outside the previous
    range, the inside bound is anchored to the previous range's near boundary
    rather than the candle's own wick.

Usage:
    python range_detector_pa.py --timeframe daily --start-date 2024-01-15
    python range_detector_pa.py --timeframe 75 --start-date "2024-01-15 09:15"
    python range_detector_pa.py --timeframe 15 --start-date 2024-01-15 --min-range-bars 8
    python range_detector_pa.py --timeframe daily --start-date 2024-01-15 --all
"""

import os
import sys
import argparse
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from resample import load_data, timeframe_label

OUTPUT_DIR = os.path.join(BASE_DIR, 'outputs')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
_ESTABLISHED_FILL   = 'rgba(30, 120, 255, 0.12)'
_ESTABLISHED_LINE   = 'rgba(30, 120, 255, 0.70)'
_ESTABLISHED_MID    = 'rgba(30, 120, 255, 0.45)'
_TRANSIENT_FILL     = 'rgba(160, 160, 160, 0.08)'
_TRANSIENT_LINE     = 'rgba(160, 160, 160, 0.40)'
_TRANSIENT_MID      = 'rgba(160, 160, 160, 0.30)'
_SETTER_UP_COLOR    = '#00e676'
_SETTER_DOWN_COLOR  = '#ff5252'
_SETTER_INIT_COLOR  = '#ffeb3b'

# ---------------------------------------------------------------------------
# Core algorithm
# ---------------------------------------------------------------------------

def compute_pa_ranges(df: pd.DataFrame, start_idx: int, min_range_bars: int = 5):
    """
    Price-action range detection from start_idx onwards.

    Returns
    -------
    result : pd.DataFrame
        Original df with added columns: range_high, range_low, range_mid,
        episode_id, close_pct_in_range.
    episodes : list[dict]
        One dict per episode with start/end metadata.
    """
    n      = len(df)
    opens  = df['open'].values
    highs  = df['high'].values
    lows   = df['low'].values
    closes = df['close'].values

    bar_range_high = np.full(n, np.nan)
    bar_range_low  = np.full(n, np.nan)
    bar_episode_id = np.full(n, -1, dtype=int)

    episodes  = []
    ep_id     = 0
    ep_start  = start_idx
    rh        = highs[start_idx]
    rl        = lows[start_idx]
    direction = 'initial'
    gap_open  = False
    prev_rh = prev_rl = None

    bar_range_high[start_idx] = rh
    bar_range_low[start_idx]  = rl
    bar_episode_id[start_idx] = ep_id

    for i in range(start_idx + 1, n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]

        broke_up   = c > rh
        broke_down = c < rl

        if broke_up or broke_down:
            ep_bars = i - ep_start
            episodes.append({
                'episode_id':   ep_id,
                'start_idx':    ep_start,
                'end_idx':      i - 1,
                'start_ts':     df.index[ep_start],
                'end_ts':       df.index[i - 1],
                'bar_count':    ep_bars,
                'is_transient': ep_bars < min_range_bars,
                'range_high':   rh,
                'range_low':    rl,
                'range_mid':    (rh + rl) / 2,
                'direction':    direction,
                'gap_open':     gap_open,
            })

            prev_rh, prev_rl = rh, rl
            ep_id    += 1
            ep_start  = i

            if broke_up:
                direction = 'up'
                gap_open  = o > prev_rh
                new_rh    = h
                new_rl    = prev_rh if gap_open else l
            else:
                direction = 'down'
                gap_open  = o < prev_rl
                new_rl    = l
                new_rh    = prev_rl if gap_open else h

            rh, rl = new_rh, new_rl
        else:
            if h > rh: rh = h
            if l < rl: rl = l

        bar_range_high[i] = rh
        bar_range_low[i]  = rl
        bar_episode_id[i] = ep_id

    # Finalize last (open) episode
    ep_bars = n - ep_start
    episodes.append({
        'episode_id':   ep_id,
        'start_idx':    ep_start,
        'end_idx':      n - 1,
        'start_ts':     df.index[ep_start],
        'end_ts':       df.index[-1],
        'bar_count':    ep_bars,
        'is_transient': ep_bars < min_range_bars,
        'range_high':   rh,
        'range_low':    rl,
        'range_mid':    (rh + rl) / 2,
        'direction':    direction,
        'gap_open':     gap_open,
    })

    spread = bar_range_high - bar_range_low
    with np.errstate(invalid='ignore', divide='ignore'):
        pct_in_range = np.where(spread > 0,
                                100 * (closes - bar_range_low) / spread,
                                50.0)

    result = df.copy()
    result['range_high']        = bar_range_high
    result['range_low']         = bar_range_low
    result['range_mid']         = (bar_range_high + bar_range_low) / 2
    result['episode_id']        = bar_episode_id
    result['close_pct_in_range'] = pct_in_range

    return result, episodes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_start_idx(df: pd.DataFrame, start_date_str: str, timeframe) -> int:
    """Return the index of the candle matching or just after start_date_str."""
    if timeframe == 'daily':
        target = pd.Timestamp(start_date_str).normalize()
    else:
        target = pd.Timestamp(start_date_str)

    idx = df.index.searchsorted(target)
    if idx >= len(df):
        raise ValueError(f'--start-date {start_date_str} is beyond the data range.')
    return int(idx)


def _is_intraday(timeframe) -> bool:
    return timeframe != 'daily'


def _x_extension(timeframe):
    """How far to extend the last (open) episode rectangle beyond the last bar."""
    if timeframe == 'daily':
        return pd.Timedelta(days=1)
    return pd.Timedelta(minutes=int(timeframe))


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------

def plot(result: pd.DataFrame, episodes: list, timeframe,
         from_date, to_date=None, min_range_bars: int = 5,
         label: str = '', no_browser: bool = False):

    view = result.loc[from_date:to_date] if to_date else result.loc[from_date:]
    if view.empty:
        print(f'No data in window {from_date}–{to_date}')
        return

    # Episodes that overlap the view window
    view_start = view.index[0]
    view_end   = view.index[-1]
    visible_eps = [
        ep for ep in episodes
        if ep['end_ts'] >= view_start and ep['start_ts'] <= view_end
    ]

    fig = make_subplots(rows=1, cols=1)

    # --- Candlestick ---
    fig.add_trace(go.Candlestick(
        x=view.index,
        open=view['open'], high=view['high'],
        low=view['low'],   close=view['close'],
        name='Nifty',
        increasing_line_color='#26a69a',
        decreasing_line_color='#ef5350',
        whiskerwidth=0.3,
    ))

    ext = _x_extension(timeframe)

    # --- Range rectangles and mid-lines per episode ---
    for ep in visible_eps:
        is_last = ep['episode_id'] == episodes[-1]['episode_id']
        x0 = ep['start_ts']
        x1 = ep['end_ts'] + ext if is_last else ep['end_ts']
        y0 = ep['range_low']
        y1 = ep['range_high']
        mid = ep['range_mid']

        if ep['is_transient']:
            fill  = _TRANSIENT_FILL
            line  = dict(color=_TRANSIENT_LINE, width=1, dash='dash')
            mline = dict(color=_TRANSIENT_MID,  width=1, dash='dot')
        else:
            fill  = _ESTABLISHED_FILL
            line  = dict(color=_ESTABLISHED_LINE, width=1)
            mline = dict(color=_ESTABLISHED_MID,  width=1, dash='dot')

        fig.add_shape(type='rect',
                      xref='x', yref='y',
                      x0=x0, x1=x1, y0=y0, y1=y1,
                      fillcolor=fill, line=line, layer='below')

        fig.add_shape(type='line',
                      xref='x', yref='y',
                      x0=x0, x1=x1, y0=mid, y1=mid,
                      line=mline, layer='below')

    # --- Range setter markers ---
    up_x, up_y, up_text     = [], [], []
    dn_x, dn_y, dn_text     = [], [], []
    init_x, init_y          = [], []

    for ep in visible_eps:
        ts = ep['start_ts']
        if ts not in view.index:
            continue
        bar  = view.loc[ts]
        d    = ep['direction']
        gtag = '  GAP' if ep['gap_open'] else ''
        if d == 'up':
            up_x.append(ts); up_y.append(bar['low'] * 0.9995)
            up_text.append(f'↑{gtag}')
        elif d == 'down':
            dn_x.append(ts); dn_y.append(bar['high'] * 1.0005)
            dn_text.append(f'↓{gtag}')
        else:
            init_x.append(ts); init_y.append(bar['low'] * 0.9995)

    if up_x:
        fig.add_trace(go.Scatter(
            x=up_x, y=up_y, mode='markers+text',
            marker=dict(symbol='triangle-up', color=_SETTER_UP_COLOR, size=10,
                        line=dict(width=1, color='#222')),
            text=up_text, textposition='bottom center',
            textfont=dict(size=9, color='#aaa'),
            name='Setter ↑', showlegend=False,
        ))
    if dn_x:
        fig.add_trace(go.Scatter(
            x=dn_x, y=dn_y, mode='markers+text',
            marker=dict(symbol='triangle-down', color=_SETTER_DOWN_COLOR, size=10,
                        line=dict(width=1, color='#222')),
            text=dn_text, textposition='top center',
            textfont=dict(size=9, color='#aaa'),
            name='Setter ↓', showlegend=False,
        ))
    if init_x:
        fig.add_trace(go.Scatter(
            x=init_x, y=init_y, mode='markers',
            marker=dict(symbol='diamond', color=_SETTER_INIT_COLOR, size=10,
                        line=dict(width=1, color='#222')),
            name='Initial setter', showlegend=False,
        ))

    # --- Layout ---
    tf_label = timeframe_label(timeframe)
    title = f'Nifty PA Range Detection ({tf_label}){" — " + label if label else ""}'
    fig.update_layout(
        title=title,
        template='plotly_dark',
        xaxis_rangeslider_visible=False,
        xaxis_title='',
        yaxis_title='Price',
        height=700,
        margin=dict(l=60, r=40, t=60, b=40),
        legend=dict(orientation='h', yanchor='bottom', y=1.01, xanchor='left', x=0),
    )

    if _is_intraday(timeframe):
        fig.update_xaxes(rangebreaks=[
            dict(bounds=[15.5, 9.25], pattern='hour'),
            dict(bounds=['sat', 'mon']),
        ])

    # --- Summary annotation ---
    last_ep = episodes[-1]
    last_close = result['close'].iloc[-1]
    pct = result['close_pct_in_range'].iloc[-1]
    status = 'TRANSIENT' if last_ep['is_transient'] else 'ESTABLISHED'
    ann = (f'Last range: {last_ep["range_low"]:.0f}–{last_ep["range_high"]:.0f} '
           f'| mid {last_ep["range_mid"]:.0f} '
           f'| close {last_close:.0f} ({pct:.1f}% in range) '
           f'| {last_ep["bar_count"]} bars [{status}]')
    fig.add_annotation(text=ann, xref='paper', yref='paper',
                       x=0.01, y=0.01, showarrow=False,
                       font=dict(size=11, color='#ccc'),
                       align='left', bgcolor='rgba(0,0,0,0.4)')

    return fig


def _save_and_show(fig, path, no_browser):
    fig.write_html(path, include_plotlyjs='cdn')
    print(f'Saved: {path}')
    if not no_browser:
        import webbrowser
        webbrowser.open(f'file://{os.path.abspath(path)}')


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_episodes_csv(episodes: list, path: str):
    rows = []
    for ep in episodes:
        rows.append({
            'episode_id':    ep['episode_id'],
            'episode_start': ep['start_ts'],
            'episode_end':   ep['end_ts'],
            'bar_count':     ep['bar_count'],
            'is_transient':  ep['is_transient'],
            'direction':     ep['direction'],
            'gap_open':      ep['gap_open'],
            'range_high':    round(ep['range_high'], 2),
            'range_low':     round(ep['range_low'],  2),
            'range_mid':     round(ep['range_mid'],  2),
            'width_pts':     round(ep['range_high'] - ep['range_low'], 2),
            'width_pct':     round(100 * (ep['range_high'] - ep['range_low']) / ep['range_mid'], 3),
        })
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f'Episodes CSV: {path}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Price-action range detector for Nifty.')
    parser.add_argument('--timeframe',      default='daily',
                        help='daily | integer minutes (e.g. 75, 15, 5, 3)')
    parser.add_argument('--start-date',     required=True,
                        help='Initial range setter candle: YYYY-MM-DD or "YYYY-MM-DD HH:MM"')
    parser.add_argument('--min-range-bars', type=int, default=5,
                        help='Min bars for a range to be considered established (default: 5)')
    parser.add_argument('--months',         type=int, default=None,
                        help='Months to display in single-chart mode (default: all data)')
    parser.add_argument('--all',            action='store_true',
                        help='Generate one chart per calendar year + episodes CSV')
    parser.add_argument('--years',          type=int, nargs='+',
                        help='Restrict --all output to specific years')
    parser.add_argument('--tag',            default='',
                        help='Suffix added to output filenames (for tuning runs)')
    parser.add_argument('--no-browser',     action='store_true',
                        help='Save HTML without opening in browser')
    args = parser.parse_args()

    # Parse timeframe
    tf = args.timeframe
    if tf != 'daily':
        try:
            tf = int(tf)
        except ValueError:
            print(f'Invalid --timeframe "{tf}". Use "daily" or an integer (e.g. 75).')
            sys.exit(1)

    tf_label = timeframe_label(tf)
    tag      = f'_{args.tag}' if args.tag else ''

    print(f'Loading {tf_label} data…')
    df = load_data(tf)
    print(f'  {len(df)} bars from {df.index[0].date()} to {df.index[-1].date()}')

    start_idx = _find_start_idx(df, args.start_date, tf)
    print(f'  Start candle: {df.index[start_idx]}  (index {start_idx})')

    print('Computing PA ranges…')
    result, episodes = compute_pa_ranges(df, start_idx, args.min_range_bars)

    est  = sum(1 for e in episodes if not e['is_transient'])
    trans = len(episodes) - est
    print(f'  {len(episodes)} episodes total — {est} established, {trans} transient')

    if args.all:
        years = args.years or sorted({df.index[i].year for i in range(start_idx, len(df))})
        csv_path = os.path.join(OUTPUT_DIR, f'range_episodes_pa_{tf_label}{tag}.csv')
        export_episodes_csv(episodes, csv_path)

        for year in years:
            from_date = pd.Timestamp(f'{year}-01-01')
            to_date   = pd.Timestamp(f'{year}-12-31')
            fig = plot(result, episodes, tf, from_date, to_date,
                       args.min_range_bars, label=str(year), no_browser=True)
            if fig is None:
                continue
            fname = f'range_chart_pa_{tf_label}_{year}{tag}.html'
            _save_and_show(fig, os.path.join(OUTPUT_DIR, fname), args.no_browser)
    else:
        if args.months:
            from_date = df.index[-1] - pd.DateOffset(months=args.months)
        else:
            from_date = df.index[start_idx]
        fig = plot(result, episodes, tf, from_date, None,
                   args.min_range_bars, no_browser=args.no_browser)
        if fig:
            fname = f'range_chart_pa_{tf_label}{tag}.html'
            _save_and_show(fig, os.path.join(OUTPUT_DIR, fname), args.no_browser)


if __name__ == '__main__':
    main()
