"""
range_detector_pa.py — Price-action range detector for Nifty, with optional ADX hybrid modes.

PA logic:
  - A candle that closes outside the current range becomes the new range setter.
    Its H/L define the new range bounds.
  - Subsequent candles that make a new H or L but fail to close outside the
    current bounds expand the range via wick extension.
  - Gap openings: if the range setter's open is already outside the previous
    range, the inside bound is anchored to the previous range's near boundary
    rather than the candle's own wick.

Hybrid modes (--hybrid):
  none  Pure PA. Established = bar_count >= min_range_bars. (default)
  a     Option A: ADX hard gate. Established = bar_count >= min_range_bars
        AND avg episode ADX < adx_threshold. Long PA episodes during trends
        are drawn in orange (trending) instead of blue (established).

Usage:
    python range_detector_pa.py --timeframe daily --start-date 2023-05-23 --all
    python range_detector_pa.py --timeframe daily --start-date 2023-05-23 --hybrid a --all
    python range_detector_pa.py --timeframe 75 --start-date "2024-01-02 09:15" --months 3
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
_ESTABLISHED_FILL = 'rgba(30, 120, 255, 0.12)'
_ESTABLISHED_LINE = 'rgba(30, 120, 255, 0.70)'
_ESTABLISHED_MID  = 'rgba(30, 120, 255, 0.45)'
_TRENDING_FILL    = 'rgba(255, 140, 0, 0.10)'
_TRENDING_LINE    = 'rgba(255, 140, 0, 0.55)'
_TRENDING_MID     = 'rgba(255, 140, 0, 0.30)'
_TRANSIENT_FILL   = 'rgba(160, 160, 160, 0.08)'
_TRANSIENT_LINE   = 'rgba(160, 160, 160, 0.40)'
_TRANSIENT_MID    = 'rgba(160, 160, 160, 0.30)'
_SETTER_UP_COLOR  = '#00e676'
_SETTER_DN_COLOR  = '#ff5252'
_SETTER_IN_COLOR  = '#ffeb3b'

# ---------------------------------------------------------------------------
# ADX (Wilder)
# ---------------------------------------------------------------------------

def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    highs  = df['high'].values
    lows   = df['low'].values
    closes = df['close'].values
    n = len(df)

    tr  = np.zeros(n)
    pdm = np.zeros(n)
    ndm = np.zeros(n)
    for i in range(1, n):
        tr[i]  = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i]  - closes[i - 1]))
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        pdm[i] = up   if up > down and up > 0   else 0.0
        ndm[i] = down if down > up and down > 0 else 0.0

    atr  = np.zeros(n)
    apdm = np.zeros(n)
    andm = np.zeros(n)
    if n > period:
        atr[period]  = tr[1:period + 1].sum()
        apdm[period] = pdm[1:period + 1].sum()
        andm[period] = ndm[1:period + 1].sum()
        for i in range(period + 1, n):
            atr[i]  = atr[i - 1]  - atr[i - 1]  / period + tr[i]
            apdm[i] = apdm[i - 1] - apdm[i - 1] / period + pdm[i]
            andm[i] = andm[i - 1] - andm[i - 1] / period + ndm[i]

    with np.errstate(invalid='ignore', divide='ignore'):
        pdi = np.where(atr > 0, 100 * apdm / atr, 0.0)
        ndi = np.where(atr > 0, 100 * andm / atr, 0.0)
        dx  = np.where((pdi + ndi) > 0,
                       100 * np.abs(pdi - ndi) / (pdi + ndi), 0.0)

    adx_out = np.full(n, np.nan)
    start = period * 2
    if n > start:
        adx_out[start] = dx[period:start + 1].mean()
        for i in range(start + 1, n):
            adx_out[i] = (adx_out[i - 1] * (period - 1) + dx[i]) / period

    return pd.Series(adx_out, index=df.index, name='adx')


def annotate_adx(result: pd.DataFrame, episodes: list, adx_threshold: float) -> list:
    """Add avg_adx and is_adx_ranging to each episode dict."""
    adx_vals = result['adx'].values
    for ep in episodes:
        ep_adx = adx_vals[ep['start_idx']:ep['end_idx'] + 1]
        ep_adx = ep_adx[~np.isnan(ep_adx)]
        if len(ep_adx) > 0:
            avg = float(np.mean(ep_adx))
            ep['avg_adx']        = round(avg, 1)
            ep['is_adx_ranging'] = avg < adx_threshold
        else:
            ep['avg_adx']        = float('nan')
            ep['is_adx_ranging'] = True
    return episodes


# ---------------------------------------------------------------------------
# Core PA algorithm
# ---------------------------------------------------------------------------

def compute_pa_ranges(df: pd.DataFrame, start_idx: int,
                      min_range_bars: int = 5, breakout_confirm: int = 1):
    """
    Returns
    -------
    result    : pd.DataFrame with range_high, range_low, range_mid,
                episode_id, close_pct_in_range added.
    episodes  : list[dict], one per episode.

    breakout_confirm : int
        Number of additional closes that must stay outside the range after the
        initial breakout close before a new range setter is committed.
        0 = immediate commit (original behaviour).
        1 = one confirmation bar required (false breakout filter, default).
    """
    n      = len(df)
    opens  = df['open'].values
    highs  = df['high'].values
    lows   = df['low'].values
    closes = df['close'].values

    bar_rh = np.full(n, np.nan)
    bar_rl = np.full(n, np.nan)
    bar_ep = np.full(n, -1, dtype=int)

    episodes  = []
    ep_id     = 0
    ep_start  = start_idx
    rh        = highs[start_idx]
    rl        = lows[start_idx]
    direction = 'initial'
    gap_open  = False
    prev_rh = prev_rl = None

    bar_rh[start_idx] = rh
    bar_rl[start_idx] = rl
    bar_ep[start_idx] = ep_id

    # --- pending breakout state ---
    pend          = False   # waiting for confirmation?
    pend_dir      = None    # 'up' | 'down'
    pend_idx      = None    # index of first breakout bar (would-be range setter)
    pend_rh       = None    # would-be new range high
    pend_rl       = None    # would-be new range low
    pend_gap      = False
    pend_prev_rh  = None    # rh before pending (confirmation threshold)
    pend_prev_rl  = None
    pend_seen     = 0       # confirming closes accumulated so far
    pend_extra    = []      # indices of accumulation bars (after first breakout bar)

    def _make_episode(end_idx):
        ep_bars = end_idx - ep_start + 1
        episodes.append({
            'episode_id':   ep_id,
            'start_idx':    ep_start,
            'end_idx':      end_idx,
            'start_ts':     df.index[ep_start],
            'end_ts':       df.index[end_idx],
            'bar_count':    ep_bars,
            'is_transient': ep_bars < min_range_bars,
            'range_high':   rh,
            'range_low':    rl,
            'range_mid':    (rh + rl) / 2,
            'direction':    direction,
            'gap_open':     gap_open,
        })

    def _commit():
        """Finalise pending breakout as a genuine range setter."""
        nonlocal pend, ep_id, ep_start, rh, rl, direction, gap_open, prev_rh, prev_rl
        nonlocal pend_seen, pend_extra

        _make_episode(pend_idx - 1)   # close out the previous episode

        prev_rh, prev_rl = pend_prev_rh, pend_prev_rl
        ep_id    += 1
        ep_start  = pend_idx
        rh, rl    = pend_rh, pend_rl
        direction = pend_dir
        gap_open  = pend_gap

        # Retroactively fix the first breakout bar
        bar_rh[pend_idx] = rh
        bar_rl[pend_idx] = rl
        bar_ep[pend_idx] = ep_id

        # Apply accumulation bars as wick extensions within the new episode
        for idx in pend_extra:
            if highs[idx] > rh: rh = highs[idx]
            if lows[idx]  < rl: rl = lows[idx]
            bar_rh[idx] = rh
            bar_rl[idx] = rl
            bar_ep[idx] = ep_id

        pend = False
        pend_seen = 0
        pend_extra.clear()

    def _deny():
        """Absorb all pending bars into the current range (false breakout)."""
        nonlocal pend, rh, rl, pend_seen

        for idx in [pend_idx] + pend_extra:
            if highs[idx] > rh: rh = highs[idx]
            if lows[idx]  < rl: rl = lows[idx]
            bar_rh[idx] = rh
            bar_rl[idx] = rl
            # bar_ep already set to current ep_id

        pend = False
        pend_seen = 0
        pend_extra.clear()

    def _enter_pending(i, broke_up):
        nonlocal pend, pend_dir, pend_idx, pend_rh, pend_rl, pend_gap
        nonlocal pend_prev_rh, pend_prev_rl, pend_seen

        pend         = True
        pend_idx     = i
        pend_seen    = 0
        pend_prev_rh = rh
        pend_prev_rl = rl

        if broke_up:
            pend_dir = 'up'
            pend_gap = opens[i] > rh
            pend_rh  = highs[i]
            pend_rl  = rh if pend_gap else lows[i]
        else:
            pend_dir = 'down'
            pend_gap = opens[i] < rl
            pend_rl  = lows[i]
            pend_rh  = rl if pend_gap else highs[i]

        # Show as wick extension of current range until confirmed
        bar_rh[i] = max(rh, highs[i])
        bar_rl[i] = min(rl, lows[i])
        bar_ep[i] = ep_id

    def _commit_immediate(i, broke_up):
        """No-confirmation path (breakout_confirm == 0)."""
        nonlocal ep_id, ep_start, rh, rl, direction, gap_open, prev_rh, prev_rl

        _make_episode(i - 1)

        if broke_up:
            new_gap = opens[i] > rh
            new_rh  = highs[i]
            new_rl  = rh if new_gap else lows[i]
            new_dir = 'up'
        else:
            new_gap = opens[i] < rl
            new_rl  = lows[i]
            new_rh  = rl if new_gap else highs[i]
            new_dir = 'down'

        prev_rh, prev_rl = rh, rl
        ep_id    += 1
        ep_start  = i
        rh, rl    = new_rh, new_rl
        direction = new_dir
        gap_open  = new_gap

        bar_rh[i] = rh
        bar_rl[i] = rl
        bar_ep[i] = ep_id

    # --- main loop ---
    for i in range(start_idx + 1, n):
        h, l, c = highs[i], lows[i], closes[i]

        if pend:
            still = ((pend_dir == 'up'   and c > pend_prev_rh) or
                     (pend_dir == 'down' and c < pend_prev_rl))
            if still:
                pend_seen += 1
                if pend_seen >= breakout_confirm:
                    _commit()
                    # fall through: process bar i against the new range
                else:
                    # still accumulating — store bar, extend pending bounds
                    pend_extra.append(i)
                    if pend_dir == 'up':
                        pend_rh = max(pend_rh, h)
                    else:
                        pend_rl = min(pend_rl, l)
                    bar_rh[i] = max(rh, h)
                    bar_rl[i] = min(rl, l)
                    bar_ep[i] = ep_id
                    continue
            else:
                _deny()
                # fall through: process bar i against the expanded range

        # Normal bar processing (pend is False here)
        broke_up   = c > rh
        broke_down = c < rl

        if broke_up or broke_down:
            if breakout_confirm == 0:
                _commit_immediate(i, broke_up)
            else:
                _enter_pending(i, broke_up)
        else:
            if h > rh: rh = h
            if l < rl: rl = l
            bar_rh[i] = rh
            bar_rl[i] = rl
            bar_ep[i] = ep_id

    # Unconfirmed pending at end of data → absorb conservatively
    if pend:
        _deny()

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

    spread = bar_rh - bar_rl
    with np.errstate(invalid='ignore', divide='ignore'):
        pct = np.where(spread > 0, 100 * (closes - bar_rl) / spread, 50.0)

    result = df.copy()
    result['range_high']         = bar_rh
    result['range_low']          = bar_rl
    result['range_mid']          = (bar_rh + bar_rl) / 2
    result['episode_id']         = bar_ep
    result['close_pct_in_range'] = pct

    return result, episodes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_start_idx(df: pd.DataFrame, start_date_str: str, timeframe) -> int:
    target = (pd.Timestamp(start_date_str).normalize()
              if timeframe == 'daily' else pd.Timestamp(start_date_str))
    idx = df.index.searchsorted(target)
    if idx >= len(df):
        raise ValueError(f'--start-date {start_date_str} is beyond the data range.')
    return int(idx)


def _is_intraday(timeframe) -> bool:
    return timeframe != 'daily'


def _x_ext(timeframe):
    return (pd.Timedelta(days=1) if timeframe == 'daily'
            else pd.Timedelta(minutes=int(timeframe)))


def _episode_state(ep, hybrid_mode, min_range_bars) -> str:
    """Return 'established', 'trending', or 'transient'."""
    if ep['bar_count'] < min_range_bars:
        return 'transient'
    if hybrid_mode == 'a' and not ep.get('is_adx_ranging', True):
        return 'trending'
    return 'established'


_MODE_LABEL = {
    'none': 'PA only',
    'a':    'Hybrid A — ADX gate',
}

# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------

def plot(result: pd.DataFrame, episodes: list, timeframe,
         from_date, to_date=None, min_range_bars: int = 5,
         hybrid_mode: str = 'none', adx_threshold: float = 20.0,
         label: str = '', no_browser: bool = False):

    view = result.loc[from_date:to_date] if to_date else result.loc[from_date:]
    if view.empty:
        print(f'No data in window {from_date}–{to_date}')
        return None

    view_start  = view.index[0]
    view_end    = view.index[-1]
    visible_eps = [ep for ep in episodes
                   if ep['end_ts'] >= view_start and ep['start_ts'] <= view_end]

    fig = make_subplots(rows=1, cols=1)

    fig.add_trace(go.Candlestick(
        x=view.index,
        open=view['open'], high=view['high'],
        low=view['low'],   close=view['close'],
        name='Nifty',
        increasing_line_color='#26a69a',
        decreasing_line_color='#ef5350',
        whiskerwidth=0.3,
    ))

    ext      = _x_ext(timeframe)
    last_ep  = episodes[-1]

    for ep in visible_eps:
        is_last = ep['episode_id'] == last_ep['episode_id']
        x0, x1  = ep['start_ts'], ep['end_ts'] + ext if is_last else ep['end_ts']
        y0, y1  = ep['range_low'], ep['range_high']
        mid     = ep['range_mid']
        state   = _episode_state(ep, hybrid_mode, min_range_bars)

        if state == 'established':
            fill  = _ESTABLISHED_FILL
            line  = dict(color=_ESTABLISHED_LINE, width=1)
            mline = dict(color=_ESTABLISHED_MID,  width=1, dash='dot')
        elif state == 'trending':
            fill  = _TRENDING_FILL
            line  = dict(color=_TRENDING_LINE, width=1, dash='dash')
            mline = dict(color=_TRENDING_MID,  width=1, dash='dot')
        else:
            fill  = _TRANSIENT_FILL
            line  = dict(color=_TRANSIENT_LINE, width=1, dash='dash')
            mline = dict(color=_TRANSIENT_MID,  width=1, dash='dot')

        fig.add_shape(type='rect', xref='x', yref='y',
                      x0=x0, x1=x1, y0=y0, y1=y1,
                      fillcolor=fill, line=line, layer='below')
        fig.add_shape(type='line', xref='x', yref='y',
                      x0=x0, x1=x1, y0=mid, y1=mid,
                      line=mline, layer='below')

    # Range setter markers
    up_x, up_y, up_txt = [], [], []
    dn_x, dn_y, dn_txt = [], [], []
    in_x, in_y         = [], []

    for ep in visible_eps:
        ts = ep['start_ts']
        if ts not in view.index:
            continue
        bar  = view.loc[ts]
        d    = ep['direction']
        gtag = '  GAP' if ep['gap_open'] else ''
        if d == 'up':
            up_x.append(ts); up_y.append(bar['low'] * 0.9995)
            up_txt.append(f'↑{gtag}')
        elif d == 'down':
            dn_x.append(ts); dn_y.append(bar['high'] * 1.0005)
            dn_txt.append(f'↓{gtag}')
        else:
            in_x.append(ts); in_y.append(bar['low'] * 0.9995)

    if up_x:
        fig.add_trace(go.Scatter(
            x=up_x, y=up_y, mode='markers+text',
            marker=dict(symbol='triangle-up', color=_SETTER_UP_COLOR, size=10,
                        line=dict(width=1, color='#222')),
            text=up_txt, textposition='bottom center',
            textfont=dict(size=9, color='#aaa'),
            name='Setter ↑', showlegend=False,
        ))
    if dn_x:
        fig.add_trace(go.Scatter(
            x=dn_x, y=dn_y, mode='markers+text',
            marker=dict(symbol='triangle-down', color=_SETTER_DN_COLOR, size=10,
                        line=dict(width=1, color='#222')),
            text=dn_txt, textposition='top center',
            textfont=dict(size=9, color='#aaa'),
            name='Setter ↓', showlegend=False,
        ))
    if in_x:
        fig.add_trace(go.Scatter(
            x=in_x, y=in_y, mode='markers',
            marker=dict(symbol='diamond', color=_SETTER_IN_COLOR, size=10,
                        line=dict(width=1, color='#222')),
            name='Initial setter', showlegend=False,
        ))

    tf_label   = timeframe_label(timeframe)
    mode_label = _MODE_LABEL.get(hybrid_mode, hybrid_mode)
    title = (f'Nifty PA Range Detection ({tf_label}) [{mode_label}]'
             f'{" — " + label if label else ""}')
    fig.update_layout(
        title=title,
        template='plotly_dark',
        xaxis_rangeslider_visible=False,
        xaxis_title='', yaxis_title='Price',
        height=700,
        margin=dict(l=60, r=40, t=60, b=40),
        legend=dict(orientation='h', yanchor='bottom', y=1.01, xanchor='left', x=0),
    )

    if _is_intraday(timeframe):
        fig.update_xaxes(rangebreaks=[
            dict(bounds=[15.5, 9.25], pattern='hour'),
            dict(bounds=['sat', 'mon']),
        ])

    # Bottom annotation — last episode summary
    last_close = result['close'].iloc[-1]
    pct        = result['close_pct_in_range'].iloc[-1]
    state      = _episode_state(last_ep, hybrid_mode, min_range_bars).upper()
    adx_str    = (f' | ADX {last_ep["avg_adx"]:.1f}'
                  if 'avg_adx' in last_ep and not np.isnan(last_ep['avg_adx'])
                  else '')
    ann = (f'Last range: {last_ep["range_low"]:.0f}–{last_ep["range_high"]:.0f}'
           f' | mid {last_ep["range_mid"]:.0f}'
           f' | close {last_close:.0f} ({pct:.1f}% in range)'
           f' | {last_ep["bar_count"]} bars{adx_str} [{state}]')
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
        row = {
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
            'width_pct':     round(100 * (ep['range_high'] - ep['range_low'])
                                   / ep['range_mid'], 3),
        }
        if 'avg_adx' in ep:
            row['avg_adx']        = ep['avg_adx'] if not np.isnan(ep['avg_adx']) else ''
            row['is_adx_ranging'] = ep['is_adx_ranging']
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f'Episodes CSV: {path}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Price-action range detector for Nifty (with optional ADX hybrid modes).')
    parser.add_argument('--timeframe',      default='daily',
                        help='daily | integer minutes (e.g. 75, 15, 5, 3)')
    parser.add_argument('--start-date',     required=True,
                        help='Initial range setter candle: YYYY-MM-DD or "YYYY-MM-DD HH:MM"')
    parser.add_argument('--min-range-bars',    type=int, default=5,
                        help='Min bars for an established range (default: 5)')
    parser.add_argument('--breakout-confirm', type=int, default=1,
                        help='Confirmation bars required after a breakout close (default: 1, 0 = immediate)')
    parser.add_argument('--hybrid',         default='none', choices=['none', 'a'],
                        help='Hybrid mode: none = pure PA, a = ADX hard gate (default: none)')
    parser.add_argument('--adx-threshold',  type=float, default=20.0,
                        help='ADX ranging threshold used in hybrid modes (default: 20)')
    parser.add_argument('--adx-period',     type=int, default=14,
                        help='Wilder ADX period (default: 14)')
    parser.add_argument('--months',         type=int, default=None,
                        help='Months to display in single-chart mode (default: all from start)')
    parser.add_argument('--all',            action='store_true',
                        help='Generate one chart per calendar year + episodes CSV')
    parser.add_argument('--years',          type=int, nargs='+',
                        help='Restrict --all to specific years')
    parser.add_argument('--tag',            default='',
                        help='Suffix added to output filenames')
    parser.add_argument('--no-browser',     action='store_true',
                        help='Save HTML without opening in browser')
    args = parser.parse_args()

    tf = args.timeframe
    if tf != 'daily':
        try:
            tf = int(tf)
        except ValueError:
            print(f'Invalid --timeframe "{tf}". Use "daily" or an integer (e.g. 75).')
            sys.exit(1)

    tf_label = timeframe_label(tf)
    tag      = f'_{args.tag}' if args.tag else ''
    hybrid   = args.hybrid

    print(f'Loading {tf_label} data…')
    df = load_data(tf)
    print(f'  {len(df)} bars  {df.index[0].date()} → {df.index[-1].date()}')

    start_idx = _find_start_idx(df, args.start_date, tf)
    print(f'  Start candle: {df.index[start_idx]}  (index {start_idx})')

    print('Computing PA ranges…')
    result, episodes = compute_pa_ranges(df, start_idx, args.min_range_bars,
                                          args.breakout_confirm)
    est   = sum(1 for e in episodes if not e['is_transient'])
    trans = len(episodes) - est
    print(f'  {len(episodes)} episodes — {est} established, {trans} transient (PA)')

    if hybrid != 'none':
        print(f'Computing ADX (period={args.adx_period}, threshold={args.adx_threshold})…')
        result['adx'] = compute_adx(df, args.adx_period)
        episodes = annotate_adx(result, episodes, args.adx_threshold)
        n_trending = sum(1 for e in episodes
                         if not e['is_transient'] and not e.get('is_adx_ranging', True))
        print(f'  {n_trending} established PA episodes downgraded to trending by ADX')

    csv_path = os.path.join(OUTPUT_DIR, f'range_episodes_pa_{tf_label}{tag}.csv')
    export_episodes_csv(episodes, csv_path)

    plot_kwargs = dict(min_range_bars=args.min_range_bars,
                       hybrid_mode=hybrid,
                       adx_threshold=args.adx_threshold)

    if args.all:
        years = args.years or sorted({df.index[i].year
                                      for i in range(start_idx, len(df))})
        for year in years:
            from_date = pd.Timestamp(f'{year}-01-01')
            to_date   = pd.Timestamp(f'{year}-12-31')
            fig = plot(result, episodes, tf, from_date, to_date,
                       label=str(year), no_browser=True, **plot_kwargs)
            if fig is None:
                continue
            fname = f'range_chart_pa_{tf_label}_{year}{tag}.html'
            _save_and_show(fig, os.path.join(OUTPUT_DIR, fname), args.no_browser)
    else:
        from_date = (df.index[-1] - pd.DateOffset(months=args.months)
                     if args.months else df.index[start_idx])
        fig = plot(result, episodes, tf, from_date, None,
                   no_browser=args.no_browser, **plot_kwargs)
        if fig:
            fname = f'range_chart_pa_{tf_label}{tag}.html'
            _save_and_show(fig, os.path.join(OUTPUT_DIR, fname), args.no_browser)


if __name__ == '__main__':
    main()
