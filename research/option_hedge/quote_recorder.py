"""
MCX option quote recorder (plans/mcx-option-hedging.md, Tier C): bid, ask, their sizes and the traded volume of the option chains a hedge
would use, at the times a hedge would be bought and sold (22:45 to the close, and the first 45 minutes of the session). Spread and size are
the two things Fyers's one-minute history cannot give, and only a forward record can.

The source is the Fyers data websocket (SymbolUpdate mode: top of book, last price, volume, total buy and sell quantity). It makes no REST
data requests of its own, so it cannot use up the Fyers request budget Hestia's candle source depends on; the only REST call is inside
the Fyers SDK (one symbol-resolution POST per subscribe), and the recorder subscribes twice per window.

A separate process: it never imports Hestia's live modules (only the pure MCX calendar), writes only under its own output and log
folders, reads the Fyers token file and nothing else of Hestia's, and never places an order. The token is for data only.

    python research/option_hedge/quote_recorder.py --plan      # today's windows, no network
    python research/option_hedge/quote_recorder.py             # record today (start before the first window; exits after the last)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import statistics
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

import recorder_configs as cfg                                  # noqa: E402
from hestia_core.mcx_market import MarketCalendar              # noqa: E402  (pure calendar; no logger, no network)

IST = ZoneInfo('Asia/Kolkata')
log = logging.getLogger('option_recorder')
AUTH_CODES = (-16, -8, -15, -17, 401, 403)


# ---- the day's windows ------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    name: str
    start: datetime
    end: datetime


def day_windows(cal: MarketCalendar, d: date) -> List[Window]:
    """The recording windows of one day (naive IST); none on a day both sessions are closed. On an evening-only day the session-open
    buckets run from the 17:00 open."""
    if cal.fully_closed(d):
        return []
    opening = cal.session_open(d)
    windows = [Window(name, opening + timedelta(minutes=a), opening + timedelta(minutes=b)) for name, a, b in cfg.OPEN_BUCKETS]
    name, start = cfg.CLOSE_BUCKET
    windows.append(Window(name, datetime.combine(d, start), cal.session_close(d)))
    return windows


# ---- the token (read only; the freshness rule of hestia_core/fyers_shadow.TokenGate) -------------------------------------------------

def read_token(path: Path, now: datetime) -> Tuple[Optional[str], str]:
    """(auth, reason): "app_id:access_token" when the file is mode 600, modified today, issued today and not expired; otherwise None and a
    reason that never contains the token."""
    try:
        st = path.stat()
    except OSError:
        return None, 'no token file'
    if st.st_mode & 0o077:
        return None, f'token file mode is {oct(st.st_mode & 0o777)}, not 600'
    if datetime.fromtimestamp(st.st_mtime, IST).date() != now.date():
        return None, 'token file not modified today'
    try:
        rec = json.loads(path.read_text())
        issued = datetime.fromisoformat(rec['issued_at']).astimezone(IST).replace(tzinfo=None)
        expires = datetime.fromisoformat(rec['expires_at']).astimezone(IST).replace(tzinfo=None)
        auth = f"{rec['app_id']}:{rec['access_token']}"
        if not rec['app_id'] or not rec['access_token']:
            raise KeyError('empty')
    except Exception as exc:                                    # noqa: BLE001
        return None, f'token file unreadable or malformed ({type(exc).__name__})'
    if issued.date() != now.date():
        return None, 'token not issued today'
    if now >= expires:
        return None, 'token expired'
    return auth, ''


# ---- the contracts ----------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Contract:
    symbol: str
    underlying: str
    kind: str                 # FUT | CE | PE
    expiry: date
    strike: float
    lot_qty: float
    prev_oi: float = 0.0      # yesterday's open interest as the symbol master lists it (the sockets carry no open interest)


def load_master(cache_dir: Path, today: date, fetch: Optional[Callable[[str], bytes]] = None,
                fallback: Path = cfg.MASTER_FALLBACK) -> Tuple[List[Contract], str]:
    """The Fyers MCX symbol master (a public file, not an API call): today's copy in the recorder's own folder, else a fresh download,
    else the pipeline's older copy. Returns the contracts of the recorded underlyings and where they came from."""
    cache = cache_dir / f'MCX_COM_sym_master_{today:%Y%m%d}.json'
    source = 'cache'
    if not cache.exists():
        try:
            data = (fetch or _download)(cfg.MASTER_URL)
            json.loads(data)
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
            source = 'download'
        except Exception as exc:                                # noqa: BLE001
            log.warning('symbol master download failed (%s); using %s', type(exc).__name__, fallback)
            cache, source = fallback, 'fallback'
    raw = json.loads(Path(cache).read_text())
    out = []
    for rec in raw.values():
        und = rec.get('exSymbol')
        if und not in cfg.UNDERLYINGS:
            continue
        ticker = rec.get('symTicker', '')
        kind = rec.get('optType') if rec.get('optType') in ('CE', 'PE') else ('FUT' if ticker.endswith('FUT') else None)
        if kind is None:
            continue
        try:
            expiry = datetime.fromtimestamp(int(rec['expiryDate']), IST).date()
        except (KeyError, ValueError):
            continue
        out.append(Contract(ticker, und, kind, expiry, float(rec.get('strikePrice', -1)), float(rec.get('qtyMultiplier') or 0),
                            float(rec.get('previousOi') or 0)))
    return out, source


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': cfg.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


