"""
range_detector.py — Nifty range detection prototype.

Uses official daily Nifty OHLC (AngelOne nifty_daily.csv) to detect whether
the market is range-bound (ADX < threshold) or trending, and computes adaptive
range bounds using swing highs/lows anchored at the last trend breakout.

Usage:
    python range_detector.py [--months 3] [--adx-period 14] [--adx-threshold 20]

Output:
    Interactive HTML chart (opens in browser) + printed summary table.
"""

import os
import sys
import argparse
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT     = os.path.dirname(os.path.dirname(BASE_DIR))
NIFTY_DAILY_FILE = os.path.join(REPO_ROOT, 'data_pipeline', 'data', 'indices', 'nifty_daily.csv')
OUTPUT_DIR    = os.path.join(BASE_DIR, 'outputs')
OUTPUT_HTML   = os.path.join(OUTPUT_DIR, 'range_chart.html')


# ---------------------------------------------------------------------------
# Data loading & resampling
# ---------------------------------------------------------------------------

def load_daily(months_back=None) -> pd.DataFrame:
    df = pd.read_csv(NIFTY_DAILY_FILE)
    df['time_stamp'] = pd.to_datetime(df['time_stamp'])
    df = df.set_index('time_stamp').sort_index()
    df = df[df['close'].notna() & (df['close'] > 0)].copy()

    if months_back is None:
        return df, df.index[0]

    cutoff = df.index[-1] - pd.DateOffset(months=months_back)
    # Give ADX enough warm-up data — pull extra before the cutoff
    warmup_cutoff = cutoff - pd.DateOffset(months=2)
    df = df[df.index >= warmup_cutoff]
    return df, cutoff


# ---------------------------------------------------------------------------
# ADX calculation (Wilder's smoothing, no external TA library needed)
# ---------------------------------------------------------------------------

def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    n     = period
    high  = df['high'].values
    low   = df['low'].values
    close = df['close'].values
    size  = len(df)

    tr    = np.full(size, np.nan)
    dmp   = np.full(size, np.nan)   # +DM
    dmm   = np.full(size, np.nan)   # -DM

    for i in range(1, size):
        hl   = high[i] - low[i]
        hpc  = abs(high[i] - close[i-1])
        lpc  = abs(low[i]  - close[i-1])
        tr[i] = max(hl, hpc, lpc)

        up   = high[i] - high[i-1]
        down = low[i-1] - low[i]
        dmp[i] = up   if (up > down and up > 0)   else 0.0
        dmm[i] = down if (down > up and down > 0) else 0.0

    # Wilder smoothing for TR, +DM, -DM — initial seed is SUM (Wilder's convention)
    # This keeps the scale at ~n * bar_average, which cancels in DI ratio
    atr  = np.full(size, np.nan)
    spdm = np.full(size, np.nan)
    smdm = np.full(size, np.nan)

    atr[n]  = np.nansum(tr[1:n+1])
    spdm[n] = np.nansum(dmp[1:n+1])
    smdm[n] = np.nansum(dmm[1:n+1])
    for i in range(n+1, size):
        atr[i]  = atr[i-1]  - atr[i-1]  / n + tr[i]
        spdm[i] = spdm[i-1] - spdm[i-1] / n + dmp[i]
        smdm[i] = smdm[i-1] - smdm[i-1] / n + dmm[i]

    with np.errstate(divide='ignore', invalid='ignore'):
        pdi = np.where(atr > 0, 100 * spdm / atr, 0.0)
        mdi = np.where(atr > 0, 100 * smdm / atr, 0.0)
        sum_di = pdi + mdi
        dx = np.where(sum_di > 0, 100 * np.abs(pdi - mdi) / sum_di, 0.0)

    # ADX: Wilder smoothing of DX — seed with MEAN (DX is already 0-100 scaled)
    adx = np.full(size, np.nan)
    first_dx_idx = n  # DX is valid from index n
    if first_dx_idx + n < size:
        adx[first_dx_idx + n] = np.mean(dx[first_dx_idx: first_dx_idx + n])
        for i in range(first_dx_idx + n + 1, size):
            adx[i] = (adx[i-1] * (n - 1) + dx[i]) / n

    return pd.Series(adx, index=df.index)


# ---------------------------------------------------------------------------
# Swing high / low detection (Williams Fractal, 2-bar lookahead/lookback)
# ---------------------------------------------------------------------------

