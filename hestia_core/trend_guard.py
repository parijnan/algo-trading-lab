"""
Two alerts for an engine that is on the wrong side of its own Supertrend without anything having told it so (2026-10-27).

On 26 Oct 2026 Typhon flipped bullish live at 21:45 and held a long overnight; the 09:00 seed rebuilt the series from a pipeline file
whose minutes differed, came out bearish (ST 293.73 against the live 292.40), and nothing noticed: the ledger, the broker book and the
engine's own state all agreed on the position, and a held position with no flip pending was never compared with the trend. The engine
then missed the 16:00 bearish flip. Nothing here changes a decision; both checks only alert.

1. `last_live_bar`: every bar an engine processes records the trading contract's trend and Supertrend value in the engine's state. At
   start the seeded series is compared with that mark at the SAME bar: a different trend, or a value more than `seed_st_tolerance_pct`
   away, means the seed and the chain the engine traded on disagree. It works flat or in a trade, and on any restart.
2. A position against the trend: a position held in the opposite direction to the current Supertrend trend while no flip, request or
   missed flip is pending. After a seed or a restart this is the symptom of a wrong seed or a missed flip; on a flip bar it is a flip the
   engine did not act on.

The engines mix `TrendGuard` in and need: `state` (with `last_live_bar`, `status`, `direction`, `lots`, `pending`, `pending_flip`,
`pending_missed_flip`, `frozen`), `cfg.seed_st_tolerance_pct`, `contract`, `ctx.st_series(contract, n)`, `_say` and `_busy`.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

log = logging.getLogger('hestia_trend_guard')

REALERT_S = 3600.0                  # a position that stays against the trend is re-reported this often


def trend_name(trend) -> Optional[str]:
    """'bullish' / 'bearish' from a Direction (a str enum) or a plain string; None when there is no trend yet."""
    return None if trend is None else str(getattr(trend, 'value', trend)).lower()


def live_mark(token: str, bar, st) -> dict:
    return {'ts': bar.ts.isoformat(), 'token': token, 'trend': trend_name(st.trend), 'st': float(st.value), 'close': float(bar.close)}


def seed_disagreement(mark: Optional[dict], series, token: str, tolerance_pct: float) -> Optional[str]:
    """Why the seeded series differs from the last live bar, or None when it matches (or cannot be compared: no mark, another contract,
    the bar is not in the series, no trend there yet). `series` is the engine's (bar, supertrend point) list."""
    if not mark or mark.get('token') != token:
        return None
    for bar, st in series:
        if bar.ts.isoformat() != mark.get('ts'):
            continue
        seeded = trend_name(st.trend)
        if seeded is None or st.value is None or not mark.get('st'):
            return None
        live = mark['trend']
        if seeded != live:
            return (f"the seed rebuilt the {bar.ts:%d %b %H:%M} bar as {seeded} (ST {st.value:.2f}), but the engine traded it live as {live} "
                    f"(ST {mark['st']:.2f})")
        diff_pct = abs(st.value - mark['st']) / abs(mark['st']) * 100.0
        if diff_pct > tolerance_pct:
            return (f"the seed rebuilt the {bar.ts:%d %b %H:%M} bar with ST {st.value:.2f}, the engine traded it live with ST {mark['st']:.2f} "
                    f"({diff_pct:.2f}% apart, more than {tolerance_pct:g}%); both are {live}")
        return None
    return None


class TrendGuard:

    def _record_live_bar(self, ev) -> None:
        """Remember the trading contract's trend and Supertrend at this bar; saved with the state right after."""
        if ev.bar is None or ev.st is None or ev.st.trend is None or ev.st.value is None:
            return
        self.state.last_live_bar = live_mark(ev.contract.token, ev.bar, ev.st)

    def _check_trend_guards_at_start(self) -> None:
        """At the end of the start sequence, once the ledger, pending requests, the roll and any missed flip are settled."""
        s = self.state
        if s.frozen or self.contract is None:
            return
        series: List[Tuple] = list(self.ctx.st_series(self.contract, 300))
        if not series:
            return
        problem = seed_disagreement(s.last_live_bar, series, self.contract.token, self.cfg.seed_st_tolerance_pct)
        if problem and s.pending_flip is None and s.pending_missed_flip is None:
            self._say('critical', f'SEED DISAGREES with the live chain: {problem}. Trades this session follow the seeded series; check the '
                                  f'trend on the chart against the position before relying on it.', key='seed_vs_live')
        bar, st = series[-1]
        self._check_position_against_trend(trend_name(st.trend), bar.ts, bar.close, st.value, where='at start')

    def _check_position_against_trend(self, trend: Optional[str], bar_ts, close, st_value, where: str = '') -> None:
        s = self.state
        if s.status != 'in_trade' or s.frozen or trend is None or s.direction is None or trend == s.direction:
            self._trend_alert_at = None                  # nothing wrong (any more): the next occurrence alerts at once
            return
        if s.pending_flip is not None or s.pending_missed_flip is not None or self._busy():
            return
        now = self._now()
        last = getattr(self, '_trend_alert_at', None)
        if last is not None and (now - last).total_seconds() < REALERT_S:      # a standing mismatch repeats hourly, not every bar
            return
        self._trend_alert_at = now
        value = 'n/a' if st_value is None else f'{st_value:.2f}'
        at = f' {where}' if where else ''
        self._say('critical', f'POSITION AGAINST THE TREND{at}: holding {s.direction} ({s.open_lots()} lots of {s.contract_symbol}) while '
                              f'the {bar_ts:%d %b %H:%M} bar has the Supertrend {trend} (close {close:.2f}, ST {value}) and no flip is pending. '
                              f'Check the chart and the broker terminal.', key='position_vs_trend')