# ---- the universe -----------------------------------------------------------------------------------------------------------------

def reference_futures(contracts: Sequence[Contract], today: date) -> Dict[Tuple[str, date], Contract]:
    """For each (underlying, option expiry) among the nearest unexpired expiries, the future the option is struck against: the nearest
    future expiring on or after the option's own expiry."""
    out = {}
    for und in cfg.UNDERLYINGS:
        opt_expiries = sorted({c.expiry for c in contracts if c.underlying == und and c.kind in ('CE', 'PE') and c.expiry >= today})
        futures = sorted((c for c in contracts if c.underlying == und and c.kind == 'FUT' and c.expiry >= today), key=lambda c: c.expiry)
        for expiry in opt_expiries[:cfg.EXPIRIES_PER_UNDERLYING]:
            ref = next((f for f in futures if f.expiry >= expiry), futures[-1] if futures else None)
            if ref is not None:
                out[(und, expiry)] = ref
    return out


@dataclass
class Universe:
    options: List[Contract] = field(default_factory=list)
    futures: Dict[str, str] = field(default_factory=dict)          # option symbol -> its reference future symbol
    fut_lp: Dict[str, float] = field(default_factory=dict)


def build_universe(contracts: Sequence[Contract], fut_lp: Dict[str, float], today: date, max_options: int) -> Universe:
    """Option symbols within BAND_PCT of their reference future, closest to the money first. The allowance is shared equally between the
    (underlying, expiry) pairs so that a fine strike ladder (crude) does not crowd out a coarse one (silver); allowance a pair cannot use
    (too few strikes in the band) goes round-robin to the others. A pair whose future has no price is skipped and logged."""
    refs = reference_futures(contracts, today)
    pools: List[List[Tuple[float, Contract, Contract]]] = []
    for (und, expiry), fut in sorted(refs.items()):
        price = fut_lp.get(fut.symbol)
        if not price:
            log.warning('no price for %s: %s %s skipped', fut.symbol, und, expiry)
            continue
        chain = [(abs(c.strike - price) / price * 100, c, fut) for c in contracts
                 if c.underlying == und and c.expiry == expiry and c.kind in ('CE', 'PE')]
        pools.append(sorted((x for x in chain if x[0] <= cfg.BAND_PCT), key=lambda x: (x[0], x[1].symbol)))
    chosen: List[Tuple[float, Contract, Contract]] = []
    if pools:
        quota = max_options // len(pools)
        leftovers = []
        for pool in pools:
            chosen.extend(pool[:quota])
            leftovers.append(pool[quota:])
        spare = max_options - len(chosen)
        while spare > 0 and any(leftovers):
            for rest in leftovers:
                if rest and spare > 0:
                    chosen.append(rest.pop(0))
                    spare -= 1
    uni = Universe(fut_lp=dict(fut_lp))
    for _, c, fut in sorted(chosen, key=lambda x: (x[1].underlying, x[1].expiry, x[1].kind, x[1].strike)):
        uni.options.append(c)
        uni.futures[c.symbol] = fut.symbol
    return uni


