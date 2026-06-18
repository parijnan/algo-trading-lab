"""
Shared Greek computation layer for all Greek Analysis research branches.

IV computation: mibian Black-Scholes with float DTE (calendar days).

mibian unit conventions kept throughout:
  - IV in percent (e.g. 17.5, not 0.175)
  - daysToExpiration: float calendar days
  - rate: percent (5.0 for 5%)
  - theta: points per calendar day (negative — option loses value over time)
  - vega:  points per 1 vol-point (per 1% change in IV)
  - gamma: per (spot-point)^2
  - delta: dimensionless (calls [0,1]; puts [-1,0])
"""

import os
import mibian
import pandas as pd

RISK_FREE_RATE = 5.0   # % — annualised, matches athena_backtest/configs.py
LOT_SIZE = 65          # Nifty lot size (constant across the backtest period 2020–2026)

TRADE_LOGS_DIR = "athena_backtest/data/trade_logs"
TRADE_SUMMARY_PATH = "athena_backtest/data/trade_summary.csv"
IV_CACHE_DIR = "research/greek_analysis/data/iv_cache"


# ---------------------------------------------------------------------------
# DTE helpers
# ---------------------------------------------------------------------------

def get_dte_days(ts: pd.Timestamp, expiry_date: str) -> float:
    """Calendar days from ts to expiry close (15:30 on expiry_date). Clamps to 0."""
    expiry_dt = pd.Timestamp(expiry_date) + pd.Timedelta(hours=15, minutes=30)
    return max((expiry_dt - ts).total_seconds() / 86400.0, 0.0)


# ---------------------------------------------------------------------------
# Core Greeks
# ---------------------------------------------------------------------------

def compute_iv(ltp: float, spot: float, strike: float,
               dte_days: float, option_type: str) -> float | None:
    """
    Back-out IV (%) from market LTP using mibian Black-Scholes.

    Returns None when:
      - DTE < 0.0001 days (sub-minute to expiry — model unstable)
      - LTP < 0.05 (effectively zero — cannot meaningfully back out IV)
      - Time value < 0.5 pts (LTP ≈ intrinsic — mibian root-finder has no valid
        solution when price is at or below intrinsic, causing an infinite loop)
      - mibian returns non-finite or out-of-range IV
    """
    if dte_days < 1e-4 or ltp < 0.05:
        return None
    intrinsic = max(0.0, (strike - spot) if option_type == 'pe' else (spot - strike))
    if ltp <= intrinsic + 0.5:
        return None
    try:
        args = [float(spot), float(strike), RISK_FREE_RATE, float(dte_days)]
        if option_type == 'ce':
            m = mibian.BS(args, callPrice=float(ltp))
        else:
            m = mibian.BS(args, putPrice=float(ltp))
        iv = m.impliedVolatility
        return float(iv) if (iv is not None and 0.0 < iv < 300.0) else None
    except Exception:
        return None


def compute_greeks(iv: float, spot: float, strike: float,
                   dte_days: float, option_type: str) -> dict | None:
    """
    Compute delta, gamma, theta, vega from IV using mibian Black-Scholes.

    Returns None when IV is None or DTE < 0.0001.

    Units (all mibian conventions):
      delta: dimensionless (calls 0→1, puts -1→0)
      gamma: 1 / spot-point  (change in delta per 1-point spot move)
      theta: points / calendar-day  (negative — option price decays)
      vega:  points / vol-point  (per 1% absolute change in IV)
    """
    if iv is None or dte_days < 1e-4:
        return None
    try:
        m = mibian.BS([float(spot), float(strike), RISK_FREE_RATE, float(dte_days)],
                      volatility=float(iv))
        if option_type == 'ce':
            return {
                'delta': m.callDelta,
                'gamma': m.gamma,
                'theta': m.callTheta,
                'vega': m.vega,
            }
        else:
            return {
                'delta': m.putDelta,
                'gamma': m.gamma,
                'theta': m.putTheta,
                'vega': m.vega,
            }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Trade data loading
# ---------------------------------------------------------------------------

def load_trade_summary(path: str = TRADE_SUMMARY_PATH) -> pd.DataFrame:
    """
    Load trade summary CSV.

    Adds:
      trade_id   — 1-indexed sequential ID matching log filenames
      entry_date — date string 'YYYY-MM-DD' of entry_time (used in log filename)
    """
    df = pd.read_csv(path, parse_dates=['entry_time', 'exit_time'])
    df['trade_id'] = range(1, len(df) + 1)
    df['entry_date'] = df['entry_time'].dt.date.astype(str)
    return df


def trade_log_path(trade_id: int, entry_date: str,
                   log_dir: str = TRADE_LOGS_DIR) -> str:
    """Return the absolute path for a trade's log file."""
    return os.path.join(log_dir, f"trade_{trade_id:04d}_{entry_date}.csv")


def load_trade_log(trade_id: int, entry_date: str,
                   log_dir: str = TRADE_LOGS_DIR) -> pd.DataFrame:
    """Load a single trade log. Returns DataFrame with time_stamp as Timestamp."""
    return pd.read_csv(trade_log_path(trade_id, entry_date, log_dir),
                       parse_dates=['time_stamp'])


