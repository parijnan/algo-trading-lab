"""
oi_engine.py — Core OI feature computation from sparse per-strike tick-on-trade CSVs.

Fully vectorised with numpy broadcasting. No Python loops over bars.

Features computed per bar:
  ce_wall_strike, ce_wall_oi, ce_wall_dist_pts, ce_wall_dist_pct
  pe_wall_strike, pe_wall_oi, pe_wall_dist_pts, pe_wall_dist_pct
  max_pain_strike, max_pain_dist_pts
  pcr_near, pcr_broad, total_oi, atm_strike, spot

Usage:
    from research.oi_analysis.oi_engine import build_oi_profile, oi_at_strike
"""

import os
import re
import glob
import warnings

import numpy as np
import pandas as pd

_OPEN  = '09:15'
_CLOSE = '15:30'


# ─── file helpers ─────────────────────────────────────────────────────────────

def _parse_strike_side(filename):
    m = re.match(r'^(\d+)(ce|pe)\.csv$', filename.lower())
    if not m:
        return None, None
    return int(m.group(1)), m.group(2)


def _oi_col(df):
    for c in ('open_interest', 'oi'):
        if c in df.columns:
            return c
    return None


def _ts_col(df):
    for c in ('datetime', 'time_stamp'):
        if c in df.columns:
            return c
    return None


def _week_dates(expiry_date_str, lookback_days=10):
    expiry = pd.Timestamp(expiry_date_str).date()
    dates = pd.date_range(end=expiry_date_str, periods=lookback_days, freq='D')
    return set(d.date() for d in dates if d.date() <= expiry)


def _minute_grid(dates):
    parts = [
        pd.date_range(start=f'{d} {_OPEN}', end=f'{d} {_CLOSE}', freq='1min')
        for d in sorted(dates)
    ]
    return parts[0].append(parts[1:]) if len(parts) > 1 else parts[0]


def _load_all_strikes(options_dir, week_dates):
    """
    Return (ce_matrix, pe_matrix, ce_strikes, pe_strikes, grid)
    where matrices have shape (n_grid_minutes, n_strikes), forward-filled.
    """
    files = sorted(glob.glob(os.path.join(options_dir, '*.csv')))
    grid  = _minute_grid(week_dates)

    ce_raw = {}  # strike → Series on grid
    pe_raw = {}

    for fpath in files:
        strike, side = _parse_strike_side(os.path.basename(fpath))
        if strike is None:
            continue

        raw = pd.read_csv(fpath)
        ts_c = _ts_col(raw)
        oi_c = _oi_col(raw)
        if ts_c is None or oi_c is None:
            continue

        raw[ts_c] = pd.to_datetime(raw[ts_c], utc=False)
        if raw[ts_c].dt.tz is not None:
            raw[ts_c] = raw[ts_c].dt.tz_localize(None)

        raw = raw[raw[ts_c].dt.date.isin(week_dates)]
        if raw.empty:
            continue

        oi_1m = (
            raw.set_index(ts_c)[oi_c]
            .resample('1min').last()
            .reindex(grid).ffill().fillna(0).clip(lower=0)
        )
        (ce_raw if side == 'ce' else pe_raw)[strike] = oi_1m

    def _to_matrix(d):
        if not d:
            return np.empty((len(grid), 0)), np.array([])
        strikes  = np.array(sorted(d.keys()), dtype=float)
        matrix   = np.column_stack([d[s].values for s in sorted(d.keys())])
        return matrix, strikes

    ce_mat, ce_st = _to_matrix(ce_raw)
    pe_mat, pe_st = _to_matrix(pe_raw)
    return ce_mat, pe_mat, ce_st, pe_st, grid


# ─── vectorised feature builders ──────────────────────────────────────────────

def _wall_vec(oi_mat, strikes, spot_arr, direction, min_pct, max_pct):
    """
    Vectorised OI wall for all bars simultaneously.
    direction: 'ce' → search above spot; 'pe' → search below spot
    Returns (wall_strike, wall_oi) each shape (n_bars,).
    """
    if oi_mat.shape[1] == 0:
        nan = np.full(len(spot_arr), np.nan)
        return nan, nan

    sp = spot_arr[:, None]              # (n_bars, 1)
    st = strikes[None, :]               # (1, n_strikes)

    if direction == 'ce':
        in_range = (st >= sp * (1 + min_pct / 100)) & (st <= sp * (1 + max_pct / 100))
    else:
        in_range = (st >= sp * (1 - max_pct / 100)) & (st <= sp * (1 - min_pct / 100))

    masked = np.where(in_range, oi_mat, 0.0)   # (n_bars, n_strikes)
    has    = masked.max(axis=1) > 0             # (n_bars,)

    peak_idx    = np.argmax(masked, axis=1)      # (n_bars,) — safe even if all 0
    wall_strike = np.where(has, strikes[peak_idx], np.nan)
    wall_oi     = np.where(has, masked[np.arange(len(peak_idx)), peak_idx], np.nan)
    return wall_strike, wall_oi


