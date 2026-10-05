"""
Flag files: the operator's way to talk to a running Hestia (plan section 2, Flags), keeping the semantics Prometheus users know.

  <dir>/hestia_active.flag         exists while Hestia runs; REMOVING it ends everything (host flag, a graceful shutdown)
  <dir>/hestia_disabled.flag       a startup gate for the whole host: while it exists `hestia.py` (cron or Slack Start) logs, alerts and exits
                                   before logging in. Written by the Slack panel's Stop/Disable button together with the removal of the
                                   active flag, removed by its Clear button; nothing in a running Hestia reads it
  <dir>/<engine>_command.flag      one word: EXIT | KILL | DISABLE
      EXIT     liquidate this engine's position and re-arm; the flag stays until the engine is confirmed flat with nothing in
               flight, then is cleared (an unconfirmed liquidation leaves it, and the EXIT is re-delivered)
      KILL     stop this engine only; its position stays open and untouched. The flag stays, so a restart keeps the engine down
               until the operator clears it (truncate or remove)
      DISABLE  a startup gate: the engine is not launched at all while the flag says DISABLE

FlagWatcher polls on the reactor (so it runs under the core lock) and only reads files; a Slack listener, a shell `echo`, or the
restart procedure can all write them.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional

from hestia_core.interface import CommandKind, StopReason

log = logging.getLogger('hestia_flags')


class FlagFiles:

    def __init__(self, directory: os.PathLike):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.host_flag = self.dir / 'hestia_active.flag'
        self.host_disabled_flag = self.dir / 'hestia_disabled.flag'

    def command_path(self, engine: str) -> Path:
        return self.dir / f'{engine}_command.flag'

    def read_command(self, engine: str) -> Optional[str]:
        try:
            text = self.command_path(engine).read_text().strip().upper()
        except FileNotFoundError:
            return None
        return text or None

    def clear_command(self, engine: str) -> None:
        self.command_path(engine).unlink(missing_ok=True)

    def raise_host_flag(self) -> None:
        self.host_flag.touch()

    def host_flag_present(self) -> bool:
        return self.host_flag.exists()

    def drop_host_flag(self) -> None:
        self.host_flag.unlink(missing_ok=True)

    def host_disabled(self) -> bool:
        return self.host_disabled_flag.exists()

    def set_host_disabled(self) -> None:
        self.host_disabled_flag.touch()

    def clear_host_disabled(self) -> None:
        self.host_disabled_flag.unlink(missing_ok=True)


class FlagWatcher:

    def __init__(self, core, reactor, flags: FlagFiles, on_host_flag_removed: Callable[[], None], poll_s: float = 1.0,
                 exit_retry_s: float = 60.0):
        self.core, self.reactor, self.flags = core, reactor, flags
        self.on_host_flag_removed = on_host_flag_removed
        self.poll_s, self.exit_retry_s = poll_s, exit_retry_s
        self._killed: set = set()
        self._exit_sent: Dict[str, object] = {}
        self._host_gone = False
        self._stopped = False

    def start(self) -> None:
        self.reactor.after(self.poll_s, self._tick)

    def stop(self) -> None:
        self._stopped = True

    def disabled_engines(self, names: List[str]) -> List[str]:
        return [n for n in names if self.flags.read_command(n) == 'DISABLE']

    def _flat_and_idle(self, engine: str) -> bool:
        holds = any(net for (e, _), (net, _, _) in self.core._ledger.items() if e == engine)
        busy = any(r.engine == engine and r.state in ('queued', 'running', 'reconciling', 'unconfirmed')
                   for r in self.core._registry.values())
        return not holds and not busy

    def _tick(self) -> None:
        if self._stopped:
            return
        if not self._host_gone and not self.flags.host_flag_present():
            self._host_gone = True
            log.info('host flag removed: shutting down')
            self.on_host_flag_removed()
        for name in list(self.core._factories):
            cmd = self.flags.read_command(name)
            if cmd == 'KILL' and name not in self._killed:
                self._killed.add(name)
                self.core._alert('warning', name, 'KILL flag: engine stopped, its position (if any) is left open')
                self.core.send_command(name, CommandKind.KILL)
            elif cmd == 'EXIT' and name not in self._killed:
                last = self._exit_sent.get(name)
                if last is None:
                    self._exit_sent[name] = self.reactor.now
                    self.core._alert('warning', name, 'EXIT flag: liquidating')
                    self.core.send_command(name, CommandKind.EXIT)
                elif self._flat_and_idle(name):
                    self.flags.clear_command(name)
                    self._exit_sent.pop(name, None)
                    self.core._alert('info', name, 'EXIT complete: flat, flag cleared')
                elif (self.reactor.now - last).total_seconds() >= self.exit_retry_s:
                    self._exit_sent[name] = self.reactor.now
                    self.core._alert('critical', name, 'EXIT flag: still not flat, re-sending the command')
                    self.core.send_command(name, CommandKind.EXIT)
            elif cmd is None:
                self._killed.discard(name)                       # the operator cleared a KILL: allow the next one to act
                self._exit_sent.pop(name, None)
        self.reactor.after(self.poll_s, self._tick)
