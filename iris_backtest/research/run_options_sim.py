"""
Options fill simulation for ST_FAST signals.

For each ST_FAST signal: buy ATM, mild-ITM (−50), or moderate-ITM (−100) call/put
on the nearest weekly Nifty expiry with DTE ≥ 2. Entry at the open of the bar
5 min after signal (BAR_PERIOD_MINUTES = 5). Exit measured at 5, 15, 30 min.

Output: data/options_sim_results.csv + printed summary table.

Usage (from repo root):
    python iris_backtest/research/run_options_sim.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from datetime import date, timedelta
from configs import OUTPUT_DIR, OPTIONS_PATH, LOT_SIZE, STRIKE_STEP

SIM_HORIZONS   = [5, 15, 30]
MIN_DTE        = 2      # minimum calendar days to expiry at signal time
MAX_GAP_MIN    = 5      # max minutes to search for a price around target timestamp
DEPTHS         = [0, 1, 2]     # 0 = ATM; 1 = one step ITM; 2 = two steps ITM
# CE ITM = lower strike (ATM − depth×step); PE ITM = higher strike (ATM + depth×step)
DEPTH_LABELS   = ['ATM', 'ITM_50', 'ITM_100']


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_expiry_dates() -> list[date]:
    dirs = sorted(OPTIONS_PATH.glob('20??-??-??'))
    return [date.fromisoformat(d.name) for d in dirs]


def _nearest_expiry(signal_date: date, expiry_dates: list[date]) -> date | None:
    for exp in expiry_dates:
        if (exp - signal_date).days >= MIN_DTE:
            return exp
    return None


def _atm_strike(spot: float) -> int:
    return int(round(spot / STRIKE_STEP) * STRIKE_STEP)


def _load_option(expiry: date, strike: int, right: str) -> pd.DataFrame | None:
    """right = 'ce' or 'pe'. Returns DataFrame indexed by datetime, or None."""
    path = OPTIONS_PATH / expiry.isoformat() / f'{strike}{right}.csv'
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=['datetime'])
    df = df.set_index('datetime').sort_index()
    return df[(df['open'] > 0) & (df['close'] > 0)]


def _price_at(df_opt: pd.DataFrame | None, ts: pd.Timestamp,
               col: str = 'open') -> float:
    """Return option price at or within MAX_GAP_MIN minutes of ts."""
    if df_opt is None or df_opt.empty:
        return np.nan
    idx = df_opt.index
    pos = idx.searchsorted(ts)
    # Check forward bar first (for entry), then backward (fallback)
    for i in (pos, pos - 1):
        if 0 <= i < len(idx):
            gap = abs((idx[i] - ts).total_seconds()) / 60
            if gap <= MAX_GAP_MIN:
                return float(df_opt.iloc[i][col])
    return np.nan


# ---------------------------------------------------------------------------
# Main simulation
# ---------------------------------------------------------------------------

def main():
    exc_path = OUTPUT_DIR / 'ST_FAST_excursions.csv'
    if not exc_path.exists():
        print(f'Missing {exc_path} — run: python iris_backtest/research/run_all.py --signal ST_FAST')
        sys.exit(1)

    exc = pd.read_csv(exc_path, parse_dates=['signal_ts', 'entry_ts'])
    expiry_dates = _load_expiry_dates()

    print(f'ST_FAST signals:   {len(exc):,}')
    print(f'Expiry dates avail: {len(expiry_dates)}  '
          f'({expiry_dates[0]} → {expiry_dates[-1]})\n')

    records = []

    for _, row in exc.iterrows():
        signal_ts  = row['signal_ts']
        entry_ts   = row['entry_ts']
        direction  = row['direction']
        spot       = row['entry_price']

        if pd.isna(spot) or pd.isna(entry_ts):
            continue

        expiry = _nearest_expiry(signal_ts.date(), expiry_dates)
        if expiry is None:
            continue

        right  = 'ce' if direction == 'bullish' else 'pe'
        atm    = _atm_strike(spot)

        # ITM for CE = lower strike; ITM for PE = higher strike
        sign   = -1 if right == 'ce' else +1

        rec = {
            'signal_ts': signal_ts,
            'direction': direction,
            'spot':      spot,
            'expiry':    expiry.isoformat(),
        }

        for depth, label in zip(DEPTHS, DEPTH_LABELS):
            strike = atm + sign * depth * STRIKE_STEP
            df_opt = _load_option(expiry, strike, right)

            entry_px = _price_at(df_opt, entry_ts, 'open')
            rec[f'{label}_entry'] = entry_px

            for N in SIM_HORIZONS:
                exit_ts  = entry_ts + pd.Timedelta(minutes=N)
                exit_px  = _price_at(df_opt, exit_ts, 'open')
                pnl      = (exit_px - entry_px) if pd.notna(exit_px) and pd.notna(entry_px) else np.nan
                rec[f'{label}_exit_{N}']  = exit_px
                rec[f'{label}_pnl_{N}']   = round(pnl, 2) if pd.notna(pnl) else np.nan
                rec[f'{label}_pnl_rs_{N}'] = round(pnl * LOT_SIZE, 0) if pd.notna(pnl) else np.nan

        records.append(rec)

    results = pd.DataFrame(records)
    out_path = OUTPUT_DIR / 'options_sim_results.csv'
    results.to_csv(out_path, index=False)
    print(f'Saved {len(results):,} rows → {out_path.name}\n')

    # ── Summary table ──────────────────────────────────────────────────────
    print(f'{"Depth":<10}  {"Trades":>7}  {"Avg Entry":>10}',
          end='')
    for N in SIM_HORIZONS:
        print(f'  {"WR@"+str(N)+"m":>7} {"AvgPnL":>8} {"MedPnL":>8} {"AvgRs":>8}', end='')
    print()
    print('─' * (10 + 12 + len(SIM_HORIZONS) * 36))

    for label in DEPTH_LABELS:
        entry_col = f'{label}_entry'
        valid     = results[results[entry_col].notna()]
        n         = len(valid)
        avg_entry = valid[entry_col].mean()

        print(f'{label:<10}  {n:>7,}  {avg_entry:>10.1f}', end='')
        for N in SIM_HORIZONS:
            pnl_col = f'{label}_pnl_{N}'
            rs_col  = f'{label}_pnl_rs_{N}'
            sub     = valid[valid[pnl_col].notna()]
            if sub.empty:
                print(f'  {"n/a":>7} {"n/a":>8} {"n/a":>8} {"n/a":>8}', end='')
                continue
            wr   = (sub[pnl_col] > 0).mean() * 100
            avg  = sub[pnl_col].mean()
            med  = sub[pnl_col].median()
            avrs = sub[rs_col].mean()
            print(f'  {wr:>6.1f}% {avg:>+8.2f} {med:>+8.2f} {avrs:>+8.0f}', end='')
        print()

    print(f'\nP&L in option points (×{LOT_SIZE} = INR per lot).')


if __name__ == '__main__':
    main()