def _max_pain_vec(ce_mat, pe_mat, ce_st, pe_st, spot_arr, max_pct):
    """
    Max pain per bar (only for bars where spot is valid/non-NaN).
    Returns array of shape (n_bars,) with NaN for invalid bars.
    """
    all_st = np.array(sorted(set(ce_st) | set(pe_st)), dtype=float)
    n_bars = len(spot_arr)
    max_pain = np.full(n_bars, np.nan)

    if len(all_st) == 0:
        return max_pain

    # helper: map all_st → column indices in ce_mat / pe_mat
    def _strike_to_col(all_strikes, mat_strikes):
        """Return index array (len=all_strikes) mapping all_strikes → mat column index, -1 if absent."""
        idx_map = np.full(len(all_strikes), -1, dtype=int)
        for j, s in enumerate(all_strikes):
            col = np.searchsorted(mat_strikes, s)
            if col < len(mat_strikes) and mat_strikes[col] == s:
                idx_map[j] = col
        return idx_map

    ce_col_map = _strike_to_col(all_st, ce_st)
    pe_col_map = _strike_to_col(all_st, pe_st)

    # pain bases (nK × nK) — independent of time
    K_2d = all_st[:, None]
    S_2d = all_st[None, :]
    ce_pain_base = np.maximum(K_2d - S_2d, 0.0)   # pain for CE holders at each K
    pe_pain_base = np.maximum(S_2d - K_2d, 0.0)   # pain for PE holders at each K

    for t in range(n_bars):
        sp = spot_arr[t]
        if np.isnan(sp) or sp == 0:
            continue

        lo = sp * (1 - max_pct / 100)
        hi = sp * (1 + max_pct / 100)
        in_range = (all_st >= lo) & (all_st <= hi)
        if not in_range.any():
            continue

        K_idx = np.where(in_range)[0]
        K = all_st[K_idx]

        # OI for the valid strikes only
        ce_oi_t = np.array([
            ce_mat[t, ce_col_map[j]] if ce_col_map[j] >= 0 else 0.0
            for j in K_idx
        ])
        pe_oi_t = np.array([
            pe_mat[t, pe_col_map[j]] if pe_col_map[j] >= 0 else 0.0
            for j in K_idx
        ])

        # pain[k] = Σ_s max(K[k]-S[s],0)*CE[s] + Σ_s max(S[s]-K[k],0)*PE[s]
        pb_ce = ce_pain_base[np.ix_(K_idx, K_idx)]   # (nK, nK)
        pb_pe = pe_pain_base[np.ix_(K_idx, K_idx)]
        pain  = pb_ce @ ce_oi_t + pb_pe @ pe_oi_t    # (nK,)
        max_pain[t] = K[np.argmin(pain)]

    return max_pain


def _pcr_vec(ce_mat, pe_mat, ce_st, pe_st, spot_arr, strike_step, near_strikes, max_pct):
    """Vectorised PCR (near and broad) for all bars."""
    sp = spot_arr[:, None]

    def _band_sum(mat, strikes, lo, hi):
        # lo, hi: (n_bars, 1); returns (n_bars,)
        st = strikes[None, :]                            # (1, n_strikes)
        mask = (st >= lo) & (st <= hi)                  # (n_bars, n_strikes)
        return (np.where(mask, mat, 0.0)).sum(axis=1)   # (n_bars,)

    atm = (spot_arr / strike_step).round() * strike_step

    # near
    near_ce_lo = atm[:, None]
    near_ce_hi = (atm + near_strikes * strike_step)[:, None]
    near_pe_lo = (atm - near_strikes * strike_step)[:, None]
    near_pe_hi = atm[:, None]

    near_ce = _band_sum(ce_mat, ce_st, near_ce_lo, near_ce_hi)
    near_pe = _band_sum(pe_mat, pe_st, near_pe_lo, near_pe_hi)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        pcr_near = np.where(near_ce > 0, near_pe / near_ce, np.nan)

    # broad
    broad_lo = sp * (1 - max_pct / 100)
    broad_hi = sp * (1 + max_pct / 100)
    broad_ce = _band_sum(ce_mat, ce_st, broad_lo, broad_hi)
    broad_pe = _band_sum(pe_mat, pe_st, broad_lo, broad_hi)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        pcr_broad  = np.where(broad_ce > 0, broad_pe / broad_ce, np.nan)
    total_oi = broad_ce + broad_pe

    return pcr_near, pcr_broad, total_oi


# ─── public API ───────────────────────────────────────────────────────────────

