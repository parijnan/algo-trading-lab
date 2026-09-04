"""
Prometheus — MCX CRUDEOILM/CRUDEOIL intraday trend-following. Phase 3
(plans/prometheus-phase3-production.md): ST_15 single-timeframe, 2-lot
scale-out, positions can now span multiple sessions (no EOD flatten) and,
once §4-§9 are built, a contract rollover. Standalone cron entry, not
Leto-routed (different exchange, different underlying, no VIX coupling).

Lifecycle:
  Start  -> resolve effective contract -> seed ST_15 (private intraday
            cache + live gap-fetch, §15) -> arm (status=watching)
  Signal -> enter 2 lots (status=in_trade)
  Exit   -> lot1 target / lot2 target / stop_loss / trend_flip
  Stop   -> graceful teardown; an open position is LEFT OPEN as the normal
            case (§2, no EOD flatten) — a restart resumes monitoring it.

DRY_RUN is ON by default (DRY_RUN=True in prometheus_configs.py).
Set DRY_RUN=False only after Rollout steps 2-4 (plan) are complete:
  1. order-update WS verified for MCX
  2. backtest/live parity check
  3. DRY_RUN paper mode under real market conditions
"""
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from prometheus_configs import (
    DRY_RUN, DATA_DIR, FLAG_PATH, COMMAND_FLAG_PATH, PID_FILE, STATE_FILE, CREDS_FILE,
    REPO_ROOT, SYMBOL, LOT_SIZE, LOTS_PER_LEG, MCX_FO_WS_EXCHANGE_TYPE,
    MIN_ENTRY_TIME, SESSION_START_TIME,
    SESSION_END_TIME, ST_PERIOD, ST_MULTIPLIER,
    DYNAMIC_SIZING, STATIC_UNITS, MARGIN_PER_UNIT, TRADE_UPDATE_SEC, TRADES_FILE,
    TODAY_1M_CACHE_FILE, DEFERRED_BAR_CUTOFF_MIN,
    SEED_RETRY_ATTEMPTS, SEED_RETRY_INTERVAL_SEC,
)
from prometheus_state import PrometheusState, save_state, load_state
from prometheus_logger_setup import get_logger
from prometheus_functions import (
    compute_st, resolve_effective_contract, seed_st15, persist_15m_series,
    fetch_one_minute_window, _merge_and_save, clear_today_cache, read_today_cache,
    patch_opening_bar_if_artifact, fetch_crudeoil_opening_bar,
    resolve_thresholds, resolve_target2,
    place_order, get_fill_price_and_qty, OrderFillWatcher, fetch_ltp_rest,
    load_trade_counter, save_trade_counter, append_trade_log_row, append_cumulative_trade,
    check_no_active_strategies, mcx_fully_closed_today,
)

sys.path.insert(0, str(REPO_ROOT))
from websocket_feed import SharedFeed  # noqa: E402
try:
    from leto_config import (SLACK_TRADEBOT_CHANNEL, SLACK_TRADE_ALERTS,
                              SLACK_TRADE_UPDATES, SLACK_ERRORS_CHANNEL)
except ImportError:
    SLACK_TRADEBOT_CHANNEL = SLACK_TRADE_ALERTS = SLACK_TRADE_UPDATES = SLACK_ERRORS_CHANNEL = None

logger = get_logger('prometheus')


def _load_slack_token() -> str:
    try:
        creds = pd.read_csv(REPO_ROOT / 'data/user_credentials.csv')
        return str(creds.iloc[0]['slack_token'])
    except Exception:
        return ''


_SLACK_TOKEN = _load_slack_token()


def _tag(symbol_root: str) -> str:
    return f'*Prometheus [{symbol_root}]*'


def _slack(msg: str, channel=None) -> None:
    if channel is None:
        channel = SLACK_TRADE_ALERTS
    if not channel or not _SLACK_TOKEN:
        return
    try:
        from slack_sdk import WebClient
        import threading
        def _send():
            try:
                WebClient(token=_SLACK_TOKEN).chat_postMessage(channel=channel, text=msg)
            except Exception:
                pass
        threading.Thread(target=_send, daemon=True).start()
    except ImportError:
        pass


