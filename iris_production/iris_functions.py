"""
Iris production utilities.

Includes:
  - SupertrendIndicator (copied from apollo_production/technical_indicators.py)
  - Live ST computation helpers
  - Single-leg order placement + fill verification
  - OrderFillWatcher — Angel One order-update WebSocket (ported from Apollo)
  - Guardian check (refuse to start if another strategy is live)
"""
import sys
import json
import threading
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from iris_logger_setup import get_logger
from iris_configs import (
    REPO_ROOT, LOT_SIZE, QTY_FREEZE, FO_EXCHANGE,
    ST_PERIOD, ST_MULTIPLIER, ENTRY_TF_MIN, REGIME_TF_MIN,
    SEED_DAYS, NIFTY_TOKEN, INDEX_EXCHANGE, MARKET_OPEN,
    ITM_DEPTH_STEPS, STRIKE_STEP, ORDER_TIMEOUT_SEC, LTP_POLL_LIMIT,
)

try:
    from SmartApi.smartWebSocketOrderUpdate import SmartWebSocketOrderUpdate
    _ORDER_WS_AVAILABLE = True
except ImportError:
    _ORDER_WS_AVAILABLE = False

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
    r['supertrend'] = pd.to_numeric(r['supertrend'], errors='coerce')  # None → NaN for warmup bars
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


def _resample_to_15m(df_5m: pd.DataFrame) -> pd.DataFrame:
    """
    Resample 5-min OHLCV DataFrame to 15-min, anchored at MARKET_OPEN (09:15).
    Iterates day-by-day, same approach as Apollo's 15→75 resample.
    """
    market_open_time = pd.Timestamp(MARKET_OPEN).time()
    candles_15 = []

    for date, day_df in df_5m.groupby(df_5m['time_stamp'].dt.date):
        anchor = pd.Timestamp(f'{date} {MARKET_OPEN}')
        while anchor.time() <= pd.Timestamp(f'{date} 15:15').time():
            window_end = anchor + timedelta(minutes=REGIME_TF_MIN) - timedelta(minutes=ENTRY_TF_MIN)
            window = day_df[
                (day_df['time_stamp'] >= anchor) &
                (day_df['time_stamp'] <= window_end)
            ]
            if not window.empty:
                candles_15.append({
                    'time_stamp': anchor,
                    'open':       window['open'].iloc[0],
                    'high':       window['high'].max(),
                    'low':        window['low'].min(),
                    'close':      window['close'].iloc[-1],
                    'volume':     window['volume'].sum(),
                })
            anchor += timedelta(minutes=REGIME_TF_MIN)

    if not candles_15:
        return pd.DataFrame()
    return pd.DataFrame(candles_15).reset_index(drop=True)


