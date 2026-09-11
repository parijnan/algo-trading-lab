"""
_wait_for_pid_exit() — regression tests.

Context: 2026-09-11 restart-race incident. main()'s stale-PID takeover used
to send SIGTERM to the old process then sleep a fixed 3s, assuming that was
enough time for teardown (normally ~1s). That day, the old process's own
shutdown was dragged out past 3s by Slack calls retrying/failing against a
workspace-level message_limit_exceeded condition. The new process, having
"waited" its fixed 3s, wrote a fresh PID_FILE/FLAG_PATH while the old one
was still alive -- and when the old process finally finished and ran its
own routine `finally` cleanup (FLAG_PATH.unlink()), it deleted the NEW
process's fresh flag out from under it, which read the loss as an operator-
pulled circuit breaker and shut itself down too. Both processes exited;
nothing was left running for the rest of the day.

Fixed by polling os.kill(pid, 0) (a liveness probe, sends no signal) until
the PID is confirmed gone from the process table, instead of trusting a
fixed sleep -- a PID can only disappear after the process (including its
own `finally` block) has fully exited, so a confirmed disappearance is a
real guarantee, not a guess. Tests mock os.kill so no real process/signal
is ever touched.
"""
import importlib.util
import logging
import os
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
PROM_DIR = os.path.join(REPO_ROOT, 'prometheus_production')


def _null_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.handlers = []
    lg.addHandler(logging.NullHandler())
    lg.propagate = False
    return lg


def _load_prometheus_module():
    sys.path.insert(0, PROM_DIR)
    sys.path.insert(0, REPO_ROOT)
    for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
               'prometheus_logger_setup', 'prometheus'):
        sys.modules.pop(mod, None)
    spec = importlib.util.spec_from_file_location('prometheus', os.path.join(PROM_DIR, 'prometheus.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules['prometheus'] = mod
    spec.loader.exec_module(mod)
    mod.save_state = lambda *a, **k: None
    mod.logger = _null_logger('test_prometheus_stale_pid_null')
    # Fast polling for tests -- the real cadence (0.3s) would make a
    # multi-poll test slow for no benefit; only the poll COUNT/logic matters.
    mod._STALE_PID_POLL_INTERVAL_SEC = 0.01
    return mod


class TestWaitForPidExit(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()

    def tearDown(self):
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def test_returns_true_when_pid_already_gone(self):
        with patch.object(self.mod.os, 'kill', side_effect=ProcessLookupError):
            self.assertTrue(self.mod._wait_for_pid_exit(12345, timeout=1.0))

    def test_returns_true_after_a_few_polls(self):
        """Alive, alive, then gone -- the exact shape of the incident: the
        old process outlives the first couple of checks before exiting."""
        calls = {'n': 0}

        def fake_kill(pid, sig):
            calls['n'] += 1
            if calls['n'] < 3:
                return None  # still alive
            raise ProcessLookupError

        with patch.object(self.mod.os, 'kill', side_effect=fake_kill):
            self.assertTrue(self.mod._wait_for_pid_exit(12345, timeout=1.0))
        self.assertEqual(calls['n'], 3)

    def test_returns_false_on_timeout_when_still_alive(self):
        with patch.object(self.mod.os, 'kill', return_value=None):
            self.assertFalse(self.mod._wait_for_pid_exit(12345, timeout=0.05))

    def test_permission_error_treated_as_gone(self):
        """A PID we can't signal (different owner) is almost certainly a
        stale number the OS already reassigned, not our old process still
        running -- every Prometheus process on this box runs as the same
        user, so this can't be our own old process."""
        with patch.object(self.mod.os, 'kill', side_effect=PermissionError):
            self.assertTrue(self.mod._wait_for_pid_exit(12345, timeout=1.0))

    def test_default_timeout_matches_module_constant(self):
        import inspect
        sig = inspect.signature(self.mod._wait_for_pid_exit)
        self.assertEqual(sig.parameters['timeout'].default, self.mod._STALE_PID_WAIT_TIMEOUT_SEC)


if __name__ == '__main__':
    unittest.main()