class Prometheus:
    def __init__(self, obj, auth_token: str, api_key: str, client_code: str):
        self.obj = obj
        self.auth_token = auth_token
        self._api_key = api_key
        self._client_code = client_code

        self.state = load_state()
        self.feed = None
        self.order_watcher = OrderFillWatcher()
        self._shutdown = False
        # Set by the KILL command handler so _teardown() knows NOT to
        # auto-exit an open position -- KILL's entire premise ("Control
        # dropped. Position remains OPEN.", slack_listener.py's own message)
        # contradicts _teardown()'s default behavior of auto-flattening
        # anything still open, since teardown otherwise has no way to tell
        # a deliberate hand-off apart from a normal end-of-session flatten.
        self._kill_no_exit = False

        self._contract = None      # resolved once at setup — §1's single authoritative source
        self._df_15m = None        # full seeded + accumulated 15-min ST series (in-memory)
        self._df_1m_today = pd.DataFrame(columns=['time_stamp', 'open', 'high', 'low', 'close', 'volume'])

        self._pending_recovery = []   # [(from_dt, to_dt)] — §3 outer non-blocking retry queue
        self._pending_15m_boundary = None   # §12: 15m boundary awaiting a complete 1-min window
        self._pending_15m_deadline = None   # §12: cutoff before building it from what's on hand
        self._opening_bar_checked = False   # §11: run patch_opening_bar_if_artifact once per session
        self._trade_counter = load_trade_counter()
        self._pending_trade_row = {}   # built in _execute_entry; reconstructed in _setup() on crash resume

        self._trade_count = 0
        self._total_pnl_rs = 0.0

        self._summary = {'strategy': 'Prometheus', 'symbol': SYMBOL, 'traded': False}

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info('Shutdown signal received.')
        self._shutdown = True

    # -----------------------------------------------------------------------
    # Sizing (§6) — Artemis's formula, not Apollo/Athena's dual-constraint one
    # -----------------------------------------------------------------------

    def _calculate_units(self) -> int:
        if not DYNAMIC_SIZING:
            logger.debug(f'Sizing: fixed STATIC_UNITS={STATIC_UNITS}')
            return STATIC_UNITS
        try:
            margin = float(self.obj.rmsLimit()['data']['availablecash'])
            units = max(1, int(margin // MARGIN_PER_UNIT))
            logger.info(f'Sizing: Available margin={margin:,.0f}  MARGIN_PER_UNIT={MARGIN_PER_UNIT:,}  '
                        f'Units={units}')
            return units
        except Exception as e:
            logger.warning(f'rmsLimit() failed ({e}) — falling back to STATIC_UNITS={STATIC_UNITS}')
            return STATIC_UNITS

    def _check_margin_sufficient(self, units: int) -> bool:
        """§6: general pre-entry margin check regardless of sizing mode — the
        primary defense against the tender-margin window is the early roll
        (§1), this is defense-in-depth against any other margin shift."""
        try:
            margin = float(self.obj.rmsLimit()['data']['availablecash'])
            required = units * MARGIN_PER_UNIT
            if margin < required:
                logger.error(f'Insufficient margin: available={margin:,.0f} required={required:,.0f} '
                            f'for {units} unit(s) — skipping entry.')
                _slack(f'⚠️ {_tag(self._contract["symbol_root"])}: insufficient margin '
                      f'(available Rs.{margin:,.0f}, need Rs.{required:,.0f}) — entry skipped.',
                      SLACK_ERRORS_CHANNEL)
                return False
            return True
        except Exception as e:
            logger.warning(f'Margin check failed ({e}) — proceeding without it.')
            return True

    # -----------------------------------------------------------------------
    # Circuit breaker (§5) — Prometheus's own dedicated flag, not the shared
    # SLACK_COMMAND.flag (an operator on NSE/BSE shouldn't accidentally kill MCX).
    # -----------------------------------------------------------------------

    def _check_command_flag(self) -> None:
        if not COMMAND_FLAG_PATH.exists():
            return
        try:
            command = COMMAND_FLAG_PATH.read_text().strip()
            tag = _tag(self._contract['symbol_root']) if self._contract else '*Prometheus*'

            if command == 'EXIT':
                msg = f'⚠️ {tag}: Slack `Exit Trade` detected. Liquidating...'
                logger.critical(msg.replace('*', ''))
                _slack(msg, SLACK_TRADE_ALERTS)
                if self.state.status == 'in_trade':
                    self._execute_exit_all('slack_exit')
                raise RuntimeError('Session terminated by Slack !exit command.')

            elif command == 'KILL':
                msg = f'\U0001f6a8 {tag}: Slack `Kill Switch` detected. Dropping control immediately.'
                logger.critical(msg.replace('*', ''))
                _slack(msg, SLACK_TRADE_ALERTS)
                self._kill_no_exit = True   # tell _teardown() not to auto-exit an open position
                raise RuntimeError('Session terminated by Slack !kill command.')

            # DISABLE: startup gate only, handled in main() before instantiation.

        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f'Error reading command flag: {e}')

    # -----------------------------------------------------------------------
    # Setup / teardown
    # -----------------------------------------------------------------------

    def _setup(self) -> bool:
        logger.info(f'Prometheus starting [DRY_RUN={DRY_RUN}] SYMBOL={SYMBOL}')

        self._contract = resolve_effective_contract(SYMBOL)
        tag = _tag(self._contract['symbol_root'])
        roll_note = ' (rolled early, tender-margin window)' if self._contract['rolled_early'] else ''
        logger.info(f'Effective contract: {self._contract["symbol"]} '
                    f'(token {self._contract["token"]}, expiry {self._contract["expiry_date"]:%Y-%m-%d})'
                    f'{roll_note}')
        _slack(f'{"[PAPER] " if DRY_RUN else ""}⚡ {tag} starting — '
              f'trading {self._contract["symbol"]}{roll_note}', SLACK_TRADEBOT_CHANNEL)

        # §15: seed_st15 is single-shot by default (no retry) -- a broker
        # hiccup during its live gap-fetch would otherwise block the whole
        # day's trading. _setup() runs before the main loop starts, before
        # any position exists and before _check_exit_conditions_ltp has
        # anything to monitor, so a blocking retry here is safe -- unlike
        # every other retry loop in this codebase, there's no concurrent
        # exit-check loop to starve.
        for attempt in range(1, SEED_RETRY_ATTEMPTS + 1):
            self._df_15m = seed_st15(self.obj, self._contract, datetime.now())
            if self._df_15m is not None and not self._df_15m.empty:
                break
            if attempt == 1:
                _slack(f'⚠️ {tag}: ST_15 seed failed (attempt {attempt}/{SEED_RETRY_ATTEMPTS}) — '
                      f'retrying every {SEED_RETRY_INTERVAL_SEC // 60} min.', SLACK_ERRORS_CHANNEL)
            logger.warning(f'ST_15 seed failed (attempt {attempt}/{SEED_RETRY_ATTEMPTS}).')
            if attempt < SEED_RETRY_ATTEMPTS:
                time.sleep(SEED_RETRY_INTERVAL_SEC)

        if self._df_15m is None or self._df_15m.empty:
            logger.critical(f'ST_15 seed failed after {SEED_RETRY_ATTEMPTS} attempts — cannot start.')
            _slack(f'\U0001f6a8 {tag}: seed failed after {SEED_RETRY_ATTEMPTS} attempts — cannot start.',
                  SLACK_ERRORS_CHANNEL)
            return False

        last = self._df_15m.iloc[-1]
        trend_str = ('bullish' if bool(last['trend']) else 'bearish') if not pd.isna(last['trend']) else 'warmup'
        _slack(f'{"[PAPER] " if DRY_RUN else ""}✅ {tag}: ST_15 seeded '
              f'({len(self._df_15m)} bars). Trend: {trend_str}.', SLACK_TRADEBOT_CHANNEL)

        # §15: seed_st15 already assembled + cached today's 1-min data
        # internally (private cache + live gap-fetch) — re-read that same
        # cache rather than the shared pipeline file, which never has
        # today's rows under this design.
        self._df_1m_today = read_today_cache(datetime.now())
        self._maybe_check_opening_bar()   # §11: covers both a fresh 09:00 start
                                           # (bar not there yet, no-op) and a
                                           # mid-day restart (bar already
                                           # present in the cache, check now
                                           # rather than never)

        feed_token = self.obj.getfeedToken()
        self.feed = SharedFeed()
        self.feed.start(
            auth_token=self.auth_token, api_key=self._api_key, client_code=self._client_code,
            feed_token=feed_token,
            alert_callback=lambda m: (_slack(f'⚠️ {tag}: {m}', SLACK_ERRORS_CHANNEL),
                                      logger.warning(m)),
        )
        # Deliberately NOT passed via startup_tokens: SharedFeed's
        # _subscribed_index bucket (what startup_tokens populates) drops
        # exchange_type on reconnect — resubscribe_all() hardcodes
        # EXCHANGE_NSE_CM for everything in it (websocket_feed.py:327-331),
        # which would silently resubscribe this MCX token under the wrong
        # exchange after a WS reconnect. subscribe_options's _subscribed_options
        # bucket tracks {token: exchange_type} per-token and resubscribes
        # correctly — used here for the one-time, permanent-for-the-session
        # subscription instead. Never paired with unsubscribe_options (see
        # get_fill_price_and_qty's docstring) — this token stays live the
        # whole session regardless of trade state.
        self.feed.subscribe_options([self._contract['token']], exchange_type=MCX_FO_WS_EXCHANGE_TYPE)

        if not DRY_RUN:
            self.order_watcher.start(auth_token=self.auth_token, api_key=self._api_key,
                                     client_code=self._client_code, feed_token=feed_token)

        if self.state.status == 'in_trade' and self.state.token:
            logger.info('Resuming in-trade state.')
            # No feed.subscribe_options() needed — the contract token is
            # already permanently subscribed via startup_tokens above (same
            # token every session, unlike Iris's per-trade option tokens).
            # Reconstruct the cumulative-tracker row from persisted state so a
            # crash mid-trade doesn't lose trade_id/entry fields when the
            # trade eventually closes (lot1/lot2 exit fields, if any were
            # already booked before the crash, are also carried forward).
            self._pending_trade_row = {
                'trade_id': self._trade_counter, 'contract_expiry': self.state.contract_expiry,
                'direction': self.state.direction, 'units': self.state.units,
                'entry_ts': self.state.entry_ts, 'entry_price': self.state.entry_price,
                'signal_ts': self.state.signal_ts, 'signal_close': self.state.signal_close,
                'entry_slippage_points': None,
                'sl_price': self.state.sl_price,
                'lot1_target': self.state.lot1_target, 'lot2_target': self.state.lot2_target,
                'lot2_target_source': self.state.lot2_target_source,
            }
            if self.state.lot1_status == 'booked':
                self._pending_trade_row.update({
                    'lot1_exit_ts': self.state.lot1_exit_ts, 'lot1_exit_price': self.state.lot1_exit_price,
                    'lot1_exit_reason': self.state.lot1_exit_reason,
                })
            if self.state.lot2_status == 'booked':
                self._pending_trade_row.update({
                    'lot2_exit_ts': self.state.lot2_exit_ts, 'lot2_exit_price': self.state.lot2_exit_price,
                    'lot2_exit_reason': self.state.lot2_exit_reason,
                })
            # §4: reconciliation gap flagged in the plan (crash between order
            # placement and fill confirmation) — not built; a resumed in-trade
            # state is trusted as-is, same as Iris/Athena today. Flag loudly.
            _slack(f'ℹ️ {tag}: resumed with an open position from a prior session '
                  f'({self.state.direction}, entry {self.state.entry_price}). State was NOT '
                  f'reconciled against the broker\'s order book — verify manually if in doubt.',
                  SLACK_TRADEBOT_CHANNEL)
        else:
            self.state.status = 'watching'
        save_state(self.state)
        logger.info('Setup complete — watchdog armed.')
        return True

    def _teardown(self) -> None:
        tag = _tag(self._contract['symbol_root']) if self._contract else '*Prometheus*'

        if self._kill_no_exit and self.state.status == 'in_trade':
            # KILL's entire premise (slack_listener.py's own message: "Control
            # dropped. Position remains OPEN.") is to hand the position off
            # untouched for manual management -- do not place any exit order,
            # do not change state. Only the feed/process itself shuts down.
            if self.feed:
                try:
                    self.feed.stop()
                except Exception:
                    pass
            logger.warning('Teardown after KILL — leaving open position untouched, per Kill Switch.')
            _slack(f'⏹ {tag}: stopped via Kill Switch. Position left OPEN and untouched, '
                  f'as promised — manage it manually, or restart Prometheus to resume monitoring it.',
                  SLACK_TRADEBOT_CHANNEL)
            self._send_session_report()
            return

        # §2 (Phase 3, 2026-09-04): no EOD flatten — a position is *expected*
        # to still be open at session end most days (§3: positions can span
        # a contract roll), so teardown's normal path is "leave it open,
        # stop the feed, save state, exit cleanly," not "force flat and go
        # idle" the way Phase 2 always did. The rare exits that DO need to
        # happen before the process exits (SL/target/trend_flip firing right
        # as the loop ends) are handled by their own existing paths
        # (_check_exit_conditions_ltp, _handle_new_15m_bar) during run()
        # itself — teardown never reaches in and flattens on its own
        # initiative any more.
        if self.feed:
            try:
                self.feed.stop()
            except Exception:
                pass

        clear_today_cache()   # tomorrow re-fetches fresh regardless of position state (§15)

        if self.state.status == 'in_trade':
            save_state(self.state)
            logger.info('Teardown with an open position — left OPEN, as designed (Phase 3, no EOD flatten).')
            _slack(f'{"[PAPER] " if DRY_RUN else ""}⏹ {tag} stopped. Position left OPEN — expected, '
                  f'not an error. A restart resumes monitoring it.', SLACK_TRADEBOT_CHANNEL)
            self._send_session_report()
            return

        self.state.status = 'idle'
        save_state(self.state)
        logger.info('Prometheus stopped.')
        _slack(f'{"[PAPER] " if DRY_RUN else ""}⏹ {tag} stopped.', SLACK_TRADEBOT_CHANNEL)
        self._send_session_report()

    def _send_session_report(self) -> None:
        """
        End-of-session Slack report — mirrors leto.py's _send_session_report
        style (divider lines, per-trade blocks, bold session total,
        #tradebot-updates) but reads prometheus_trades.csv (the cumulative
        tracker) for today's date rather than a single in-memory summary
        dict: unlike Leto's once-daily routed strategies, Prometheus can
        have several trades in one session (confirmed 2026-08-31: 2 trades
        in a single day), so a Leto-style "one outcome per strategy" shape
        doesn't fit -- this lists every trade plus a session total, and
        reports a still-open position (§2, Phase 3: no EOD flatten, so this
        is the NORMAL end-of-day shape on most days, not a defensive
        fallback for a rare unclosed position the way it was in Phase 2).
        Wrapped so a reporting failure never masks the actual teardown.
        """
        try:
            tag_root = self._contract['symbol_root'] if self._contract else SYMBOL
            day_str = datetime.now().strftime('%a %d %b %Y')
            lines = [f'\U0001f4ca *Prometheus [{tag_root}] — Session Report*  |  {day_str}', '']
            lines.append('━' * 37)

            today = datetime.now().date()
            trades_today = pd.DataFrame()
            if TRADES_FILE.exists():
                all_trades = pd.read_csv(TRADES_FILE, parse_dates=['entry_ts'])
                if not all_trades.empty:
                    trades_today = all_trades[all_trades['entry_ts'].dt.date == today]

            total_rs = 0.0
            traded = not trades_today.empty or self.state.status == 'in_trade'

            if not traded:
                lines.append('  ↳ No trade today')
                lines.append('')
            else:
                for _, t in trades_today.iterrows():
                    direction = str(t['direction']).capitalize()
                    entry_ts_str = pd.Timestamp(t['entry_ts']).strftime('%H:%M')
                    entry_price = t['entry_price']
                    exit_candidates = [pd.Timestamp(v) for v in
                                       (t.get('lot1_exit_ts'), t.get('lot2_exit_ts')) if pd.notna(v)]
                    exit_ts_str = max(exit_candidates).strftime('%H:%M') if exit_candidates else '—'
                    exit_reason = t.get('lot2_exit_reason') or t.get('lot1_exit_reason') or '?'
                    exit_str = str(exit_reason).replace('_', ' ').title()
                    pnl_pts = t.get('total_pnl_points') or 0
                    pnl_rs = t.get('total_pnl_rs') or 0
                    total_rs += pnl_rs

                    lines.append(f"*Trade #{int(t['trade_id'])}*  ·  {direction}  |  Units: {int(t['units'])}")
                    lines.append(f"  ↳ Entry: {entry_ts_str} @ {entry_price:.2f}   "
                                 f"Exit: {exit_ts_str}  ·  {exit_str}")
                    lines.append(f"  ↳ P&L        : *{pnl_pts:+.1f} pts  ({pnl_rs:+,.0f} Rs)*")
                    lines.append('')

                if self.state.status == 'in_trade':
                    direction = (self.state.direction or '?').capitalize()
                    entry_ts_str = (datetime.fromisoformat(self.state.entry_ts).strftime('%H:%M')
                                    if self.state.entry_ts else '?')
                    entry = self.state.entry_price or 0
                    ltp = self.state.last_known_ltp or entry
                    # §13 (2026-09-04): realised + unrealised, not unrealised
                    # alone — a still-open trade routinely has one lot
                    # already booked under Phase 3 (positions run days to
                    # weeks, §3), and that locked-in P&L was previously
                    # invisible here, silently understating the session total.
                    pnl = self._compute_trade_pnl(ltp)
                    total_rs += pnl['total_rs']

                    lines.append(f"*Open Position*  ·  {direction}  |  Units: {self.state.units}")
                    lines.append(f"  ↳ Entry: {entry_ts_str} @ {entry:.2f}   Still open at session end")
                    lines.append(f"  ↳ Realised   : {pnl['realised_pts']:+.1f} pts  ({pnl['realised_rs']:+,.0f} Rs)")
                    lines.append(f"  ↳ Unrealised : {pnl['unrealised_pts']:+.1f} pts  ({pnl['unrealised_rs']:+,.0f} Rs)")
                    lines.append(f"  ↳ P&L        : *{pnl['total_rs']:+,.0f} Rs*")
                    lines.append('')

            lines.append('━' * 37)
            lines.append(f'*Session Total  :  {total_rs:+,.0f} Rs*')
            lines.append('━' * 37)

            _slack('\n'.join(lines), SLACK_TRADEBOT_CHANNEL)
        except Exception as e:
            logger.error(f'_send_session_report failed: {e}')

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def run(self):
        if not self._setup():
            self._summary['no_trade_reason'] = 'Setup failed'
            return self._summary

        tag = _tag(self._contract['symbol_root'])
        session_end = datetime.now().replace(
            hour=int(SESSION_END_TIME.split(':')[0]), minute=int(SESSION_END_TIME.split(':')[1]),
            second=0, microsecond=0)
        last_update_ts = time.time()

        # Non-blocking 1-min boundary tracker — deliberately NOT a blocking
        # sleep-until-boundary (that pattern is right for a pure poller like
        # mcx_live_downloader.py, wrong here: it would starve the LTP-driven
        # exit checks below for up to 60s at a time, plan §3's "candle- vs
        # LTP-dependent logic stay two explicit blocks" requirement).
        next_boundary = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1)

        try:
            while not self._shutdown and FLAG_PATH.exists() and datetime.now() < session_end:
                self._check_command_flag()
                now = datetime.now()

                # ── In-trade: tight LTP-driven exit loop, every tick ────
                if self.state.status == 'in_trade':
                    self._check_exit_conditions_ltp(now)
                    if time.time() - last_update_ts >= TRADE_UPDATE_SEC:
                        self._send_trade_update()
                        last_update_ts = time.time()

                # ── 1-min boundary reached: poll, merge, and on a 15-min
                #    mark, recompute ST and check entry/trend-flip (§3) ──
                if now >= next_boundary:
                    boundary = next_boundary
                    self._recover_pending_windows()
                    win_to = datetime.now()
                    win_from = win_to - timedelta(minutes=5)
                    df = fetch_one_minute_window(self.obj, self._contract['token'], win_from, win_to)
                    if df is not None:
                        self._merge_1m(df)
                    else:
                        self._pending_recovery.append((win_from, win_to))

                    # §12 (2026-09-04): don't compute a 15m bar the instant
                    # its boundary tick arrives if the 1-min poller hasn't
                    # actually delivered all 15 minutes yet (an AB1021
                    # stretch, confirmed live 2026-09-03 to span ~60s) —
                    # wait, re-checking every 1-min cycle, up to
                    # DEFERRED_BAR_CUTOFF_MIN before building it from
                    # whatever's on hand with a loud warning. Minimizes
                    # flip-detection lag over fallback precision (user's
                    # call) — SL/target monitoring above is completely
                    # unaffected by the wait either way.
                    if boundary.minute % 15 == 0 and self._pending_15m_boundary is None:
                        self._pending_15m_boundary = boundary
                        self._pending_15m_deadline = boundary + timedelta(minutes=DEFERRED_BAR_CUTOFF_MIN)

                    if self._pending_15m_boundary is not None:
                        pb = self._pending_15m_boundary
                        window_start = pb - timedelta(minutes=15)
                        window = self._df_1m_today[(self._df_1m_today['time_stamp'] >= window_start) &
                                                   (self._df_1m_today['time_stamp'] < pb)]
                        complete = len(window) >= 15
                        past_cutoff = datetime.now() >= self._pending_15m_deadline
                        if complete or past_cutoff:
                            if not complete:
                                logger.warning(f'15m bar {window_start:%H:%M}-{pb:%H:%M} still '
                                               f'incomplete ({len(window)}/15) after '
                                               f'{DEFERRED_BAR_CUTOFF_MIN}min cutoff — building from '
                                               f'what is on hand.')
                                _slack(f'⚠️ {tag}: 15m bar {window_start:%H:%M}-{pb:%H:%M} still '
                                      f'incomplete ({len(window)}/15) after '
                                      f'{DEFERRED_BAR_CUTOFF_MIN}min — building anyway.',
                                      SLACK_ERRORS_CHANNEL)
                            self._handle_new_15m_bar(pb)
                            self._pending_15m_boundary = None
                            self._pending_15m_deadline = None

                    # §4: "one row per polling cycle" — the 1-min boundary,
                    # not the 0.5s exit-check tick (~24,000 rows/trade at
                    # tick rate vs. ~200 at poll rate for the average hold).
                    if self.state.status == 'in_trade':
                        ltp = self.state.last_known_ltp
                        if ltp:
                            self._append_running_row(ltp)

                    next_boundary += timedelta(minutes=1)

                time.sleep(0.5 if self.state.status == 'in_trade' else 1.0)

            if not FLAG_PATH.exists():
                logger.info('Circuit breaker flag removed externally — stopping.')
            elif datetime.now() >= session_end:
                logger.info(f'Session end ({SESSION_END_TIME}) reached — stopping.')

        except RuntimeError as e:
            if 'Session terminated' not in str(e):
                logger.error(f'Unhandled RuntimeError in Prometheus run loop: {e}')
                _slack(f'⚠️ {tag} EXCEPTION: {e}', SLACK_ERRORS_CHANNEL)
        except Exception as e:
            logger.error(f'Unhandled exception in Prometheus run loop: {e}')
            _slack(f'⚠️ {tag} EXCEPTION: {e}', SLACK_ERRORS_CHANNEL)
        finally:
            self._teardown()
        return self._summary

    def _recover_pending_windows(self) -> None:
        if not self._pending_recovery:
            return
        still_pending = []
        for (win_from, win_to) in self._pending_recovery:
            recovered = fetch_one_minute_window(self.obj, self._contract['token'], win_from, win_to)
            if recovered is not None:
                self._merge_1m(recovered)
                logger.info(f'Recovered pending window [{win_from} -> {win_to}]')
            else:
                still_pending.append((win_from, win_to))
        self._pending_recovery = still_pending

    def _merge_1m(self, new_df: pd.DataFrame) -> None:
        """§15 (2026-09-04): writes into Prometheus's own private intraday
        cache (TODAY_1M_CACHE_FILE) — NOT the shared file data_downloader_mcx.py
        maintains, which Prometheus never writes to any more — AND updates
        the in-memory today-accumulator used to build 15-min bars."""
        _merge_and_save(TODAY_1M_CACHE_FILE, new_df)

        working = new_df.copy()
        if working['time_stamp'].dt.tz is not None:
            working['time_stamp'] = working['time_stamp'].dt.tz_localize(None)
        today = datetime.now().date()
        new_today = working[working['time_stamp'].dt.date == today]
        if new_today.empty:
            return
        combined = pd.concat([self._df_1m_today, new_today], ignore_index=True)
        combined = combined.drop_duplicates(subset=['time_stamp'], keep='last')
        self._df_1m_today = combined.sort_values('time_stamp').reset_index(drop=True)
        self._maybe_check_opening_bar()

    def _maybe_check_opening_bar(self) -> None:
        """§11 (2026-09-04): once per session, the first time today's 09:00
        1-min row is available in self._df_1m_today, check it against
        CRUDEOIL's own 09:00 print for the thin-liquidity price-discovery
        artifact and patch it in place if OPENING_BAR_CORRECTION_ENABLED —
        in memory only, at 1-min-ingestion time, before it's ever read by
        any resample (compute_st's ATR window included). When the toggle
        is off, this still runs and logs what it WOULD have done, so the
        user can validate ST accuracy against the raw, uncorrected chart.
        """
        if self._opening_bar_checked or self._df_1m_today.empty:
            return
        session_start = pd.Timestamp(f'{datetime.now().date()} {SESSION_START_TIME}')
        mask = self._df_1m_today['time_stamp'] == session_start
        if not mask.any():
            return
        self._opening_bar_checked = True   # only ever attempt this once per session,
                                            # success or failure — never retried mid-session
        row = self._df_1m_today.loc[mask].iloc[0]
        m_bar = {'open': row['open'], 'high': row['high'], 'low': row['low'], 'close': row['close']}
        o_bar = fetch_crudeoil_opening_bar(self.obj, session_start)
        if o_bar is None:
            logger.warning('Opening-bar artifact check: CRUDEOIL reference unavailable — skipping.')
            return
        patched = patch_opening_bar_if_artifact(m_bar, o_bar)
        if patched is not m_bar:
            idx = self._df_1m_today.index[mask][0]
            for col in ('open', 'high', 'low', 'close'):
                self._df_1m_today.at[idx, col] = patched[col]

    # -----------------------------------------------------------------------
    # 15-min bar handling — flip detection, fresh entry, trend-flip exit+reentry
    # -----------------------------------------------------------------------

    def _handle_new_15m_bar(self, boundary: datetime) -> None:
        tag = _tag(self._contract['symbol_root'])
        window_start = boundary - timedelta(minutes=15)
        window = self._df_1m_today[(self._df_1m_today['time_stamp'] >= window_start) &
                                   (self._df_1m_today['time_stamp'] < boundary)]
        # A missing bar isn't a hypothetical: the 1-min poller's own pending-
        # recovery queue can still be catching up when a 15-min boundary
        # fires. Unlike seed_st15 (gap-checked, refuses to seed on any hole),
        # there's no way to "refuse" a live boundary — the bar either gets
        # built from what's on hand or the ST series silently gets a gap
        # compute_st then computes across as if contiguous. Alert loudly
        # either way rather than let it pass unnoticed (matches this
        # codebase's "no silent staleness" convention throughout §1/§3).
        if window.empty:
            logger.error(f'No 1-min bars for the {window_start:%H:%M}-{boundary:%H:%M} window — '
                        f'15m bar SKIPPED, leaving a gap in the ST series.')
            _slack(f'\U0001f6a8 {tag}: no 1-min data for {window_start:%H:%M}-{boundary:%H:%M} — '
                  f'15m bar skipped, ST series has a gap.', SLACK_ERRORS_CHANNEL)
            return
        if len(window) < 8:   # < ~half the expected 15 rows
            logger.warning(f'Only {len(window)}/15 1-min bars for {window_start:%H:%M}-{boundary:%H:%M} — '
                           f'building the 15m bar from incomplete data.')
            _slack(f'⚠️ {tag}: only {len(window)}/15 1-min bars for {window_start:%H:%M}-'
                  f'{boundary:%H:%M} — 15m bar built from incomplete data.', SLACK_ERRORS_CHANNEL)

        new_bar = pd.DataFrame([{
            'time_stamp': window_start, 'open': window['open'].iloc[0],
            'high': window['high'].max(), 'low': window['low'].min(),
            'close': window['close'].iloc[-1], 'volume': window['volume'].sum(),
        }])
        combined = pd.concat([self._df_15m, new_bar], ignore_index=True)
        combined = combined.drop_duplicates(subset=['time_stamp'], keep='last').sort_values('time_stamp')
        self._df_15m = compute_st(combined.reset_index(drop=True), ST_PERIOD, ST_MULTIPLIER)
        persist_15m_series(self._df_15m)

        row = self._df_15m[self._df_15m['time_stamp'] == window_start]
        if row.empty:
            return
        bar = row.iloc[-1]
        if pd.isna(bar['trend']):
            return

        flip = bool(bar['trend_flip'])
        direction_now = 'bullish' if bool(bar['trend']) else 'bearish'
        tag = _tag(self._contract['symbol_root'])

        if flip:
            logger.info(f'15m bar {window_start:%H:%M} — ST={bar["supertrend"]:.2f} '
                       f'close={bar["close"]:.2f}  FLIP -> {direction_now}')
            _slack(f'\U0001f504 {tag}: ST_15 flip -> *{direction_now}* at {window_start:%H:%M} '
                  f'(close={bar["close"]:.2f}, ST={bar["supertrend"]:.2f})', SLACK_TRADEBOT_CHANNEL)
        else:
            logger.info(f'15m bar {window_start:%H:%M} — ST={bar["supertrend"]:.2f} '
                       f'close={bar["close"]:.2f}  no flip')

        if self.state.status == 'in_trade':
            if flip and direction_now != self.state.direction:
                # Rule 7: this flip closes the remaining lot(s) AND is itself
                # the entry for the opposite direction — same-moment resolution.
                # Only fire the re-entry if the exit was genuinely confirmed
                # closed -- firing a fresh opposite-direction entry while the
                # exit itself failed to confirm would mean attempting to hold
                # both directions against a position with an unknown real
                # state (the exact incident this whole confirmation chain
                # was added to prevent, 2026-08-31).
                closed = self._execute_exit_all('trend_flip')
                if closed and now_after_min_entry():
                    self._execute_entry(direction_now, window_start, bar['close'])
                elif not closed:
                    logger.critical('Rule 7 re-entry SKIPPED — trend-flip exit did not confirm closed.')
            return

        # status == 'watching' — fresh entry detection. §2 (Phase 3): no
        # LAST_ENTRY_TIME cutoff before close any more — configs_p3.py never
        # gated entries near close (positional, nothing to "hold until").
        if flip and now_after_min_entry():
            self._execute_entry(direction_now, window_start, bar['close'])

    # -----------------------------------------------------------------------
    # Entry
    # -----------------------------------------------------------------------

    def _execute_entry(self, direction: str, signal_ts: datetime, signal_close: float) -> None:
        tag = _tag(self._contract['symbol_root'])
        ltp = self.feed.get_ltp(self._contract['token'])
        if not ltp:
            logger.error('LTP unavailable — cannot enter.')
            return

        units = self._calculate_units()
        if not self._check_margin_sufficient(units):
            return

        requested_lots = units * LOTS_PER_LEG * 2
        order_ids = place_order(self.obj, 'BUY' if direction == 'bullish' else 'SELL',
                                self._contract['symbol'], self._contract['token'],
                                requested_lots, DRY_RUN, self._contract['freeze_qty'])
        if not order_ids:
            logger.error('Entry order failed.')
            return

        fill_price, filled_lots = get_fill_price_and_qty(
            self.obj, self.order_watcher, order_ids, self._contract['symbol'],
            self._contract['token'], requested_lots, DRY_RUN, self.feed)
        if fill_price is None or filled_lots == 0:
            logger.error('Entry fill verification failed — no position opened.')
            _slack(f'\U0001f6a8 {tag}: entry order failed to fill — no position opened.', SLACK_ERRORS_CHANNEL)
            return

        lot1_lots = min(filled_lots, units * LOTS_PER_LEG)
        lot2_lots = max(0, filled_lots - lot1_lots)
        if filled_lots < requested_lots:
            logger.warning(f'Partial fill: requested {requested_lots} lots, filled {filled_lots} '
                          f'(lot1={lot1_lots}, lot2={lot2_lots}).')
            _slack(f'⚠️ {tag}: partial fill — requested {requested_lots} lots, got '
                  f'{filled_lots}. lot2 {"never opens" if lot2_lots == 0 else "opens at reduced size"}.',
                  SLACK_ERRORS_CHANNEL)

        entry_price = fill_price
        lot1_distance, sl_distance = resolve_thresholds(entry_price)
        lot1_target = entry_price + lot1_distance if direction == 'bullish' else entry_price - lot1_distance
        sl_price = (entry_price - sl_distance if direction == 'bullish'
                   else entry_price + sl_distance) if sl_distance is not None else None
        lot2_target, lot2_source = resolve_target2(entry_price, direction)

        now = datetime.now()
        self._trade_counter += 1
        save_trade_counter(self._trade_counter)

        self.state = PrometheusState(
            status='in_trade', direction=direction, units=units,
            entry_price=entry_price, entry_ts=now.isoformat(),
            signal_ts=signal_ts.isoformat(), signal_close=signal_close,
            contract_expiry=self._contract['expiry_date'].strftime('%Y-%m-%d'),
            symbol=self._contract['symbol'], token=self._contract['token'],
            sl_price=sl_price,
            lot1_target=lot1_target, lot1_lots=lot1_lots,
            lot1_status='open' if lot1_lots > 0 else 'never_opened',
            lot2_target=lot2_target, lot2_target_source=lot2_source, lot2_lots=lot2_lots,
            lot2_status='open' if lot2_lots > 0 else 'never_opened',
            last_known_ltp=entry_price,
        )
        save_state(self.state)

        self._trade_count += 1
        self._summary.update({'traded': True, 'direction': direction, 'units': units,
                              'entry_time': now.strftime('%H:%M')})

        entry_slippage = (entry_price - signal_close) if direction == 'bullish' else (signal_close - entry_price)
        self._pending_trade_row = {
            'trade_id': self._trade_counter, 'contract_expiry': self.state.contract_expiry,
            'direction': direction, 'units': units, 'entry_ts': now.isoformat(), 'entry_price': entry_price,
            'signal_ts': signal_ts.isoformat(), 'signal_close': signal_close,
            'entry_slippage_points': round(entry_slippage, 2),
            'sl_price': round(sl_price, 2) if sl_price is not None else None,
            'lot1_target': round(lot1_target, 2), 'lot2_target': round(lot2_target, 2),
            'lot2_target_source': lot2_source,
        }

        sl_str = f'{sl_price:.2f}' if sl_price is not None else 'n/a'
        msg = (f'{"[PAPER] " if DRY_RUN else ""}⚡ {tag}: Entered {direction.upper()}\n'
              f'{self.state.symbol} | Units: {units} ({filled_lots} lots)\n'
              f'Entry: {entry_price:.2f} | SL: {sl_str}\n'
              f'Lot1 target: {lot1_target:.2f} | Lot2 target: {lot2_target:.2f} ({lot2_source})')
        logger.info(msg.replace('\n', '  '))
        _slack(msg)

    # -----------------------------------------------------------------------
    # Exit — priority: stop_loss -> lot1 target -> lot2 target -> trend_flip
    # (SL wins any same-tick tie against a target — backtest_p2.py convention).
    # §2 (Phase 3): no EOD-squareoff tier any more — a position is expected
    # to carry overnight/across days; matches configs_p3.py, which never
    # had an EOD exit calibrated into it.
    # -----------------------------------------------------------------------

    def _get_ltp(self) -> float:
        # §3 (2026-09-04): state.token, not self._contract['token'] — while
        # in_trade, every price read keys off the position's own token, not
        # whichever contract is currently resolved as "effective" (these can
        # diverge across a rollover, not yet built — this fix lands ahead of
        # that so the invariant is already in place when it does).
        if self.feed is not None and self.feed.is_connected():
            ltp = self.feed.get_ltp(self.state.token)
            if ltp is not None:
                return ltp
        return fetch_ltp_rest(self.obj, self.state.symbol, self.state.token)

    def _check_exit_conditions_ltp(self, now: datetime) -> None:
        ltp = self._get_ltp()
        if ltp:
            self.state.last_known_ltp = ltp
            save_state(self.state)

        if not ltp:
            return

        direction = self.state.direction
        sl = self.state.sl_price
        if sl is not None:
            hit = (ltp <= sl) if direction == 'bullish' else (ltp >= sl)
            if hit:
                self._execute_exit_all('stop_loss')
                return

        if self.state.lot1_status == 'open':
            hit = (ltp >= self.state.lot1_target) if direction == 'bullish' else (ltp <= self.state.lot1_target)
            if hit:
                self._execute_exit_lot(1, 'target1', ltp)

        if self.state.status == 'in_trade' and self.state.lot2_status == 'open':
            hit = (ltp >= self.state.lot2_target) if direction == 'bullish' else (ltp <= self.state.lot2_target)
            if hit:
                self._execute_exit_lot(2, f'target2_{self.state.lot2_target_source}', ltp)

        if self.state.status == 'in_trade' and self.state.lot1_status != 'open' and self.state.lot2_status != 'open':
            self._finalize_trade()

    def _execute_exit_lot(self, lot_num: int, reason: str, ltp: float) -> bool:
        """
        Returns True only if the exit is genuinely confirmed (real orderid,
        real fill, filled > 0). On any failure, lot{n}_status is left
        untouched (still 'open') and NOTHING about this lot is marked
        closed -- no fabricated fill price, no P&L, no Slack success
        message. This is a deliberate, hard invariant, added after a real
        incident (2026-08-31): the previous code placed an order that
        failed (orderid=None), then silently used the current LTP as a fake
        "fill price" when fill resolution predictably timed out, marking
        both lots closed and returning to 'watching' -- while the real
        position stayed fully open at the broker, completely unmonitored,
        for ~28 minutes until caught manually. Returning False here instead
        leaves lot_status=='open', so the NEXT tick's exit-condition check
        (every 0.5-1s) retries automatically -- no separate retry loop
        needed, and no lie enters the state file or trade log.
        """
        lots = self.state.lot1_lots if lot_num == 1 else self.state.lot2_lots
        if not lots:
            return True   # nothing to exit -- vacuously done, not a failure
        tag = _tag(self._contract['symbol_root'])
        order_ids = place_order(self.obj, 'SELL' if self.state.direction == 'bullish' else 'BUY',
                                self.state.symbol, self.state.token, lots, DRY_RUN,
                                self._contract['freeze_qty'])
        if not order_ids:
            logger.critical(f'Lot{lot_num} exit order FAILED to place ({reason}) — '
                            f'position may still be OPEN at the broker.')
            _slack(f'\U0001f6a8 {tag}: Lot{lot_num} exit order FAILED to place ({reason}). '
                  f'Position may still be OPEN at the broker — will keep retrying automatically, '
                  f'but check the broker terminal manually now.', SLACK_ERRORS_CHANNEL)
            return False

        fill_price, filled = get_fill_price_and_qty(
            self.obj, self.order_watcher, order_ids, self.state.symbol, self.state.token,
            lots, DRY_RUN, self.feed)
        if fill_price is None or filled == 0:
            logger.critical(f'Lot{lot_num} exit order placed (orderid(s)={order_ids}, {reason}) but fill '
                            f'could NOT be confirmed (WS and REST both exhausted) — position status '
                            f'unknown, may still be open.')
            _slack(f'\U0001f6a8 {tag}: Lot{lot_num} exit order placed (orderid(s)={order_ids}) but fill '
                  f'unconfirmed — position status UNKNOWN. Check the broker terminal manually now; '
                  f'will keep retrying automatically.', SLACK_ERRORS_CHANNEL)
            return False

        entry = self.state.entry_price
        pnl_pts = (fill_price - entry) if self.state.direction == 'bullish' else (entry - fill_price)
        pnl_rs = round(pnl_pts * lots * LOT_SIZE, 2)
        self._total_pnl_rs += pnl_rs
        now = datetime.now()

        if lot_num == 1:
            self.state.lot1_status, self.state.lot1_exit_price = 'booked', round(fill_price, 2)
            self.state.lot1_exit_ts, self.state.lot1_exit_reason = now.isoformat(), reason
            self._pending_trade_row.update({
                'lot1_exit_ts': now.isoformat(), 'lot1_exit_price': round(fill_price, 2),
                'lot1_exit_reason': reason, 'lot1_pnl_points': round(pnl_pts, 2), 'lot1_pnl_rs': pnl_rs,
            })
        else:
            self.state.lot2_status, self.state.lot2_exit_price = 'booked', round(fill_price, 2)
            self.state.lot2_exit_ts, self.state.lot2_exit_reason = now.isoformat(), reason
            self._pending_trade_row.update({
                'lot2_exit_ts': now.isoformat(), 'lot2_exit_price': round(fill_price, 2),
                'lot2_exit_reason': reason, 'lot2_pnl_points': round(pnl_pts, 2), 'lot2_pnl_rs': pnl_rs,
            })
        save_state(self.state)
        # Final logged row for this lot's close, exit_reason stamped —
        # written here (status is still 'in_trade' at this point) rather
        # than relying on the next 1-min boundary, which for a trend-flip
        # close+reentry (rule 7) can land after _finalize_trade has already
        # reset self.state to 'watching', losing this trade's last row.
        self._append_running_row(fill_price, exit_reason=f'lot{lot_num}_{reason}')

        msg = (f'{"[PAPER] " if DRY_RUN else ""}✅ {tag}: Lot{lot_num} exit — {reason}\n'
              f'Entry {entry:.2f} -> Exit {fill_price:.2f} | P&L: {pnl_pts:+.2f} pts  Rs.{pnl_rs:+,.0f}')
        logger.info(msg.replace('\n', '  '))
        _slack(msg)

        if self.state.lot1_status != 'open' and self.state.lot2_status != 'open':
            self._finalize_trade()
        return True

    def _execute_exit_all(self, reason: str) -> bool:
        """
        stop_loss / trend_flip / slack_exit — closes whichever lot(s)
        remain open, at the SAME reason. §2 (Phase 3): no more eod_squareoff
        or teardown-triggered shutdown reasons — a position left open at
        session end is the normal case now, not something teardown exits.
        Returns True only if the position ended up fully closed (status
        back to 'watching').
        Callers that chain an action after this — specifically rule 7's
        same-tick trend-flip re-entry in _handle_new_15m_bar — MUST check
        this before proceeding: firing a fresh entry in the opposite
        direction while the exit itself failed to confirm would mean
        attempting to hold both directions at once against a position we
        already don't have a confirmed read on.
        """
        if self.state.status != 'in_trade':
            return True   # already flat -- vacuously true, not a failure
        ltp = self._get_ltp() or self.state.last_known_ltp or self.state.entry_price
        if self.state.lot1_status == 'open':
            self._execute_exit_lot(1, reason, ltp)
        if self.state.status == 'in_trade' and self.state.lot2_status == 'open':
            self._execute_exit_lot(2, reason, ltp)
        # No separate finalize call here -- _execute_exit_lot already
        # finalizes internally with the correct guard (both lot statuses
        # genuinely non-open). A second, looser "if still in_trade, finalize
        # anyway" check here was the exact same class of bug this whole fix
        # addresses: it force-closed the trade whenever status was still
        # 'in_trade' regardless of WHY (including both lots having just
        # failed to exit), the same blind trust in appearances that caused
        # the 2026-08-31 incident.
        return self.state.status != 'in_trade'

    def _finalize_trade(self) -> None:
        tag = _tag(self._contract['symbol_root'])
        lot1_pts = self._pending_trade_row.get('lot1_pnl_points', 0) or 0
        lot2_pts = self._pending_trade_row.get('lot2_pnl_points', 0) or 0
        lot1_rs = self._pending_trade_row.get('lot1_pnl_rs', 0) or 0
        lot2_rs = self._pending_trade_row.get('lot2_pnl_rs', 0) or 0
        self._pending_trade_row['total_pnl_points'] = round(lot1_pts + lot2_pts, 2)
        self._pending_trade_row['total_pnl_rs'] = round(lot1_rs + lot2_rs, 2)
        append_cumulative_trade(self._pending_trade_row)

        msg = (f'{"[PAPER] " if DRY_RUN else ""}\U0001f4ca {tag}: Trade #{self._trade_counter} closed. '
              f'Total P&L: {self._pending_trade_row["total_pnl_points"]:+.2f} pts  '
              f'Rs.{self._pending_trade_row["total_pnl_rs"]:+,.0f}')
        logger.info(msg)
        _slack(msg)

        # No feed.unsubscribe_options() here — the contract token stays
        # subscribed for the whole session regardless of trade state (see
        # get_fill_price_and_qty's docstring); unsubscribing it would have
        # silently killed LTP for rule 7's same-tick re-entry below.

        # Clear trade fields on state (matches iris.py's _execute_exit
        # cleanup) — a fresh entry always builds a new PrometheusState from
        # scratch regardless, so this is about keeping the CSV's watching-mode
        # rows from showing stale prior-trade data, not correctness.
        self.state = PrometheusState(status='watching')
        save_state(self.state)
        self._pending_trade_row = {}

    # -----------------------------------------------------------------------
    # Running trade log (§4) + periodic Slack update
    # -----------------------------------------------------------------------

    def _append_running_row(self, ltp: float, exit_reason: str = None) -> None:
        if self.state.status != 'in_trade' or not self.state.entry_ts:
            return
        entry_ts = datetime.fromisoformat(self.state.entry_ts)
        entry = self.state.entry_price
        direction = self.state.direction

        def _lot_pnl(target, status, exit_price, lots):
            if not lots:
                return 0.0, 0.0
            if status == 'booked' and exit_price is not None:
                pts = (exit_price - entry) if direction == 'bullish' else (entry - exit_price)
            else:
                pts = (ltp - entry) if direction == 'bullish' else (entry - ltp)
            return round(pts, 2), round(pts * lots * LOT_SIZE, 2)

        lot1_pts, lot1_rs = _lot_pnl(self.state.lot1_target, self.state.lot1_status,
                                     self.state.lot1_exit_price, self.state.lot1_lots)
        lot2_pts, lot2_rs = _lot_pnl(self.state.lot2_target, self.state.lot2_status,
                                     self.state.lot2_exit_price, self.state.lot2_lots)

        row = {
            'ts': datetime.now().isoformat(),
            'bars_since_entry': int((datetime.now() - entry_ts).total_seconds() // 60),
            'ltp': ltp, 'sl_price': self.state.sl_price,
            'lot1_target': self.state.lot1_target, 'lot2_target': self.state.lot2_target,
            'lot1_pnl_points': lot1_pts, 'lot1_pnl_rs': lot1_rs,
            'lot2_pnl_points': lot2_pts, 'lot2_pnl_rs': lot2_rs,
            'total_pnl_points': round(lot1_pts + lot2_pts, 2), 'total_pnl_rs': round(lot1_rs + lot2_rs, 2),
            'exit_reason': exit_reason,
        }
        append_trade_log_row(self._trade_counter, entry_ts, row)

    def _compute_trade_pnl(self, ltp: float) -> dict:
        """§13 (2026-09-04): realised (booked lots) + unrealised (still-open
        lots) + total — this is exactly what _finalize_trade/
        _append_running_row already compute per lot, just needed here
        un-conditioned on both lots being closed. Shared by _send_trade_update
        and _send_session_report's "still open" fallback so both report the
        same three numbers the same way.

        Real, currently-existing gap this fixes: once lot1 books, its P&L
        previously vanished from every subsequent update (both call sites
        computed P&L only from still-open lots, labeled "unrealised") — a
        trade mid-way through its scale-out silently understated its true
        total by exactly the already-booked amount. In Phase 2 this was a
        brief transitional window (position open at most one session); in
        Phase 3 a position can run for days to weeks, so it's the normal
        shape of a trade for most of its life, not an edge case.
        """
        entry = self.state.entry_price or 0
        direction = self.state.direction

        def _lot_realised(exit_price, status, lots):
            if status != 'booked' or exit_price is None or not lots:
                return 0.0, 0.0
            pts = (exit_price - entry) if direction == 'bullish' else (entry - exit_price)
            return pts, pts * lots * LOT_SIZE

        def _lot_unrealised(status, lots):
            if status != 'open' or not lots or not ltp:
                return 0.0, 0.0
            pts = (ltp - entry) if direction == 'bullish' else (entry - ltp)
            return pts, pts * lots * LOT_SIZE

        r1_pts, r1_rs = _lot_realised(self.state.lot1_exit_price, self.state.lot1_status, self.state.lot1_lots)
        r2_pts, r2_rs = _lot_realised(self.state.lot2_exit_price, self.state.lot2_status, self.state.lot2_lots)
        u1_pts, u1_rs = _lot_unrealised(self.state.lot1_status, self.state.lot1_lots)
        u2_pts, u2_rs = _lot_unrealised(self.state.lot2_status, self.state.lot2_lots)

        realised_rs, unrealised_rs = r1_rs + r2_rs, u1_rs + u2_rs
        return {
            'realised_pts': round(r1_pts + r2_pts, 2), 'realised_rs': round(realised_rs, 2),
            'unrealised_pts': round(u1_pts + u2_pts, 2), 'unrealised_rs': round(unrealised_rs, 2),
            'total_rs': round(realised_rs + unrealised_rs, 2),
        }

    def _send_trade_update(self) -> None:
        if self.state.status != 'in_trade':
            return
        ltp = self.state.last_known_ltp or 0
        entry = self.state.entry_price or 0
        direction = self.state.direction
        pnl = self._compute_trade_pnl(ltp)
        tag = _tag(self._contract['symbol_root'])
        prefix = '[PAPER] ' if DRY_RUN else ''
        msg = (f'{prefix}\U0001f4ca {tag} update: {direction.upper()}  {self.state.symbol}  '
              f'Entry: {entry:.2f}  LTP: {ltp:.2f}\n'
              f'Realised: {pnl["realised_pts"]:+.2f} pts (Rs.{pnl["realised_rs"]:+,.0f})  '
              f'Unrealised: {pnl["unrealised_pts"]:+.2f} pts (Rs.{pnl["unrealised_rs"]:+,.0f})  '
              f'Total: Rs.{pnl["total_rs"]:+,.0f}')
        logger.info(msg.replace(prefix, '').replace('\n', '  '))
        _slack(msg, SLACK_TRADE_UPDATES)


def now_after_min_entry() -> bool:
    return datetime.now().time() >= datetime.strptime(MIN_ENTRY_TIME, '%H:%M').time()


# ---------------------------------------------------------------------------
# Standalone login and entry point
# ---------------------------------------------------------------------------

def _login() -> tuple:
    import pyotp
    from SmartApi import SmartConnect

    creds = pd.read_csv(CREDS_FILE)
    row = creds.iloc[0]
    api_key = str(row['api_key'])
    client_code = str(row['user_name'])

    obj = SmartConnect(api_key=api_key)
    totp = pyotp.TOTP(str(row['qr_code'])).now()
    resp = obj.generateSession(client_code, str(row['password']), totp)
    if not resp.get('status'):
        raise RuntimeError(f'Angel One login failed: {resp}')

    auth_token = resp['data']['jwtToken']
    logger.info(f'Logged in as {client_code}')
    return obj, auth_token, api_key, client_code


def main():
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / 'trade_logs').mkdir(exist_ok=True)

    # §0: Prometheus's own daily gate against mcx_holidays.csv — distinct
    # from Leto's NSE/BSE check, and checked before login (a local-file
    # check needs no broker session) so a holiday costs nothing beyond
    # this one read.
    closed, reason = mcx_fully_closed_today()
    if closed:
        logger.info(f'MCX closed today ({reason}) — not starting.')
        print(f'MCX is closed today ({reason}). Not starting Prometheus.')
        sys.exit(0)

    if COMMAND_FLAG_PATH.exists() and COMMAND_FLAG_PATH.read_text().strip() == 'DISABLE':
        logger.info('Prometheus DISABLE flag set — not starting.')
        print('Prometheus is disabled via Slack. Clear the flag to resume.')
        sys.exit(0)

    ok, reason = check_no_active_strategies()
    if not ok:
        print(f'ERROR: Cannot start Prometheus — {reason}')
        print('Prometheus requires an exclusive Angel One session.')
        sys.exit(1)

    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, signal.SIGTERM)
            logger.info(f'Sent SIGTERM to existing Prometheus process (PID {old_pid}); waiting...')
            time.sleep(3)
        except (ProcessLookupError, ValueError):
            pass
        PID_FILE.unlink(missing_ok=True)

    PID_FILE.write_text(str(os.getpid()))
    FLAG_PATH.touch()
    logger.info('prometheus_active.flag created.')

    obj = None
    try:
        obj, auth_token, api_key, client_code = _login()
        prometheus = Prometheus(obj, auth_token, api_key, client_code)
        prometheus.run()
    except KeyboardInterrupt:
        logger.info('KeyboardInterrupt.')
    except Exception as e:
        logger.exception(f'Unhandled exception: {e}')
        _slack(f'\U0001f6a8 *Prometheus* crashed: {e}', SLACK_ERRORS_CHANNEL)
    finally:
        FLAG_PATH.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        if obj is not None:
            try:
                obj.terminateSession(str(pd.read_csv(CREDS_FILE).iloc[0]['user_name']))
            except Exception:
                pass
        logger.info('Session terminated.')


if __name__ == '__main__':
    main()
