"""
The Slack listener's view of Prometheus once Hestia hosts it (plans/hestia-p6-cutover.md).

The listener (slack_listener.py, a separate daemon) keeps writing the same operator files for Prometheus; this module says WHERE, and in
what format, depending on `hestia_config.SLACK_PROMETHEUS_VIA_HESTIA`. Pure functions of the configuration, so both worlds are
testable without Slack:

    standalone (False, today)   prometheus_production/data/prometheus_command.flag, sizing_override.json {lot_calc, lot_count}
    hosted by Hestia (True)     hestia_data/flags/prometheus_command.flag, hestia_data/state/prometheus_sizing.json
                                {dynamic, static_units}; the flag words (EXIT | KILL | DISABLE) mean the same in both

Flipping the switch is part of the cutover and is undone by the rollback; a stale command flag left in the OTHER world's directory is
harmless because nothing reads it, but the runbook clears both.
"""

from __future__ import annotations

import os
from typing import Optional


def via_hestia(cfg) -> bool:
    return bool(getattr(cfg, 'SLACK_PROMETHEUS_VIA_HESTIA', False))


def command_flag_path(cfg, base_dir: str) -> str:
    if via_hestia(cfg):
        return os.path.join(str(cfg.FLAG_DIR), 'prometheus_command.flag')
    return os.path.join(base_dir, 'prometheus_production', 'data', 'prometheus_command.flag')


def sizing_override_path(cfg, base_dir: str) -> str:
    if via_hestia(cfg):
        return os.path.join(str(cfg.STATE_DIR), 'prometheus_sizing.json')
    return os.path.join(base_dir, 'prometheus_production', 'data', 'sizing_override.json')


def sizing_override_payload(cfg, lot_calc: bool, lot_count: int) -> dict:
    """The JSON body for the override file, in the format of whichever process reads it."""
    if via_hestia(cfg):
        return {'dynamic': bool(lot_calc), 'static_units': int(lot_count)}
    return {'lot_calc': lot_calc, 'lot_count': lot_count}


def state_hint(cfg, base_dir: str) -> Optional[str]:
    """Where an operator looks for Prometheus's current state under each world (for the control panel's status line)."""
    if via_hestia(cfg):
        return os.path.join(str(cfg.STATE_DIR), 'prometheus_state.json')
    return os.path.join(base_dir, 'prometheus_production', 'data', 'prometheus_state.csv')


def start_command(cfg, python: str) -> tuple:
    """(argv, process-match pattern, log prefix) for the control panel's Start button."""
    if via_hestia(cfg):
        return [python, 'hestia.py'], 'python.*hestia.py', 'hestia'
    return [python, 'prometheus_production/prometheus.py'], 'python.*prometheus_production/prometheus.py', 'prometheus'