# ---- messages to rows -------------------------------------------------------------------------------------------------------------

TICK_FIELDS = ['ts', 'exch_time', 'symbol', 'bid_price', 'bid_size', 'ask_price', 'ask_size', 'ltp', 'last_traded_qty', 'vol_traded_today',
               'tot_buy_qty', 'tot_sell_qty', 'heartbeat']
QUOTE_KEY = ('bid_price', 'ask_price', 'bid_size', 'ask_size', 'ltp', 'vol_traded_today')


class ChangeFilter:
    """Which received messages are written: one whose top of book, last price or volume differs from what was last written for that
    symbol, and, for a symbol whose quote has not changed, one row every HEARTBEAT_S so that a quiet quote can be told from a silent feed."""

    def __init__(self):
        self.last_key: Dict[str, tuple] = {}
        self.last_written: Dict[str, datetime] = {}

    def accept(self, msg: dict, now: datetime) -> Optional[bool]:
        """None to drop the message, False to write it as a change, True to write it as a heartbeat."""
        sym = msg['symbol']
        key = tuple(msg.get(k) for k in QUOTE_KEY)
        if self.last_key.get(sym) != key:
            self.last_key[sym], self.last_written[sym] = key, now
            return False
        if (now - self.last_written[sym]).total_seconds() >= cfg.HEARTBEAT_S:
            self.last_written[sym] = now
            return True
        return None


def tick_row(now: datetime, msg: dict, heartbeat: bool) -> dict:
    exch = msg.get('exch_feed_time')
    return {'ts': now.isoformat(timespec='milliseconds'),
            'exch_time': datetime.fromtimestamp(exch, IST).replace(tzinfo=None).isoformat() if isinstance(exch, (int, float)) and exch else '',
            'symbol': msg['symbol'], 'bid_price': msg.get('bid_price'), 'bid_size': msg.get('bid_size'), 'ask_price': msg.get('ask_price'),
            'ask_size': msg.get('ask_size'), 'ltp': msg.get('ltp'), 'last_traded_qty': msg.get('last_traded_qty'),
            'vol_traded_today': msg.get('vol_traded_today'), 'tot_buy_qty': msg.get('tot_buy_qty'), 'tot_sell_qty': msg.get('tot_sell_qty'),
            'heartbeat': int(heartbeat)}


