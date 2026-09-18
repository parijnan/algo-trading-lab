"""
IV persistence and IV -> forward realized-vol forecast power, CRUDEOILM ATM straddle.
Input: prometheus_backtest/phase3_fyers/options_iv/data/atm_iv_15min.csv (23,338 rows).
Companion to the IV-vs-trade-outcome correlation documented in prometheus_backtest/README.md
("Historical ATM-straddle implied volatility" section) -- that test found no signal;
this one answers the other half of the original question ("can we predict volatility").
"""
import pandas as pd
import numpy as np
from scipy import stats as sstats

REPO = '/home/parijnan/scripts/algo-trading-lab'
IV_FILE = f'{REPO}/prometheus_backtest/phase3_fyers/options_iv/data/atm_iv_15min.csv'


def load():
    iv = pd.read_csv(IV_FILE)
    iv['time_stamp'] = pd.to_datetime(iv['time_stamp'])
    return iv.sort_values('time_stamp').reset_index(drop=True)


def iv_persistence(iv):
    print('=== IV level persistence (autocorrelation) ===')
    same_session = iv['time_stamp'].diff().dt.total_seconds() / 60 <= 20
    ac1 = iv['atm_iv'].corr(iv['atm_iv'].shift(1).where(same_session))
    print(f'  same-session lag=1 (15min): corr={ac1:.4f}  n={same_session.sum()}')

    daily = iv.groupby(iv['time_stamp'].dt.date)['atm_iv'].mean().reset_index(name='mean_iv')
    for lag in (1, 5, 20):
        print(f'  daily lag={lag:2d} trading day(s): autocorr={daily["mean_iv"].autocorr(lag=lag):.4f}  '
              f'n_days={len(daily)}')


def iv_forecasts_realized_vol(iv):
    """Calendar-time-scaled realized vol (var / elapsed_days * 365), matching mibian's own
    days/365 IV day-count convention -- deliberately NOT an assumed bars-per-year constant,
    since this dataset has real, known gaps (options history starts 2024-06-14, a
    2026-03-13->2026-06-29 Fyers void, a 2025-10-16 cycle gap) that make an empirically
    derived bars/year figure unreliable (confirmed: naively deriving it from this file's own
    gaps gives ~183 trading days/year vs MCX's real ~250-252-day calendar -- an artifact of
    the holes, not a real annualization factor)."""
    print('\n=== IV -> forward realized volatility ===')
    dt_min = iv['time_stamp'].diff().dt.total_seconds() / 60
    log_ret = np.log(iv['futures_price'] / iv['futures_price'].shift(1))
    structural_break = dt_min > 3 * 1440   # multi-day void / contract-splice edges
    z = (log_ret - log_ret.mean()) / log_ret.std()
    clean = ~structural_break & (z.abs() <= 8) & log_ret.notna()
    log_ret = log_ret.where(clean)

    s = pd.Series((log_ret ** 2).values, index=iv['time_stamp'])
    for K in (1, 5, 20):
        rev = s[::-1]
        fwd_sumsq = rev.rolling(f'{K}D', min_periods=1).sum()[::-1].shift(-1)
        fwd_cnt = s.notna()[::-1].astype(int).rolling(
            f'{K}D', min_periods=1).sum()[::-1].shift(-1)
        fwd_rv = np.sqrt(fwd_sumsq / K * 365) * 100
        fwd_rv[fwd_cnt < K * 57 * 0.5] = np.nan   # require reasonable data coverage (57 bars/session, empirical)

        df = pd.DataFrame({'atm_iv': iv['atm_iv'].values, 'fwd_rv': fwd_rv.values}).dropna()
        pc, pp = sstats.pearsonr(df['atm_iv'], df['fwd_rv'])
        sc, sp = sstats.spearmanr(df['atm_iv'], df['fwd_rv'])
        bias = (df['atm_iv'] - df['fwd_rv']).mean()
        print(f'  K={K:2d} calendar day(s): n={len(df)}  Pearson r={pc:.4f} (p={pp:.2e})  '
              f'Spearman rho={sc:.4f} (p={sp:.2e})  mean(IV-fwd_RV)={bias:.1f}pp '
              f'({bias / df["atm_iv"].mean() * 100:.0f}% of mean IV)')


if __name__ == '__main__':
    iv = load()
    iv_persistence(iv)
    iv_forecasts_realized_vol(iv)