def build_oi_profile(
    expiry_date: str,
    options_dir: str,
    index_df: pd.DataFrame,
    strike_step: int = 50,
    resample: str = '5min',
    wall_min_pct: float = 0.5,
    wall_max_pct: float = 10.0,
    near_strikes: int = 5,
) -> pd.DataFrame:
    """
    Build per-bar OI feature DataFrame for one expiry.
    index_df must have a DatetimeIndex and a 'close' column (spot).
    """
    week_dates = _week_dates(expiry_date)
    ce_mat, pe_mat, ce_st, pe_st, grid = _load_all_strikes(options_dir, week_dates)

    if ce_mat.shape[1] == 0 and pe_mat.shape[1] == 0:
        return pd.DataFrame()

    # align spot
    idx_close = index_df['close'].copy()
    if idx_close.index.tz is not None:
        idx_close.index = idx_close.index.tz_localize(None)
    spot_series = idx_close.reindex(grid).ffill()
    spot_arr    = spot_series.values.astype(float)

    # ── vectorised features ────────────────────────────────────────────────
    ce_wall_st, ce_wall_oi_arr = _wall_vec(ce_mat, ce_st, spot_arr, 'ce', wall_min_pct, wall_max_pct)
    pe_wall_st, pe_wall_oi_arr = _wall_vec(pe_mat, pe_st, spot_arr, 'pe', wall_min_pct, wall_max_pct)
    mp_arr = _max_pain_vec(ce_mat, pe_mat, ce_st, pe_st, spot_arr, wall_max_pct)
    pcr_near_arr, pcr_broad_arr, total_oi_arr = _pcr_vec(
        ce_mat, pe_mat, ce_st, pe_st, spot_arr, strike_step, near_strikes, wall_max_pct
    )

    atm_arr = (spot_arr / strike_step).round() * strike_step

    result = pd.DataFrame({
        'spot':             spot_arr,
        'atm_strike':       atm_arr,
        'ce_wall_strike':   ce_wall_st,
        'ce_wall_oi':       ce_wall_oi_arr,
        'ce_wall_dist_pts': ce_wall_st - spot_arr,
        'ce_wall_dist_pct': (ce_wall_st - spot_arr) / spot_arr * 100,
        'pe_wall_strike':   pe_wall_st,
        'pe_wall_oi':       pe_wall_oi_arr,
        'pe_wall_dist_pts': spot_arr - pe_wall_st,
        'pe_wall_dist_pct': (spot_arr - pe_wall_st) / spot_arr * 100,
        'max_pain_strike':  mp_arr,
        'max_pain_dist_pts':mp_arr - spot_arr,
        'pcr_near':         pcr_near_arr,
        'pcr_broad':        pcr_broad_arr,
        'total_oi':         total_oi_arr,
    }, index=grid)

    # ── resample ───────────────────────────────────────────────────────────
    if resample and resample != '1min':
        agg = {c: 'last' for c in result.columns}
        result = (
            result.resample(resample, label='right', closed='right')
            .agg(agg)
            .dropna(subset=['spot'])
        )

    result['expiry'] = expiry_date
    result.index.name = 'ts'
    return result


def oi_at_strike(
    expiry_date: str,
    options_dir: str,
    strike: int,
    side: str,
    index_df: pd.DataFrame,
    lookback_bars: int = 6,
    resample: str = '5min',
) -> pd.DataFrame:
    """
    Per-bar OI + OI delta at a specific strike/side.
    Positive delta = OI building (writers adding), negative = unwinding.
    """
    fname = f'{int(strike)}{side.lower()}.csv'
    fpath = os.path.join(options_dir, fname)
    if not os.path.exists(fpath):
        return pd.DataFrame()

    week_dates = _week_dates(expiry_date)
    grid = _minute_grid(week_dates)

    raw = pd.read_csv(fpath)
    ts_c = _ts_col(raw)
    oi_c = _oi_col(raw)
    if ts_c is None or oi_c is None:
        return pd.DataFrame()

    raw[ts_c] = pd.to_datetime(raw[ts_c], utc=False)
    if raw[ts_c].dt.tz is not None:
        raw[ts_c] = raw[ts_c].dt.tz_localize(None)

    raw = raw[raw[ts_c].dt.date.isin(week_dates)]
    if raw.empty:
        return pd.DataFrame()

    oi_1m  = raw.set_index(ts_c)[oi_c].resample('1min').last().reindex(grid).ffill().fillna(0).clip(lower=0)
    vol_1m = raw.set_index(ts_c)['volume'].resample('1min').sum().reindex(grid).fillna(0)

    idx_close = index_df['close'].copy()
    if idx_close.index.tz is not None:
        idx_close.index = idx_close.index.tz_localize(None)

    if resample and resample != '1min':
        oi_bar  = oi_1m.resample(resample,  label='right', closed='right').last()
        vol_bar = vol_1m.resample(resample, label='right', closed='right').sum()
        spot_bar = idx_close.resample(resample, label='right', closed='right').last()
    else:
        oi_bar, vol_bar, spot_bar = oi_1m, vol_1m, idx_close

    spot_aligned = spot_bar.reindex(oi_bar.index).ffill()

    result = pd.DataFrame({
        'oi':         oi_bar,
        'volume':     vol_bar,
        'oi_delta':   oi_bar.diff(lookback_bars),
        'oi_pct_chg': oi_bar.pct_change(lookback_bars),
        'spot':       spot_aligned,
    })
    result['expiry'] = expiry_date
    result['strike'] = strike
    result['side']   = side
    return result