def append_rows(path: Path, fields: Sequence[str], rows: Sequence[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, 'a', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if new:
            writer.writeheader()
        writer.writerows(rows)


# ---- the socket -------------------------------------------------------------------------------------------------------------------

class SdkSocket:
    """The Fyers SDK data socket behind a small interface, so the recorder can be tested without the network. The SDK is imported when the
    socket is opened. Its own log goes to SDK_LOG_DIR. Closing the socket ends the subscriptions (no unsubscribe, which would cost another
    REST symbol-resolution call)."""

    def __init__(self):
        self._ws = None

    def open(self, auth: str, on_message, on_connect, on_error, on_close) -> None:
        from fyers_apiv3.FyersWebsocket import data_ws                       # noqa: PLC0415
        cfg.SDK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._ws = data_ws.FyersDataSocket(access_token=auth, log_path=str(cfg.SDK_LOG_DIR), litemode=False, write_to_file=False,
                                           reconnect=True, reconnect_retry=cfg.SDK_RECONNECT_RETRY, on_connect=on_connect,
                                           on_message=on_message, on_error=on_error, on_close=on_close)
        self._ws.connect()

    def subscribe(self, symbols: Sequence[str]) -> None:
        self._ws.subscribe(symbols=list(symbols), data_type='SymbolUpdate')

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close_connection()
            finally:
                self._ws = None


# ---- the recorder -----------------------------------------------------------------------------------------------------------------

@dataclass
class SymbolStats:
    received: int = 0
    written: int = 0
    two_sided: int = 0
    spreads: List[float] = field(default_factory=list)
    mids: List[float] = field(default_factory=list)


class Recorder:
    def __init__(self, out_dir: Path, calendar: MarketCalendar, socket_factory: Callable[[], object], token_file: Path,
                 clock: Callable[[], datetime], sleep: Callable[[float], None], fetch_master: Optional[Callable[[str], bytes]] = None,
                 master_fallback: Path = cfg.MASTER_FALLBACK):
        self.out_dir, self.calendar, self.socket_factory, self.token_file = Path(out_dir), calendar, socket_factory, Path(token_file)
        self.clock, self.sleep, self.fetch_master, self.master_fallback = clock, sleep, fetch_master, master_fallback
        self.today = clock().date()
        self.day_dir = self.out_dir / f'{self.today:%Y-%m-%d}'
        self.windows = day_windows(calendar, self.today)
        self.contracts: Optional[List[Contract]] = None
        self.stopped = ''
        self._last_note: Optional[str] = None

    # ---- the day
    def run(self) -> None:
        for window in self.windows:
            if self.clock() >= window.end or self.stopped:
                continue
            if not self._wait_until(window.start, window.end):
                continue
            self.record_window(window)

    def _wait_until(self, start: datetime, end: datetime) -> bool:
        while self.clock() < start:
            self.sleep(min(60.0, (start - self.clock()).total_seconds()))
        return self.clock() < end

    def _killed(self) -> bool:
        return (self.out_dir / cfg.OFF_FLAG_NAME).exists()

    def _note(self, message: str) -> None:
        if message != self._last_note:
            self._last_note = message
            if message:
                log.info(message)

    def _usable_token(self, window: Window) -> Optional[str]:
        """The auth string once the token is usable and the kill flag is absent, waiting (without any request) until the window ends."""
        while self.clock() < window.end:
            if self._killed():
                self._note('kill flag present: idle')
            else:
                auth, reason = read_token(self.token_file, self.clock())
                if auth is not None:
                    self._note('')
                    return auth
                self._note(f'token not usable: {reason}')
            self.sleep(cfg.TOKEN_RECHECK_S)
        return None

    def record_window(self, window: Window) -> None:
        log.info('window %s %s-%s', window.name, f'{window.start:%H:%M}', f'{window.end:%H:%M}')
        if self.contracts is None:
            self.contracts, source = load_master(self.out_dir / 'master', self.today, self.fetch_master, self.master_fallback)
            log.info('symbol master: %d contracts of %d underlyings (%s)', len(self.contracts), len(cfg.UNDERLYINGS), source)
        stats: Dict[str, SymbolStats] = {}
        restarts = 0
        while self.clock() < window.end and restarts <= cfg.MAX_RESTARTS_PER_WINDOW:
            auth = self._usable_token(window)
            if auth is None:
                break
            outcome = self._connection(window, auth, stats)
            if outcome != 'restart':
                break
            restarts += 1
            log.warning('window %s: connection restart %d of %d', window.name, restarts, cfg.MAX_RESTARTS_PER_WINDOW)
        self._summary(window, stats)

    # ---- one connection
    def _connection(self, window: Window, auth: str, stats: Dict[str, SymbolStats]) -> str:
        """'done' (the window ended), 'restart' (connect or feed failed) or 'abort' (kill flag or a refused token)."""
        inbox: Deque[Tuple[datetime, dict]] = deque()
        flags = {'connected': False, 'auth_failed': False}
        current: List[str] = []
        secret = auth.split(':', 1)[-1]

        def on_message(msg):
            inbox.append((self.clock(), msg))

        def on_connect():
            flags['connected'] = True
            if current:                                                  # a reconnect by the SDK: subscribe everything again
                log.warning('window %s: socket reconnected: subscribing %d symbols again (one REST symbol lookup)', window.name, len(current))
                sock.subscribe(current)

        def on_error(msg):
            text = str(msg).replace(secret, '***')[:200]
            code = msg.get('code') if isinstance(msg, dict) else None
            if code in AUTH_CODES:
                flags['auth_failed'] = True
            log.error('socket error: %s', text)

        def on_close(msg):
            log.info('socket closed: %s', str(msg)[:120])

        sock = self.socket_factory()
        try:
            sock.open(auth, on_message, on_connect, on_error, on_close)
            if not self._wait(lambda: flags['connected'], cfg.CONNECT_WAIT_S, window.end):
                log.error('window %s: no connection within %d s', window.name, cfg.CONNECT_WAIT_S)
                return 'restart'
            refs = reference_futures(self.contracts, self.today)
            futures = sorted({f.symbol for f in refs.values()})
            current.extend(futures)
            log.info('window %s: subscribing %d futures (one REST symbol lookup)', window.name, len(futures))
            sock.subscribe(futures)
            filt, fut_lp = ChangeFilter(), {}
            last_msg = [self.clock()]

            def pump() -> None:
                while inbox:
                    ts, msg = inbox.popleft()
                    last_msg[0] = ts
                    self._handle(window, ts, msg, filt, stats, fut_lp)

            if not self._wait(lambda: (pump(), all(s in fut_lp for s in futures))[1], cfg.FUTURES_WAIT_S, window.end):
                if not fut_lp:
                    log.error('window %s: no reference future priced within %d s', window.name, cfg.FUTURES_WAIT_S)
                    return 'restart'
                log.warning('window %s: only %d of %d reference futures priced', window.name, len(fut_lp), len(futures))
            uni = build_universe(self.contracts, fut_lp, self.today, cfg.MAX_SYMBOLS - len(futures))
            self._write_universe(window, uni)
            options = [c.symbol for c in uni.options]
            current.extend(options)
            log.info('window %s: subscribing %d options (one REST symbol lookup), %d symbols in all', window.name, len(options), len(current))
            sock.subscribe(options)
            stale_warned = False
            next_status = self.clock() + timedelta(seconds=cfg.STATUS_S)
            while self.clock() < window.end:
                self.sleep(cfg.DRAIN_S)
                pump()
                if self.clock() >= next_status:
                    next_status += timedelta(seconds=cfg.STATUS_S)
                    log.info('window %s status: %d of %d symbols heard, %d messages, %d rows written, last message %.0f s ago', window.name,
                             len(stats), len(current), sum(x.received for x in stats.values()), sum(x.written for x in stats.values()),
                             (self.clock() - last_msg[0]).total_seconds())
                if self._killed():
                    log.info('kill flag present: stopping the window')
                    return 'abort'
                if flags['auth_failed']:
                    log.error('Fyers refused the token: stopping the window')
                    return 'abort'
                silent = (self.clock() - last_msg[0]).total_seconds()
                if silent >= cfg.RESTART_AFTER_S:
                    log.error('window %s: feed silent for %d s: restarting the connection', window.name, silent)
                    return 'restart'
                if silent >= cfg.STALE_FEED_S and not stale_warned:
                    log.warning('window %s: feed silent for %d s', window.name, silent)
                    stale_warned = True
                elif silent < cfg.STALE_FEED_S:
                    stale_warned = False
            return 'done'
        finally:
            try:
                sock.close()
            except Exception:                                            # noqa: BLE001
                log.exception('closing the socket failed')

    def _wait(self, predicate: Callable[[], bool], seconds: float, deadline: datetime) -> bool:
        waited = 0.0
        while waited < seconds and self.clock() < deadline:
            if predicate():
                return True
            self.sleep(0.5)
            waited += 0.5
        return predicate()

    # ---- messages
    def _handle(self, window: Window, ts: datetime, msg, filt: ChangeFilter, stats: Dict[str, SymbolStats], fut_lp: Dict[str, float]) -> None:
        if not isinstance(msg, dict) or msg.get('type') != 'sf' or not msg.get('symbol'):
            if isinstance(msg, dict) and msg.get('type') in ('cn', 'sub', 'ful', 'unsub'):
                log.info('socket: %s %s', msg.get('type'), msg.get('message'))
            elif isinstance(msg, dict) and msg.get('s') == 'error':
                log.warning('socket message: %s', str(msg)[:200])
            return
        sym = msg['symbol']
        st = stats.setdefault(sym, SymbolStats())
        st.received += 1
        if sym.endswith('FUT') and msg.get('ltp'):
            fut_lp[sym] = msg['ltp']
        verdict = filt.accept(msg, ts)
        if verdict is None:
            return
        st.written += 1
        bid, ask = msg.get('bid_price') or 0, msg.get('ask_price') or 0
        if bid > 0 and ask > 0 and ask >= bid:
            st.two_sided += 1
            st.spreads.append(ask - bid)
            st.mids.append((ask + bid) / 2)
        append_rows(self.day_dir / 'ticks.csv', TICK_FIELDS, [tick_row(ts, msg, verdict)])

    def _write_universe(self, window: Window, uni: Universe) -> None:
        append_rows(self.day_dir / f'universe_{window.name}.csv',
                    ['symbol', 'underlying', 'kind', 'expiry', 'strike', 'fut_symbol', 'fut_lp', 'prev_oi'],
                    [{'symbol': c.symbol, 'underlying': c.underlying, 'kind': c.kind, 'expiry': c.expiry, 'strike': c.strike,
                      'fut_symbol': uni.futures[c.symbol], 'fut_lp': uni.fut_lp.get(uni.futures[c.symbol]), 'prev_oi': c.prev_oi}
                     for c in uni.options])

    def _summary(self, window: Window, stats: Dict[str, SymbolStats]) -> None:
        rows = []
        for sym, st in sorted(stats.items()):
            rows.append({'symbol': sym, 'received': st.received, 'written': st.written,
                         'two_sided_share': round(st.two_sided / st.written, 3) if st.written else None,
                         'median_spread': statistics.median(st.spreads) if st.spreads else None,
                         'median_spread_pct_of_mid': round(statistics.median(s / m * 100 for s, m in zip(st.spreads, st.mids)), 3) if st.spreads else None})
        append_rows(self.day_dir / f'summary_{window.name}.csv',
                    ['symbol', 'received', 'written', 'two_sided_share', 'median_spread', 'median_spread_pct_of_mid'], rows)
        log.info('window %s finished: %d symbols heard, %d rows written', window.name, len(stats), sum(s.written for s in stats.values()))


# ---- command line ---------------------------------------------------------------------------------------------------------------

def setup_logging(day: date) -> None:
    """Own logger, own file; nothing is shared with Hestia's loggers and nothing propagates to the root."""
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
    for handler in (logging.FileHandler(cfg.LOG_DIR / f'option_recorder_{day:%Y%m%d}.log'), logging.StreamHandler(sys.stdout)):
        handler.setFormatter(fmt)
        log.addHandler(handler)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--plan', action='store_true', help="print today's windows and exit (no network)")
    args = parser.parse_args(argv)
    now = datetime.now(IST).replace(tzinfo=None)
    calendar = MarketCalendar(cfg.HOLIDAYS_FILE)
    windows = day_windows(calendar, now.date())
    if args.plan:
        for w in windows:
            print(f'{w.name:8s} {w.start:%H:%M}-{w.end:%H:%M}')
        print(f'{len(windows)} windows; websocket only, up to {cfg.MAX_SYMBOLS} symbols (the Standard plan allows 100)')
        return 0
    if not windows:
        print('both sessions closed today: nothing to record')
        return 0
    setup_logging(now.date())
    log.info('option quote recorder starting: %d windows, up to %d symbols', len(windows), cfg.MAX_SYMBOLS)
    recorder = Recorder(cfg.OUT_DIR, calendar, SdkSocket, cfg.TOKEN_FILE, lambda: datetime.now(IST).replace(tzinfo=None), time.sleep)
    try:
        recorder.run()
    except Exception:                                                    # noqa: BLE001
        log.exception('recorder failed')
        return 1
    log.info('option quote recorder finished')
    return 0


if __name__ == '__main__':
    sys.exit(main())