def find_swings(df: pd.DataFrame, strength: int = 2) -> tuple[pd.Series, pd.Series]:
    """
    Returns swing_high and swing_low boolean Series.
    A swing high: candle N high > all highs within ±strength bars.
    A swing low:  candle N low  < all lows  within ±strength bars.
    """
    h = df['high']
    l = df['low']
    n = len(df)
    sh = pd.Series(False, index=df.index)
    sl = pd.Series(False, index=df.index)
    for i in range(strength, n - strength):
        window_h = h.iloc[i-strength : i+strength+1]
        window_l = l.iloc[i-strength : i+strength+1]
        if h.iloc[i] == window_h.max():
            sh.iloc[i] = True
        if l.iloc[i] == window_l.min():
            sl.iloc[i] = True
    return sh, sl


# ---------------------------------------------------------------------------
# Adaptive range bounds
#
# For each day, we compute:
#   range_high = most recent swing high (within the current ranging episode)
#   range_low  = most recent swing low  (within the current ranging episode)
#   range_mid  = midpoint
#   range_episode_start = date ADX last crossed below threshold
#
# When ADX rises above threshold, we're trending — range_high/low = NaN.
# ---------------------------------------------------------------------------

def compute_ranges(daily: pd.DataFrame, adx: pd.Series, sh: pd.Series,
                   sl: pd.Series, adx_threshold: float,
                   breakout_tolerance: float = 0.002,
                   lookback_at_start: int = 4) -> pd.DataFrame:
    """
    Identify ranging episodes with two termination triggers:
      1. ADX rises above threshold  (trending breakout)
      2. Daily close moves outside range bounds ± breakout_tolerance
         (price breakout — starts a new episode if ADX is still low)

    At episode start, look back `lookback_at_start` bars to capture the
    range formation that precedes the ADX confirmation lag.
    """
    result = daily.copy()
    result['adx']         = adx
    result['is_ranging']  = adx < adx_threshold
    result['swing_high']  = np.where(sh, daily['high'], np.nan)
    result['swing_low']   = np.where(sl, daily['low'],  np.nan)
    result['range_high']  = np.nan
    result['range_low']   = np.nan
    result['episode_start'] = pd.Series(pd.NaT, index=result.index, dtype='datetime64[ns]')

    highs  = result['high'].values
    lows   = result['low'].values
    closes = result['close'].values
    dates  = result.index

    in_range      = False
    episode_start = None
    ep_high       = -np.inf
    ep_low        =  np.inf

    def _start_episode(i, lb):
        """Anchor episode bounds to the lookback window starting at lb."""
        h = float(np.max(highs[lb : i + 1]))
        l = float(np.min(lows[lb  : i + 1]))
        return dates[lb], h, l

    for i in range(len(result)):
        idx     = dates[i]
        ranging = bool(result['is_ranging'].iloc[i])

        if not ranging:
            in_range      = False
            episode_start = None
            ep_high       = -np.inf
            ep_low        =  np.inf
            continue

        # --- ADX says ranging ---
        if not in_range:
            lb = max(0, i - lookback_at_start)
            episode_start, ep_high, ep_low = _start_episode(i, lb)
            in_range = True
        else:
            close = closes[i]
            broke_low  = close < ep_low  * (1 - breakout_tolerance)
            broke_high = close > ep_high * (1 + breakout_tolerance)

            if broke_low or broke_high:
                # Price breakout within a ranging ADX episode → new range starts at this bar
                episode_start, ep_high, ep_low = _start_episode(i, i)
            else:
                # Expand bounds only on confirmed swing highs/lows
                if not np.isnan(result['swing_high'].iloc[i]):
                    ep_high = max(ep_high, highs[i])
                if not np.isnan(result['swing_low'].iloc[i]):
                    ep_low  = min(ep_low,  lows[i])

        result.at[idx, 'range_high']    = ep_high
        result.at[idx, 'range_low']     = ep_low
        result.at[idx, 'episode_start'] = episode_start

    result['range_mid'] = (result['range_high'] + result['range_low']) / 2
    result['range_pct'] = (result['close'] - result['range_low']) / (
        result['range_high'] - result['range_low']
    )
    return result


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def plot(result: pd.DataFrame, from_date: pd.Timestamp, adx_threshold: float,
         to_date: pd.Timestamp = None, label: str = ''):
    to_date = to_date if to_date is not None else result.index[-1]
    display = result[(result.index >= from_date) & (result.index <= to_date)].copy()

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.72, 0.28],
        vertical_spacing=0.04,
        subplot_titles=('Nifty Daily — Range Detection', 'ADX'),
    )

    # --- Candlesticks ---
    fig.add_trace(go.Candlestick(
        x=display.index,
        open=display['open'], high=display['high'],
        low=display['low'],   close=display['close'],
        name='Nifty',
        increasing_line_color='#26a69a',
        decreasing_line_color='#ef5350',
        showlegend=False,
    ), row=1, col=1)

    # --- Range high / low bands (filled area) ---
    # Group by contiguous ranging episodes to draw separate filled regions
    ranging_mask = display['range_high'].notna()
    in_block = False
    block_x, block_h, block_l = [], [], []

    def _flush_block(fig, bx, bh, bl):
        if not bx:
            return
        # Forward + backward traces for fill
        fig.add_trace(go.Scatter(
            x=bx + bx[::-1],
            y=bh + bl[::-1],
            fill='toself',
            fillcolor='rgba(100, 160, 255, 0.12)',
            line=dict(width=0),
            showlegend=False,
            hoverinfo='skip',
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=bx, y=bh,
            line=dict(color='rgba(100, 160, 255, 0.6)', width=1.5, dash='dot'),
            mode='lines', name='Range High', showlegend=False,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=bx, y=bl,
            line=dict(color='rgba(100, 160, 255, 0.6)', width=1.5, dash='dot'),
            mode='lines', name='Range Low', showlegend=False,
        ), row=1, col=1)

    for idx, row in display.iterrows():
        if ranging_mask[idx]:
            block_x.append(idx)
            block_h.append(row['range_high'])
            block_l.append(row['range_low'])
            in_block = True
        else:
            if in_block:
                _flush_block(fig, block_x, block_h, block_l)
                block_x, block_h, block_l = [], [], []
            in_block = False
    _flush_block(fig, block_x, block_h, block_l)

    # --- Range midline ---
    mid = display[display['range_mid'].notna()]
    if not mid.empty:
        # Draw midlines per episode (gaps where trending)
        for ep_start, grp in mid.groupby('episode_start'):
            fig.add_trace(go.Scatter(
                x=grp.index, y=grp['range_mid'],
                line=dict(color='rgba(255, 200, 50, 0.85)', width=1.5, dash='dash'),
                mode='lines',
                name=f'Range Mid ({ep_start.strftime("%d %b")})',
                showlegend=True,
            ), row=1, col=1)

    # --- Swing high / low markers ---
    sh_pts = display[display['swing_high'].notna()]
    sl_pts = display[display['swing_low'].notna()]
    fig.add_trace(go.Scatter(
        x=sh_pts.index, y=sh_pts['high'] * 1.002,
        mode='markers',
        marker=dict(symbol='triangle-down', color='rgba(239,83,80,0.8)', size=8),
        name='Swing High',
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=sl_pts.index, y=sl_pts['low'] * 0.998,
        mode='markers',
        marker=dict(symbol='triangle-up', color='rgba(38,166,154,0.8)', size=8),
        name='Swing Low',
    ), row=1, col=1)

    # --- ADX ---
    fig.add_trace(go.Scatter(
        x=display.index, y=display['adx'],
        line=dict(color='#ab47bc', width=1.8),
        name='ADX (14)',
    ), row=2, col=1)
    fig.add_hline(
        y=adx_threshold,
        line=dict(color='rgba(255,255,255,0.4)', width=1, dash='dash'),
        annotation_text=f'ADX {adx_threshold}',
        annotation_font_size=11,
        row=2, col=1,
    )

    # --- Layout ---
    fig.update_layout(
        template='plotly_dark',
        title=dict(
            text=f'Nifty Range Detection — {label}  |  ADX threshold: {adx_threshold}',
            font=dict(size=15),
        ),
        xaxis_rangeslider_visible=False,
        height=800,
        legend=dict(orientation='h', yanchor='bottom', y=1.01, xanchor='right', x=1),
        margin=dict(l=60, r=40, t=80, b=40),
    )
    fig.update_yaxes(title_text='Nifty', row=1, col=1)
    fig.update_yaxes(title_text='ADX',   row=2, col=1)
    fig.update_xaxes(showgrid=True, gridcolor='rgba(255,255,255,0.06)')
    fig.update_yaxes(showgrid=True, gridcolor='rgba(255,255,255,0.06)')

    return fig


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary(result: pd.DataFrame, from_date: pd.Timestamp, adx_threshold: float,
                  to_date: pd.Timestamp = None):
    to_date = to_date if to_date is not None else result.index[-1]
    display = result[(result.index >= from_date) & (result.index <= to_date)].copy()
    ranging = display[display['range_high'].notna()]

    print(f"\n{'='*70}")
    print(f"  NIFTY RANGE DETECTION SUMMARY  |  ADX threshold: {adx_threshold}")
    print(f"{'='*70}")

    if ranging.empty:
        print("  No ranging episodes detected in the period.")
        return

    episodes = ranging.groupby('episode_start')
    for ep_start, grp in episodes:
        ep_end   = grp.index[-1]
        days     = len(grp)
        hi       = grp['range_high'].iloc[-1]
        lo       = grp['range_low'].iloc[-1]
        mid      = grp['range_mid'].iloc[-1]
        width_pct = (hi - lo) / mid * 100
        last_close = grp['close'].iloc[-1]
        pos_pct  = (last_close - lo) / (hi - lo) * 100 if (hi - lo) > 0 else 50
        print(f"\n  Episode: {ep_start.strftime('%d %b %Y')} → {ep_end.strftime('%d %b %Y')}  ({days} days)")
        print(f"    High  : {hi:,.1f}")
        print(f"    Low   : {lo:,.1f}")
        print(f"    Mid   : {mid:,.1f}")
        print(f"    Width : {hi - lo:,.0f} pts  ({width_pct:.1f}%)")
        print(f"    Last close {last_close:,.1f} — {pos_pct:.0f}% of range (0=low, 100=high)")
        zone = "MIDDLE ✓" if 33 < pos_pct < 67 else ("UPPER BOUNDARY ⚠" if pos_pct >= 67 else "LOWER BOUNDARY ⚠")
        print(f"    Athena entry zone: {zone}")

    # Latest state
    last = display.iloc[-1]
    print(f"\n{'─'*70}")
    print(f"  Latest ({display.index[-1].strftime('%d %b %Y')}):")
    print(f"    ADX : {last['adx']:.1f}  →  {'RANGING' if last['is_ranging'] else 'TRENDING'}")
    if not np.isnan(last.get('range_high', np.nan)):
        print(f"    Range in effect: {last['range_low']:,.1f} – {last['range_high']:,.1f}  (mid {last['range_mid']:,.1f})")
    print(f"{'='*70}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Nifty range detector')
    parser.add_argument('--months',        type=int,   default=3,  help='Months to display (default 3)')
    parser.add_argument('--adx-period',    type=int,   default=14, help='ADX period (default 14)')
    parser.add_argument('--adx-threshold', type=float, default=20, help='ADX ranging threshold (default 20)')
    parser.add_argument('--swing-strength',type=int,   default=3,  help='Swing detection strength in bars (default 3)')
    parser.add_argument('--no-browser',    action='store_true',    help='Save HTML but do not open browser')
    parser.add_argument('--all',           action='store_true',    help='Plot all available data, one chart per year')
    args = parser.parse_args()

    print("Loading Nifty daily data...")
    months = None if args.all else args.months
    daily, cutoff = load_daily(months)
    print(f"Daily candles loaded: {len(daily)}  |  Display from: {cutoff.date()}")

    print("Computing ADX...")
    adx = compute_adx(daily, period=args.adx_period)

    print("Detecting swings...")
    sh, sl = find_swings(daily, strength=args.swing_strength)

    print("Computing range bounds...")
    result = compute_ranges(daily, adx, sh, sl, adx_threshold=args.adx_threshold)

    if args.all:
        import webbrowser
        years = sorted(result.index.year.unique())
        generated = []
        for year in years:
            yr_data = result[result.index.year == year]
            from_date = yr_data.index[0]
            to_date   = yr_data.index[-1]
            label     = str(year)
            print(f"\n--- {year} ---")
            print_summary(result, from_date, args.adx_threshold, to_date=to_date)
            fig  = plot(result, from_date, args.adx_threshold, to_date=to_date, label=label)
            path = os.path.join(OUTPUT_DIR, f'range_chart_{year}.html')
            fig.write_html(path, auto_open=False)
            print(f"Chart saved → {path}")
            generated.append(path)
        if not args.no_browser:
            for path in generated:
                webbrowser.open(f'file://{os.path.abspath(path)}')
    else:
        print_summary(result, cutoff, args.adx_threshold)
        print("Building chart...")
        fig = plot(result, cutoff, args.adx_threshold, label=f'last {args.months} months')
        fig.write_html(OUTPUT_HTML, auto_open=not args.no_browser)
        print(f"Chart saved → {OUTPUT_HTML}")
        if not args.no_browser:
            print("Opening in browser...")


if __name__ == '__main__':
    main()
