"""
Regression test — cross-strategy module naming collisions.

Context: apollo_production/, athena_production/, artemis_production/, and
iris_production/ used to each contain identically-named files (functions.py,
state.py, configs.py/configs_live.py, logger_setup.py). Every strategy does
`sys.path.insert(0, its_own_dir)` then a bare `from X import Y`. Python caches
imports by module name in sys.modules — the first strategy whose file gets
imported in a process wins that name, and every other strategy's same-named
import silently resolves to the same cached module instead of loading its own
file. leto.py's main() is a single long-running process for the whole
session, and its re-routing loop (a strategy handing control back to
_route(), which can then route to a different strategy) genuinely can import
more than one strategy's modules in that one process — this isn't a rare
edge case, see plans/strategy-module-naming-collision-fix.md for the full
incident writeup (found 2026-08-06, fixed 2026-08-07 by renaming every
strategy's shared-name files to a <strategy>_ prefix).

Two independent checks:
  1. Static — no two *_production directories may contain a .py file with the
     same basename. Fast, catches the problem before it ever needs a specific
     import sequence to trigger.
  2. Dynamic — actually import multiple strategies' real modules in sequence,
     in a fresh subprocess (never inline in the main test process — these are
     the real production modules, not mocks, and importing them repeatedly
     inline risks sys.modules cross-contamination with other test files that
     do their own temporary sys.modules aliasing, e.g. test_state_roundtrip.py).
     Mirrors leto.py's actual sys.path.insert + bare-import pattern, including
     os.chdir(ARTEMIS_DIR) before importing Artemis (artemis_configs.py reads
     data/contracts.csv via a relative path at import time).
"""

import glob
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
STRATEGY_DIRS = {
    'apollo':  os.path.join(REPO_ROOT, 'apollo_production'),
    'athena':  os.path.join(REPO_ROOT, 'athena_production'),
    'artemis': os.path.join(REPO_ROOT, 'artemis_production'),
    'iris':    os.path.join(REPO_ROOT, 'iris_production'),
}


class TestNoSharedFilenamesAcrossStrategies(unittest.TestCase):
    """Static check — no import sequence needed to trigger this one."""

    def test_no_duplicate_basenames(self):
        basename_to_strategies = {}
        for strategy, d in STRATEGY_DIRS.items():
            for path in glob.glob(os.path.join(d, '*.py')):
                base = os.path.basename(path)
                basename_to_strategies.setdefault(base, []).append(strategy)

        duplicates = {b: s for b, s in basename_to_strategies.items() if len(s) > 1}
        self.assertEqual(
            duplicates, {},
            "Duplicate filenames across *_production directories — these "
            "collide via sys.modules the moment leto.py imports more than "
            "one strategy's modules in the same process: "
            f"{duplicates}. See plans/strategy-module-naming-collision-fix.md."
        )


def _run_import_sequence(entry_snippets: list[str]) -> subprocess.CompletedProcess:
    """
    Run a sequence of strategy-import snippets in a fresh subprocess, in
    order. Each snippet is a small script fragment (sys.path.insert + import
    statement[s]), joined and executed as one -c script so later imports see
    earlier ones' sys.modules state, exactly like a real leto.py re-routing
    sequence would.
    """
    script = "import sys, os\nREPO_ROOT = " + repr(REPO_ROOT) + "\n" + "\n".join(entry_snippets)
    return subprocess.run(
        [sys.executable, '-c', script],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )


_IMPORT_SNIPPET = {
    'apollo': (
        "sys.path.insert(0, os.path.join(REPO_ROOT, 'apollo_production'))\n"
        "import apollo\n"
    ),
    'athena': (
        "sys.path.insert(0, os.path.join(REPO_ROOT, 'athena_production'))\n"
        "import athena_engine\n"
    ),
    'artemis': (
        "sys.path.insert(0, os.path.join(REPO_ROOT, 'artemis_production'))\n"
        "_cwd = os.getcwd()\n"
        "os.chdir(os.path.join(REPO_ROOT, 'artemis_production'))\n"
        "import artemis\n"
        "os.chdir(_cwd)\n"
    ),
    'iris': (
        "sys.path.insert(0, os.path.join(REPO_ROOT, 'iris_production'))\n"
        "import iris\n"
    ),
}


class TestCrossStrategyImportResolution(unittest.TestCase):
    """
    Dynamic check — real modules, real sys.path/sys.modules mechanics, run in
    an isolated subprocess per case so this test file has no side effects on
    the rest of the suite (or vice versa).
    """

    def _assert_sequence_imports_cleanly(self, order: list[str]):
        snippets = [_IMPORT_SNIPPET[name] for name in order]
        result = _run_import_sequence(snippets)
        self.assertEqual(
            result.returncode, 0,
            f"Import sequence {order} failed (exit {result.returncode}).\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_athena_then_iris(self):
        # The exact sequence that crashed pre-fix: leto.py hands off from an
        # idle, VIX-out-of-range Athena, re-routes, and Iris runs next in the
        # same process. Historically failed with:
        #   ImportError: cannot import name 'IrisState' from 'state'
        self._assert_sequence_imports_cleanly(['athena', 'iris'])

    def test_all_four_in_sequence(self):
        self._assert_sequence_imports_cleanly(['athena', 'artemis', 'iris', 'apollo'])

    def test_all_four_reverse_order(self):
        # Apollo-first was the specific combination checked last during the
        # fix (previously untested) — kept as its own case since it's the one
        # strategy reached only via the open-position-resume path, not fresh
        # VIX routing, and therefore the one most likely to be imported first
        # in a real session if it has a residual position to manage.
        self._assert_sequence_imports_cleanly(['apollo', 'artemis', 'athena', 'iris'])


if __name__ == '__main__':
    unittest.main()