def build_bar_sequence(row: pd.Series, log: pd.DataFrame) -> pd.DataFrame:
    """
    Prepend synthetic bar0 (at entry_time with entry prices) to the trade log.

    Without bar0, attribution would miss the first minute's P&L move
    (entry at 10:30, log starts at 10:31).

    bar0 has emer_ltp=0 (hedge not active at entry by definition).
    """
    bar0_values = {
        'time_stamp':  row['entry_time'],
        'spot':        row['entry_spot'],
        'vix':         row['entry_vix'],
        'ce_sell_ltp': row['ce_sell_entry'],
        'ce_buy_ltp':  row['ce_buy_entry'],
        'pe_sell_ltp': row['pe_sell_entry'],
        'pe_buy_ltp':  row['pe_buy_entry'],
        'ce_wing_ltp': float(row['ce_wing_entry']) if pd.notna(row.get('ce_wing_entry')) else 0.0,
        'pe_wing_ltp': float(row['pe_wing_entry']) if pd.notna(row.get('pe_wing_entry')) else 0.0,
        'emer_ltp':    0.0,
        'emer_strike': float('nan'),
        'ce_sell_strike': row['ce_sell_strike'],
        'pe_sell_strike': row['pe_sell_strike'],
        'ce_wing_strike': row.get('ce_wing_strike', float('nan')),
        'pe_wing_strike': row.get('pe_wing_strike', float('nan')),
    }
    # Build bar0 DataFrame with all columns from log, filling unknowns with NaN
    bar0 = pd.DataFrame([{col: bar0_values.get(col, float('nan')) for col in log.columns}])
    return pd.concat([bar0, log], ignore_index=True)


# ---------------------------------------------------------------------------
# IV caching
# ---------------------------------------------------------------------------

def iv_cache_path(trade_id: int, entry_date: str,
                  cache_dir: str = IV_CACHE_DIR) -> str:
    return os.path.join(cache_dir, f"trade_{trade_id:04d}_{entry_date}.parquet")


IV_COLUMNS = ['time_stamp', 'ce_sell_iv', 'ce_buy_iv', 'pe_sell_iv', 'pe_buy_iv',
              'ce_wing_iv', 'pe_wing_iv', 'emer_iv']


def compute_trade_iv(bars: pd.DataFrame, row: pd.Series) -> pd.DataFrame:
    """
    Compute per-bar IV for all legs of a single trade.

    Legs and their definitions:
      ce_sell:  CE, strike=ce_sell_strike, expiry=sell_expiry, short (-1)
      ce_buy:   CE, strike=ce_sell_strike, expiry=buy_expiry,  long  (+1)
      pe_sell:  PE, strike=pe_sell_strike, expiry=sell_expiry, short (-1)
      pe_buy:   PE, strike=pe_sell_strike, expiry=buy_expiry,  long  (+1)
      ce_wing:  CE, strike=ce_wing_strike (bar-level), expiry=buy_expiry, long (+1) — optional
      pe_wing:  PE, strike=pe_wing_strike (bar-level), expiry=buy_expiry, long (+1) — optional
      emer:     CE, strike=emer_strike    (bar-level), expiry=buy_expiry, long (+1) — optional

    Returns DataFrame with columns: IV_COLUMNS
    """
    sell_exp = str(row['sell_expiry'])
    buy_exp  = str(row['buy_expiry'])

    ce_sell_k = float(row['ce_sell_strike'])
    pe_sell_k = float(row['pe_sell_strike'])

    result = []
    for _, bar in bars.iterrows():
        ts   = bar['time_stamp']
        spot = float(bar['spot'])

        def iv_for(ltp_col, strike, expiry, opt_type):
            ltp = bar.get(ltp_col, 0.0)
            if pd.isna(ltp) or float(ltp) == 0.0:
                return None
            dte = get_dte_days(ts, expiry)
            return compute_iv(float(ltp), spot, float(strike), dte, opt_type)

        def iv_for_bar_leg(ltp_col, strike_col, expiry, opt_type):
            k = bar.get(strike_col, float('nan'))
            if pd.isna(k):
                return None
            ltp = bar.get(ltp_col, 0.0)
            if pd.isna(ltp) or float(ltp) == 0.0:
                return None
            dte = get_dte_days(ts, expiry)
            return compute_iv(float(ltp), spot, float(k), dte, opt_type)

        result.append({
            'time_stamp': ts,
            'ce_sell_iv': iv_for('ce_sell_ltp', ce_sell_k, sell_exp, 'ce'),
            'ce_buy_iv':  iv_for('ce_buy_ltp',  ce_sell_k, buy_exp,  'ce'),
            'pe_sell_iv': iv_for('pe_sell_ltp',  pe_sell_k, sell_exp, 'pe'),
            'pe_buy_iv':  iv_for('pe_buy_ltp',   pe_sell_k, buy_exp,  'pe'),
            'ce_wing_iv': iv_for_bar_leg('ce_wing_ltp', 'ce_wing_strike', buy_exp, 'ce'),
            'pe_wing_iv': iv_for_bar_leg('pe_wing_ltp', 'pe_wing_strike', buy_exp, 'pe'),
            'emer_iv':    iv_for_bar_leg('emer_ltp',    'emer_strike',    buy_exp, 'ce'),
        })

    return pd.DataFrame(result)


def load_or_compute_iv(trade_id: int, entry_date: str,
                       bars: pd.DataFrame, row: pd.Series,
                       cache_dir: str = IV_CACHE_DIR) -> pd.DataFrame:
    """
    Load per-bar IV from parquet cache; recompute and save if cache is missing or stale.

    Staleness check: cached row count must match current bar count.
    """
    os.makedirs(cache_dir, exist_ok=True)
    path = iv_cache_path(trade_id, entry_date, cache_dir)

    if os.path.exists(path):
        cached = pd.read_parquet(path)
        expected_cols = set(IV_COLUMNS)
        if len(cached) == len(bars) and expected_cols.issubset(set(cached.columns)):
            return cached

    iv_df = compute_trade_iv(bars, row)
    iv_df.to_parquet(path, index=False)
    return iv_df
