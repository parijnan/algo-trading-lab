"""
Live-read sizing overrides (plan section 7: manual and small, set through a per-engine override file read fresh on every use, the
same pattern as Prometheus's sizing_override.json).

`SizingStore.get(name)` returns the engine's SizingConfig: the defaults from configuration, with `dynamic`, `static_units` and
`allocation_rs` taken from `<dir>/<name>_sizing.json` when that file exists and parses. The unit cap is a HARD LIMIT and only ever
comes from configuration: an override file cannot raise it. A bad or missing file falls back to the defaults.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Dict, Optional

from hestia_core.interface import SizingConfig

log = logging.getLogger('hestia_sizing')


class SizingStore:

    def __init__(self, directory: os.PathLike, defaults: Dict[str, SizingConfig]):
        self.dir = Path(directory)
        self.defaults = dict(defaults)
        self._cache: Dict[str, tuple] = {}                     # name -> (mtime, SizingConfig)

    def path(self, name: str) -> Path:
        return self.dir / f'{name}_sizing.json'

    def get(self, name: str) -> SizingConfig:
        base = self.defaults[name]
        p = self.path(name)
        try:
            mtime = p.stat().st_mtime_ns
        except FileNotFoundError:
            self._cache.pop(name, None)
            return base
        cached = self._cache.get(name)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            raw = json.loads(p.read_text())
            units = raw.get('static_units', base.static_units)
            if not isinstance(units, int) or isinstance(units, bool) or units < 1:
                raise ValueError(f'static_units must be a positive integer, got {units!r}')
            alloc = raw.get('allocation_rs', base.allocation_rs)
            cfg = replace(base, dynamic=bool(raw.get('dynamic', base.dynamic)), static_units=units,
                          allocation_rs=None if alloc is None else float(alloc))
        except Exception as exc:                               # noqa: BLE001 - a bad override must never stop trading, or size it up
            log.warning('ignoring %s: %s', p, exc)
            cfg = base
        self._cache[name] = (mtime, cfg)
        return cfg
