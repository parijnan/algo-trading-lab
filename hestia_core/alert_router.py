"""
Routes the core's alerts and trade reports to Slack channels and the log (plan section 2, Reporting).

Channels follow the repo's convention (hestia_config): lifecycle and info to #tradebot-updates, warnings and worse to
#error-alerts, an engine's own explicit channel wins (`ctx.alert(..., channel='trade-alerts')`). Every message carries a tag naming
the engine and an emoji: an explicit per-event one if the caller passed `ctx.alert(..., emoji=...)` (e.g. an engine's own
"starting"/"seeded" messages, matching the standalone Prometheus process's own per-event-type vocabulary), otherwise the
severity-based default in EMOJI below (added 2026-09-29; previously severity was the only source of emoji). A short per-text
cooldown stops a repeating message from flooding a channel (the silence alert repeats on purpose every few minutes, well
outside the cooldown).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Optional

from hestia_core.slack_queue import SlackQueue

log = logging.getLogger('hestia_alerts')

EMOJI = {'info': '', 'warning': '⚠️ ', 'error': '\U0001f6a8 ', 'critical': '\U0001f6a8\U0001f6a8 '}
LOG_LEVEL = {'info': logging.INFO, 'warning': logging.WARNING, 'error': logging.ERROR, 'critical': logging.CRITICAL}


class AlertRouter:

    def __init__(self, slack: SlackQueue, channels: Dict[str, Optional[str]], cooldown_s: float = 30.0):
        """`channels` maps 'info', 'warning', 'error', 'critical' and any explicit channel alias to a Slack channel."""
        self.slack, self.channels, self.cooldown_s = slack, channels, cooldown_s
        self._last: Dict[tuple, datetime] = {}
        self.suppressed = 0

    def __call__(self, alert) -> None:
        tag = f'*Hestia [{alert.engine}]*' if alert.engine else '*Hestia*'
        log.log(LOG_LEVEL.get(alert.level, logging.INFO), '[%s] %s', alert.engine or 'host', alert.text)
        key = (alert.engine, alert.level, alert.text)
        last = self._last.get(key)
        if last is not None and (alert.ts - last).total_seconds() < self.cooldown_s:
            self.suppressed += 1
            return
        self._last[key] = alert.ts
        channel = self.channels.get(alert.channel) if alert.channel else None
        channel = channel or self.channels.get(alert.level) or self.channels.get('info')
        emoji = alert.emoji if getattr(alert, 'emoji', None) else EMOJI.get(alert.level, "")
        self.slack.send(channel, f'{emoji}{tag}: {alert.text}')

    def trade(self, engine: str, record: dict) -> None:
        """A closed trade, for #trade-alerts."""
        pnl = record.get('total_pnl_rs')
        text = (f"*Hestia [{engine}]*: trade {record.get('trade_id')} {record.get('direction')} closed"
                + (f", P&L Rs {pnl:,.0f}" if isinstance(pnl, (int, float)) else ''))
        self.slack.send(self.channels.get('trade'), text)
