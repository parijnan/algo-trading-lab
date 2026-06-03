"""
Iris production utilities.

Includes:
  - SupertrendIndicator (copied from apollo_production/technical_indicators.py)
  - Live ST computation helpers
  - Single-leg order placement + fill verification
  - Guardian check (refuse to start if another strategy is live)
"""
import sys
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from logger_setup import get_logger
from configs import (
    REPO_ROOT, LOT_SIZE, QTY_FREEZE, FO_EXCHANGE,
    ST_PERIOD, ST_MULTIPLIER, ENTRY_TF_MIN, REGIME_TF_MIN,
    SEED_CANDLES, NIFTY_TOKEN, INDEX_EXCHANGE,
    ITM_DEPTH_STEPS, STRIKE_STEP,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Supertrend (copied verbatim from apollo_production/technical_indicators.py)
# ---------------------------------------------------------------------------

class SupertrendIndicator:
    def __init__(self, period=10, multiplier=3.0):
        self.period     = period
        self.multiplier = multiplier

    def calculate(self, df):
        df = df.copy()
        df['H-L']  = df['High'] - df['Low']
        df['H-PC'] = abs(df['High'] - df['Close'].shift(1))
        df['L-PC'] = abs(df['Low']  - df['Close'].shift(1))
        df['TR']   = df[['H-L', 'H-PC', 'L-PC']].max(axis=1)
        df['ATR']  = df['TR'].ewm(alpha=1 / self.period, adjust=False).mean()

        hl2              = (df['High'] + df['Low']) / 2
        df['UpperBand']  = hl2 + self.multiplier * df['ATR']
        df['LowerBand']  = hl2 - self.multiplier * df['ATR']

        trend      = True
        supertrend = []
        for i in range(len(df)):
            if i < self.period:
                supertrend.append(None)
                continue
            curr_close  = df['Close'].iloc[i]
            prev_upper  = df['UpperBand'].iloc[i - 1]
            prev_lower  = df['LowerBand'].iloc[i - 1]
            if curr_close > prev_upper:
                trend = True
            elif curr_close < prev_lower:
                trend = False
            if trend and df['LowerBand'].iloc[i] < prev_lower:
                df.loc[df.index[i], 'LowerBand'] = prev_lower
            if not trend and df['UpperBand'].iloc[i] > prev_upper:
                df.loc[df.index[i], 'UpperBand'] = prev_upper
            supertrend.append(df['LowerBand'].iloc[i] if trend
                              else df['UpperBand'].iloc[i])

        df['Supertrend'] = supertrend
        return df.drop(columns=['H-L', 'H-PC', 'L-PC', 'TR', 'ATR',
                                 'UpperBand', 'LowerBand'])


# ---------------------------------------------------------------------------
# Live ST helpers
# ---------------------------------------------------------------------------

def _candles_to_df(raw_candles: list) -> pd.DataFrame:
    """Convert Angel One getCandleData rows to a DataFrame."""
    rows = []
    for row in raw_candles:
        ts_str = str(row[0]).replace('T', ' ')[:19]
        rows.append({
            'time_stamp': pd.Timestamp(ts_str),
            'open':   float(row[1]),
            'high':   float(row[2]),
            'low':    float(row[3]),
            'close':  float(row[4]),
            'volume': float(row[5]),
        })
    df = pd.DataFrame(rows).sort_values('time_stamp').reset_index(drop=True)
    return df


def compute_st(df: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
    """Add supertrend, trend (bool/NA), trend_flip to lowercase-ohlcv df."""
    base_cols = [c for c in ('time_stamp', 'open', 'high', 'low', 'close', 'volume')
                 if c in df.columns]
    d = df[base_cols].rename(columns={'open': 'Open', 'high': 'High',
                                      'low': 'Low', 'close': 'Close'})
    ind = SupertrendIndicator(period=period, multiplier=multiplier)
    r   = ind.calculate(d).rename(columns={
        'Open': 'open', 'High': 'high', 'Low': 'low',
        'Close': 'close', 'Supertrend': 'supertrend'})
    r['trend'] = (r['close'] > r['supertrend']).astype(object)
    r.loc[r['supertrend'].isna(), 'trend'] = pd.NA
    r['trend_flip'] = r['trend'] != r['trend'].shift(1)
    r.loc[r['supertrend'].isna(), 'trend_flip'] = False
    return r


def fetch_candles(obj, token: str, interval: str, from_dt: datetime,
                  to_dt: datetime) -> list:
    """Fetch candles from Angel One API. Returns raw rows list."""
    params = {
        'exchange':    INDEX_EXCHANGE,
        'symboltoken': token,
        'interval':    interval,
        'fromdate':    from_dt.strftime('%Y-%m-%d %H:%M'),
        'todate':      to_dt.strftime('%Y-%m-%d %H:%M'),
    }
    for attempt in range(3):
        try:
            resp = obj.getCandleData(params)
            data = resp.get('data', [])
            if data:
                return data
        except Exception as e:
            logger.warning(f'getCandleData attempt {attempt+1} failed: {e}')
            time.sleep(1)
    return []


def seed_st(obj, now: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Fetch SEED_CANDLES worth of 5-min and 15-min Nifty candles.
    Returns (df_5m_with_st, df_15m_with_st).
    """
    from_5m  = now - timedelta(minutes=SEED_CANDLES * ENTRY_TF_MIN)
    from_15m = now - timedelta(minutes=SEED_CANDLES * REGIME_TF_MIN)

    # NOTE: 'FIVE_MINUTE' interval — verify Angel One serves this before
    # first live/paper session. Fallback: fetch ONE_MINUTE and resample.
    raw_5m  = fetch_candles(obj, NIFTY_TOKEN, 'FIVE_MINUTE',  from_5m,  now)
    raw_15m = fetch_candles(obj, NIFTY_TOKEN, 'FIFTEEN_MINUTE', from_15m, now)

    df_5m  = compute_st(_candles_to_df(raw_5m),  ST_PERIOD, ST_MULTIPLIER)
    df_15m = compute_st(_candles_to_df(raw_15m), ST_PERIOD, ST_MULTIPLIER)

    logger.info(f'Seeded: {len(df_5m)} 5-min bars, {len(df_15m)} 15-min bars')
    return df_5m, df_15m


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------

def select_expiry(instrument_df: pd.DataFrame, today) -> object:
    """
    Return the nearest weekly expiry with ELM date strictly after today.
    Returns a date object or None.
    """
    from datetime import date as date_cls
    from math import floor
    expiries = (
        instrument_df['expiry']
        .drop_duplicates()
        .apply(lambda x: datetime.strptime(x, '%d%b%Y').date())
        .sort_values()
    )
    for exp in expiries:
        if exp <= today:
            continue
        # ELM date = last trading day before expiry
        elm = exp - timedelta(days=1)
        while elm.weekday() in (5, 6):   # skip Saturday, Sunday
            elm -= timedelta(days=1)
        if elm > today:
            return exp
    return None


def select_strike_and_token(instrument_df: pd.DataFrame, spot: float,
                             direction: str, expiry) -> tuple:
    """
    Returns (strike, option_type, symbol, token) for ITM_DEPTH_STEPS × 50
    into the money. CE for bullish (lower strike), PE for bearish (higher).
    """
    atm          = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
    option_type  = 'ce' if direction == 'bullish' else 'pe'
    sign         = -1  if direction == 'bullish' else +1
    strike       = atm + sign * ITM_DEPTH_STEPS * STRIKE_STEP

    expiry_str = expiry.strftime('%d%b%Y').upper()
    row = instrument_df[
        (instrument_df['expiry'] == expiry_str) &
        (instrument_df['strike'] == strike * 100) &
        (instrument_df['symbol'].str[-2:] == option_type.upper())
    ]
    if row.empty:
        logger.error(f'Token not found: {strike}{option_type.upper()} {expiry_str}')
        return None, None, None, None

    symbol = row['symbol'].iloc[0]
    token  = str(row['token'].iloc[0])
    logger.info(f'Strike selected: {strike}{option_type.upper()} {expiry_str} '
                f'→ {symbol} ({token})  spot={spot:.0f}  ATM={atm}')
    return strike, option_type, symbol, token


# ---------------------------------------------------------------------------
# Order placement (single leg, market order)
# ---------------------------------------------------------------------------

def place_order(obj, transaction_type: str, symbol: str, token: str,
                lots: int, paper_mode: bool) -> str | None:
    """
    Place a single-leg MARKET order. Returns order_id or None.
    In paper mode: logs the intent and returns a dummy ID.
    """
    qty = lots * LOT_SIZE
    if paper_mode:
        logger.info(f'[PAPER] {transaction_type} {qty} units of {symbol} '
                    f'(token={token})')
        return 'PAPER_ORDER_ID'

    orderparams = {
        'variety':         'NORMAL',
        'tradingsymbol':   symbol,
        'symboltoken':     token,
        'transactiontype': transaction_type,
        'exchange':        FO_EXCHANGE,
        'ordertype':       'MARKET',
        'producttype':     'CARRYFORWARD',
        'duration':        'DAY',
        'quantity':        str(qty),
        'price':           '0',
        'triggerprice':    '0',
    }
    try:
        resp = obj.placeOrderFullResponse(orderparams)
        order_id = resp.get('data', {}).get('orderid')
        logger.info(f'Order placed: {transaction_type} {qty} × {symbol} '
                    f'→ orderid={order_id}')
        return order_id
    except Exception as e:
        logger.error(f'Order placement failed: {e}')
        return None


def verify_fill(obj, order_id: str, symbol: str, lots: int,
                paper_mode: bool) -> float | None:
    """
    Poll orderBook for fill price. Returns avg fill price or None.
    In paper mode: returns None (caller uses LTP as proxy).
    Timeout: 30 seconds.
    """
    if paper_mode:
        return None

    expected_qty = lots * LOT_SIZE
    deadline     = time.time() + 30
    while time.time() < deadline:
        try:
            book = obj.orderBook()
            orders = book.get('data') or []
            for o in orders:
                if str(o.get('orderid')) == str(order_id):
                    if o.get('status') == 'complete':
                        return float(o.get('averageprice', 0))
                    if o.get('status') in ('rejected', 'cancelled'):
                        logger.error(f'Order {order_id} {o.get("status")}')
                        return None
        except Exception as e:
            logger.warning(f'orderBook poll failed: {e}')
        time.sleep(1)

    logger.error(f'Fill timeout for {order_id} ({symbol})')
    return None


# ---------------------------------------------------------------------------
# Guardian: refuse to start if any other strategy has open positions
# ---------------------------------------------------------------------------

_INACTIVE = {'idle', 'open', 'closed', 'None', None, ''}

def check_no_active_strategies() -> tuple[bool, str]:
    """
    Returns (ok, reason). ok=True means it is safe to start Iris.
    Checks Apollo, Athena, and both Artemis legs.
    """
    checks = [
        ('Apollo',     REPO_ROOT / 'apollo_production/data/apollo_state.csv',          'status'),
        ('Athena',     REPO_ROOT / 'athena_production/data/athena_state.csv',           'status'),
        ('Artemis PE', REPO_ROOT / 'artemis_production/data/pe_trade_params.csv',       'spread_status'),
        ('Artemis CE', REPO_ROOT / 'artemis_production/data/ce_trade_params.csv',       'spread_status'),
    ]
    for name, path, col in checks:
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if df.empty:
            continue
        val = str(df[col].iloc[0]).strip()
        if val not in _INACTIVE:
            return False, f'{name} has open position (status={val!r})'
    return True, ''
