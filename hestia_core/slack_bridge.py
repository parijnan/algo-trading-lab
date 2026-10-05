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


# ---- the per-engine and whole-host control panel (2026-10-05) ------------------------------------------------------------------
# One panel section per engine (Exit, Kill/Disable, Clear) and one for Hestia itself (Start, Stop/Disable, Clear). Every button acts on
# ITS OWN flag file only: an engine's buttons write and remove `<engine>_command.flag`, Hestia's write and remove
# `hestia_disabled.flag` (and Stop also removes `hestia_active.flag`, which is how a running Hestia is told to shut down). The flag words
# are Hestia's own (hestia_core/flags.py). Standalone (rollback) world: only Prometheus has a panel section, on its legacy files.

def panel_engines(cfg) -> list:
    """Engine names that get a panel section, in registry order."""
    if via_hestia(cfg):
        return list(getattr(cfg, 'ENGINES', {}) or {'prometheus': None})
    return ['prometheus']


def engine_command_flag_path(cfg, base_dir: str, engine: str) -> str:
    if via_hestia(cfg):
        return os.path.join(str(cfg.FLAG_DIR), f'{engine}_command.flag')
    if engine != 'prometheus':
        raise ValueError(f'the standalone world only has Prometheus, not {engine!r}')
    return command_flag_path(cfg, base_dir)


def engine_sizing_path(cfg, base_dir: str, engine: str) -> str:
    if via_hestia(cfg):
        return os.path.join(str(cfg.STATE_DIR), f'{engine}_sizing.json')
    if engine != 'prometheus':
        raise ValueError(f'the standalone world only has Prometheus, not {engine!r}')
    return sizing_override_path(cfg, base_dir)


def engine_units_label(cfg, engine: str) -> str:
    """The sizing modal's label for the engine's unit count ('Units (1 unit = N lots)')."""
    entry = (getattr(cfg, 'ENGINES', {}) or {}).get(engine)
    lots = int(getattr(entry, 'lots_per_unit', 2 if engine == 'prometheus' else 1))
    return f'Units (1 unit = {lots} lot{"s" if lots != 1 else ""})'


def host_active_flag_path(cfg) -> str:
    return os.path.join(str(cfg.FLAG_DIR), 'hestia_active.flag')


def host_disabled_flag_path(cfg) -> str:
    return os.path.join(str(cfg.FLAG_DIR), 'hestia_disabled.flag')


def read_engine_flag(path: str) -> Optional[str]:
    """The word in an engine's command flag, upper-cased; None when the file is absent or empty."""
    try:
        text = open(path).read().strip().upper()
    except FileNotFoundError:
        return None
    return text or None


def exit_refusal(flag_word: Optional[str], hestia_running: bool) -> Optional[str]:
    """Why an Exit button must do nothing, or None. An EXIT written over a KILL is silently ignored by a running Hestia and would
    replace the KILL gate; an EXIT written while Hestia is down would liquidate the position at the NEXT start without anyone watching."""
    if flag_word == 'KILL':
        return 'it is killed or disabled (flag KILL): clear the flag first, and note a killed engine only returns when Hestia restarts'
    if not hestia_running:
        return 'Hestia is not running, so nothing would act on it (the flag would fire at the next start): close the position at the broker'
    return None


def stop_hestia(cfg) -> None:
    """Stop/Disable: set the start gate FIRST, then remove the active flag, so no start can slip through between the two."""
    os.makedirs(str(cfg.FLAG_DIR), exist_ok=True)
    open(host_disabled_flag_path(cfg), 'a').close()
    try:
        os.remove(host_active_flag_path(cfg))
    except FileNotFoundError:
        pass


def apply_engine_action(cfg, base_dir: str, engine: str, action: str, hestia_running: bool) -> tuple:
    """One engine button: `action` is 'exit' (write EXIT), 'kill' (write KILL: stops a running engine, and either way keeps it out of the next
    start) or 'clear' (remove THIS engine's flag only). Returns (ok, message without the user mention)."""
    from hestia_core.display import display_name
    name = display_name(engine)
    path = engine_command_flag_path(cfg, base_dir, engine)
    current = read_engine_flag(path)
    if action == 'exit':
        why = exit_refusal(current, hestia_running)
        if why:
            return False, f'❌ Cannot exit {name}: {why}.'
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w').write('EXIT')
        return True, f'⚠️ *{name.upper()} EXIT INITIATED*. Liquidating its position; the engine stays up and watches for the next entry.'
    if action == 'kill':
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w').write('KILL')
        note = ' (it replaced an EXIT still in progress)' if current == 'EXIT' else ''
        return True, (f'🚨 *{name.upper()} KILLED / DISABLED*{note}. A running {name} engine stops now and its position stays OPEN; either way it '
                      f'stays out at the next Hestia start until you press Clear {name}.')
    if action == 'clear':
        if current is None and not os.path.exists(path):
            return True, f'ℹ️ No {name} flag found.'
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return True, (f'✅ *{name.upper()} FLAG CLEARED* (was {current or "empty"}). Only {name}\'s own flag was touched. A stopped engine '
                      f'comes back at the next Hestia start.')
    raise ValueError(f'unknown engine action {action!r}')


def apply_hestia_action(cfg, action: str) -> tuple:
    """Hestia's own buttons that only touch flag files: 'stop' (Stop/Disable) and 'clear'. Start is a process launch, done by the listener
    after `start_refusal`."""
    if action == 'stop':
        stop_hestia(cfg)
        return True, ('🛑 *HESTIA STOP / DISABLE*. The start gate is set and a running Hestia shuts down gracefully, leaving every position '
                      'OPEN and unmanaged. It will not start again, from cron or the Start button, until you press Clear Hestia Flag.')
    if action == 'clear':
        path = host_disabled_flag_path(cfg)
        if not os.path.exists(path):
            return True, 'ℹ️ No Hestia disable flag found.'
        os.remove(path)
        return True, ('✅ *HESTIA FLAG CLEARED*. Only the Hestia start gate was removed (the engines\' own flags are untouched), so Hestia '
                      'can start now: press Start Hestia, or wait for the next cron start.')
    raise ValueError(f'unknown Hestia action {action!r}')


def start_refusal(cfg, hestia_running: bool) -> Optional[str]:
    """Why the Start button must not launch anything, or None."""
    if via_hestia(cfg) and os.path.exists(host_disabled_flag_path(cfg)):
        return 'the Hestia disable flag is set. Press Clear Hestia Flag first'
    if hestia_running:
        return 'it is already running. Duplicate process prevented'
    return None
