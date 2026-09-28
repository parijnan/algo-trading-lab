"""
A scripted stand-in for SmartApi.SmartConnect. Nothing here can reach Angel One: P4 must never log in (Prometheus is live on
the same account and a second session evicts its order capability), so every live adapter is tested against this double.

It keeps an order book, a position list and a cash figure; `place_script` says what each placeOrderFullResponse call does:
  'ok'             order created and complete at once (filled at `price`)
  'reject'         broker refuses: {'message': 'RMS:...'}, no order
  'ghost'          order created, but the call raises DataException (the response was lost)
  'network'        NetworkException, no order created
  'ratelimit'      DataException 'exceeding access rate' (AB1021), no order
  'session'        Exception 'Invalid Token'
  ('open', n)      order created 'open'; complete after n orderBook reads
  'hold'           order created 'open' and never completes
  ('partial', k)   order ends 'cancelled' with k shares filled
"""
from datetime import datetime, timedelta

from hestia_core.gateway import DataException, NetworkException


class FakeClock:
    def __init__(self, base=datetime(2026, 9, 3, 12, 0, 0)):
        self.t, self.base = 0.0, base

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def wall_now(self):
        return self.base + timedelta(seconds=self.t)


class ScriptedSmartConnect:
    def __init__(self, clock: FakeClock, cash=1_000_000.0, price=100.0):
        self.clock, self.cash, self.price = clock, cash, price
        self.orders, self.position_rows, self.calls = [], [], []
        self.place_script = []
        self.book_reads = 0
        self.rms_failures = 0
        self.position_error = None
        self._seq = 0
        self._open_until = {}                     # orderid -> book reads needed

    def _new_order(self, params, status, filled, ts=None):
        self._seq += 1
        oid = f'{self._seq:012d}'
        row = {'orderid': oid, 'tradingsymbol': params['tradingsymbol'], 'symboltoken': params['symboltoken'],
               'transactiontype': params['transactiontype'], 'quantity': params['quantity'], 'status': status,
               'filledshares': str(filled), 'averageprice': self.price if filled else 0.0,
               'updatetime': (ts or self.clock.wall_now()).strftime('%d-%b-%Y %H:%M:%S')}
        self.orders.append(row)
        return row

    def placeOrderFullResponse(self, params):
        self.calls.append(('place', dict(params), self.clock.t))
        action = self.place_script.pop(0) if self.place_script else 'ok'
        kind, arg = (action if isinstance(action, tuple) else (action, None))
        qty = int(params['quantity'])
        if kind == 'ok':
            return {'message': 'SUCCESS', 'data': {'orderid': self._new_order(params, 'complete', qty)['orderid']}}
        if kind == 'reject':
            return {'message': 'RMS:Margin Exceeds', 'data': None}
        if kind == 'ghost':
            self._new_order(params, 'complete', qty)
            raise DataException('Couldn\'t parse the JSON response received from the server')
        if kind == 'network':
            raise NetworkException('Connection reset')
        if kind == 'ratelimit':
            raise DataException('Access denied because of exceeding access rate')
        if kind == 'session':
            raise Exception('Invalid Token')
        if kind == 'open':
            row = self._new_order(params, 'open', 0)
            self._open_until[row['orderid']] = self.book_reads + arg
            return {'message': 'SUCCESS', 'data': {'orderid': row['orderid']}}
        if kind == 'hold':
            row = self._new_order(params, 'open', 0)
            return {'message': 'SUCCESS', 'data': {'orderid': row['orderid']}}
        if kind == 'partial':
            row = self._new_order(params, 'cancelled', arg)
            return {'message': 'SUCCESS', 'data': {'orderid': row['orderid']}}
        raise AssertionError(kind)

    def complete_open_orders(self):
        for row in self.orders:
            if row['status'] == 'open':
                row.update(status='complete', filledshares=row['quantity'], averageprice=self.price)

    def orderBook(self):
        self.calls.append(('orderBook', None, self.clock.t))
        self.book_reads += 1
        for row in self.orders:
            need = self._open_until.get(row['orderid'])
            if row['status'] == 'open' and need is not None and self.book_reads >= need:
                row.update(status='complete', filledshares=row['quantity'], averageprice=self.price)
        return {'status': True, 'data': [dict(r) for r in self.orders]}

    def position(self):
        self.calls.append(('position', None, self.clock.t))
        if self.position_error:
            raise self.position_error
        return {'status': True, 'data': [dict(r) for r in self.position_rows]}

    def rmsLimit(self):
        self.calls.append(('rms', None, self.clock.t))
        if self.rms_failures:
            self.rms_failures -= 1
            raise Exception('AB1007 Invalid Token')
        return {'status': True, 'data': {'availablecash': str(self.cash)}}


class CandleSmartConnect:
    """Serves 1-minute candles from prepared frames as of `now_fn()` (a candle is available once its minute has closed), with
    scriptable failures: `fail(token, frm, to, now) -> bool` makes a call raise AB1021, `missing` drops specific minutes
    until removed, `ltps` and `circuit_limits` feed the quote calls."""

    def __init__(self, now_fn, frames, tz='+05:30'):
        self.now_fn, self.frames, self.tz = now_fn, frames, tz
        self.calls = []
        self.fail = None
        self.missing = set()
        self.ltps = {}
        self.circuit_limits = {}
        self.ltp_calls = 0

    def _rows(self, token, frm, to):
        now = self.now_fn()
        df = self.frames[str(token)]
        lo, hi = datetime.strptime(frm, '%Y-%m-%d %H:%M'), datetime.strptime(to, '%Y-%m-%d %H:%M')
        out = []
        for r in df[(df['time_stamp'] >= lo) & (df['time_stamp'] <= hi)].itertuples():
            ts = r.time_stamp.to_pydatetime()
            if ts + timedelta(minutes=1) <= now and (str(token), ts) not in self.missing:
                out.append([ts.strftime('%Y-%m-%dT%H:%M:%S') + self.tz, r.open, r.high, r.low, r.close, r.volume])
        return out

    def getCandleData(self, params):
        now = self.now_fn()
        self.calls.append((params['symboltoken'], params['fromdate'], params['todate'], now))
        if self.fail is not None and self.fail(params['symboltoken'], params['fromdate'], params['todate'], now):
            raise DataException('Access denied because of exceeding access rate')
        return {'status': True, 'data': self._rows(params['symboltoken'], params['fromdate'], params['todate'])}

    def ltpData(self, exchange, symbol, token):
        self.ltp_calls += 1
        return {'status': True, 'data': {'ltp': self.ltps.get(str(token))}}

    def getMarketData(self, mode, exchangeTokens):
        toks = [t for ts in exchangeTokens.values() for t in ts]
        uc, lc = self.circuit_limits.get(toks[0], (None, None))
        if uc is None:
            return {'status': True, 'data': {'fetched': []}}
        return {'status': True, 'data': {'fetched': [{'upperCircuit': uc, 'lowerCircuit': lc}]}}


class FakeFeed:
    """A FeedPort double."""

    def __init__(self):
        self.ltp, self.ohlc, self.age = {}, {}, {}
        self.subscribed = []

    def subscribe(self, tokens):
        self.subscribed += list(tokens)

    def unsubscribe(self, tokens):
        for t in tokens:
            if t in self.subscribed:
                self.subscribed.remove(t)

    def get_ltp(self, token):
        return self.ltp.get(token)

    def get_ohlc(self, token):
        return self.ohlc.pop(token, None)

    def last_tick_age(self, token):
        return self.age.get(token)