def seed_st(obj, now: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Single FIVE_MINUTE fetch over SEED_DAYS calendar days (~975 bars, under API 1000-record limit).
    Weekends and holidays are naturally absent — no special handling needed.
    Resamples to 15-min anchored at 09:15 for the regime ST.
    Returns (df_5m_with_st, df_15m_with_st).
    """
    from_dt = now - timedelta(days=SEED_DAYS)
    raw     = fetch_candles(obj, NIFTY_TOKEN, 'FIVE_MINUTE', from_dt, now)
    if not raw:
        logger.error('seed_st: no candle data returned — cannot seed ST')
        return pd.DataFrame(), pd.DataFrame()

    df = _candles_to_df(raw)

    # Filter to market hours — drops overnight/weekend noise
    market_open_time = pd.Timestamp(MARKET_OPEN).time()
    df = df[df['time_stamp'].dt.time >= market_open_time].copy()

    # Drop incomplete current bar (still forming)
    minutes_into_bar = now.minute % ENTRY_TF_MIN
    current_bar_open = now.replace(minute=now.minute - minutes_into_bar,
                                   second=0, microsecond=0)
    df = df[df['time_stamp'] < current_bar_open].copy()

    # Cap at 975 bars (13 × 75) — defensive guard against API returning more
    if len(df) > 975:
        df = df.iloc[-975:]

    df = df.sort_values('time_stamp').reset_index(drop=True)

    df_5m  = compute_st(df, ST_PERIOD, ST_MULTIPLIER)
    df_15m = compute_st(_resample_to_15m(df), ST_PERIOD, ST_MULTIPLIER)

    logger.info(
        f'Seeded: {len(df_5m)} 5-min bars, {len(df_15m)} 15-min bars  |  '
        f'5m: trend={df_5m.iloc[-1]["trend"]} ST={df_5m.iloc[-1]["supertrend"]:.2f}  '
        f'15m: trend={df_15m.iloc[-1]["trend"]} ST={df_15m.iloc[-1]["supertrend"]:.2f}'
    )

    # Log last flip timestamps so we can verify signal history at startup
    for label, dff in (('5m', df_5m), ('15m', df_15m)):
        flips = dff[dff['trend_flip'] == True]
        if not flips.empty:
            last = flips.iloc[-1]
            logger.info(
                f'  Last {label} flip: {last["time_stamp"]}  '
                f'→ {"bullish" if last["trend"] == True else "bearish"}  '
                f'close={last["close"]:.2f}  ST={last["supertrend"]:.2f}'
            )

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
# Order fill WebSocket (ported from Apollo)
# ---------------------------------------------------------------------------

class OrderFillWatcher(SmartWebSocketOrderUpdate if _ORDER_WS_AVAILABLE else object):
    """
    Background daemon for Angel One order-update WebSocket.
    Captures AB05 (complete), AB02 (cancelled), AB03 (rejected) events
    into live_orders keyed by orderid. _ws_ready is set on AB00 ack.
    Strategy polls live_orders for fill price instead of calling orderBook().
    Falls back gracefully if SmartWebSocketOrderUpdate is unavailable.
    """
    _TERMINAL_STATUSES = ('AB05', 'AB02', 'AB03')

    def __init__(self):
        if _ORDER_WS_AVAILABLE:
            # Skip parent __init__ — it runs logzero with a relative path
            self.wsapp                 = None
            self.last_pong_timestamp   = None
            self.current_retry_attempt = 0
            self.auth_token            = None
            self.api_key               = None
            self.client_code           = None
            self.feed_token            = None
        self._ws_ready   = threading.Event()
        self.live_orders = {}
        self._lock       = threading.Lock()

    def start(self, auth_token, api_key, client_code, feed_token):
        if not _ORDER_WS_AVAILABLE:
            logger.warning('OrderFillWatcher: SmartWebSocketOrderUpdate not available — REST fallback only.')
            return
        self.auth_token  = auth_token
        self.api_key     = api_key
        self.client_code = client_code
        self.feed_token  = feed_token
        threading.Thread(target=self._run, daemon=True, name='OrderFillWatcher').start()

    def _run(self):
        try:
            self.connect()
        except Exception:
            pass  # Non-fatal — falls back to REST orderBook

    def on_open(self, wsapp):
        pass

    def on_message(self, wsapp, message):
        self._handle(message)

    def on_data(self, wsapp, message, data_type, continue_flag):
        self._handle(message)  # AB00 ack arrives here

    def on_pong(self, wsapp, data):
        heartbeat = getattr(self, 'HEARTBEAT_MESSAGE', None)
        if heartbeat and data == heartbeat:
            self.last_pong_timestamp = time.time()
        else:
            self._handle(data)

    def on_error(self, wsapp, error):
        pass

    def on_close(self, wsapp, close_status_code, close_msg):
        self._ws_ready.clear()
        if _ORDER_WS_AVAILABLE:
            self.retry_connect()

    def _handle(self, raw):
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
        except Exception:
            return
        status = parsed.get('order-status')
        if status == 'AB00':
            self._ws_ready.set()
            logger.info('OrderFillWatcher: WS connected and ready.')
            return
        if status in self._TERMINAL_STATUSES:
            od = parsed.get('orderData')
            if od:
                oid = od.get('orderid')
                if oid:
                    with self._lock:
                        self.live_orders[oid] = od


# ---------------------------------------------------------------------------
# Order placement (single leg, market order)
# ---------------------------------------------------------------------------

def place_order(obj, transaction_type: str, symbol: str, token: str,
                lots: int, dry_run: bool) -> str | None:
    """
    Place a single-leg MARKET order. Returns order_id or None.
    In dry-run mode: logs the intent and returns a dummy ID.
    """
    qty = lots * LOT_SIZE
    if dry_run:
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


# ---------------------------------------------------------------------------
# REST-fallback rate limiter — LTP endpoint only (Iris has no RMS/order-book
# polling like Artemis/Athena). Self-healing per-second bucket: mirrors
# artemis_production/functions.py::_check_limit, sized off AngelOne's
# documented LTP endpoint cap.
# ---------------------------------------------------------------------------
_ltp_counter = {'count': 0, 'limit': LTP_POLL_LIMIT, 'last_reset': 0.0}

def _check_ltp_limit():
    now = time.time()
    if now - _ltp_counter['last_reset'] > 1.0:
        _ltp_counter['count'] = 0
        _ltp_counter['last_reset'] = now
    if _ltp_counter['count'] >= _ltp_counter['limit']:
        time.sleep(1.1)
        _ltp_counter['count'] = 0
        _ltp_counter['last_reset'] = time.time()
        return
    _ltp_counter['count'] += 1


def fetch_ltp_rest(obj, exchange: str, symbol: str, token: str) -> float | None:
    """
    Single-attempt, rate-limited REST LTP fetch — used when the WS feed is
    disconnected. Returns None on failure rather than blocking/retrying, so
    callers in a monitoring loop (e.g. Iris._check_exit_conditions) keep their
    normal cadence instead of stalling on a slow/failing REST call; the next
    loop iteration tries again.
    """
    try:
        _check_ltp_limit()
        ltp = obj.ltpData(exchange, symbol, token)['data']['ltp']
        return float(ltp) if ltp is not None else None
    except Exception as e:
        logger.error(f'REST ltpData fallback failed for {symbol}: {e}')
        return None


def get_fill_price(obj, order_watcher: OrderFillWatcher, order_id: str,
                   symbol: str, token: str, lots: int,
                   dry_run: bool, feed) -> float | None:
    """
    Obtain fill price after order placement.

    dry_run=True (paper trading):
        Subscribes to option LTP feed, waits up to 3s for first tick,
        falls back to REST ltpData.

    dry_run=False (live):
        Fast path — polls OrderFillWatcher WS live_orders for averageprice.
        Falls back to REST orderBook polling if WS not ready or times out.

    Returns avg fill price, or None on failure (live only — caller should abort).
    """
    # Subscribe to option feed so LTP monitoring is ready regardless of path
    feed.subscribe_options([token])

    if dry_run:
        deadline = time.time() + 3
        while time.time() < deadline:
            ltp = feed.get_ltp(token)
            if ltp:
                return ltp
            time.sleep(0.1)
        # REST fallback — ltpData gives current market price
        try:
            ltp = float(obj.ltpData(FO_EXCHANGE, symbol, token)['data']['ltp'])
            logger.info(f'[PAPER] Entry price via REST ltpData: {ltp}')
            return ltp
        except Exception as e:
            logger.warning(f'[PAPER] ltpData fallback failed: {e}')
        return None

    # --- Live: WS fast path ---
    if order_watcher._ws_ready.is_set():
        deadline = time.time() + ORDER_TIMEOUT_SEC
        while time.time() < deadline:
            with order_watcher._lock:
                od = order_watcher.live_orders.get(str(order_id))
            if od:
                avg = float(od.get('averageprice') or 0.0)
                qty = int(od.get('filledshares') or 0)
                if qty > 0:
                    logger.info(f'Fill (WS): {symbol} avg={avg} qty={qty}')
                    return avg
            time.sleep(0.05)
        logger.warning(f'WS fill timeout for {order_id} ({symbol}) — falling back to REST.')
    else:
        logger.info('OrderFillWatcher WS not ready — using REST orderBook.')

    # --- Live: REST fallback ---
    deadline = time.time() + ORDER_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            book   = obj.orderBook()
            orders = book.get('data') or []
            for o in orders:
                if str(o.get('orderid')) == str(order_id):
                    if o.get('status') == 'complete':
                        avg = float(o.get('averageprice', 0))
                        logger.info(f'Fill (REST): {symbol} avg={avg}')
                        return avg
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
