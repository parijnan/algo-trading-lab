"""
_slack_worker() resilience — regression tests.

Context: 2026-09-11 live incident. The worker's own client.chat_postMessage()
call went silent starting ~15:35 IST — the bare `except Exception: pass`
meant any failure (a hang, a rejected call) vanished with zero trace, and
since the main trading loop only ever enqueues (non-blocking), nothing on
the trading side ever noticed. ~300 messages piled up in the unbounded
queue over ~95 minutes before the user caught it by noticing Slack had
gone quiet. A live restart did NOT recover delivery either — the fresh
process's own worker also failed silently, which is what made "give this
worker an unbounded hang and no error visibility" untenable as a design.

Fixed with two independent changes: an explicit WebClient(timeout=...)
tighter than the SDK's own 30s default, and logger.error() on every
caught exception -- so a delivery failure is visible in the log the
moment it happens, not only via an external Slack-channel comparison.

Tests mock slack_sdk.WebClient (patched into sys.modules, since
_slack_worker does a lazy `from slack_sdk import WebClient` import) so no
real network call or Slack post ever happens.
"""
import importlib.util
import logging
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock

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
    mod.logger = _null_logger('test_prometheus_slack_worker_null')
    return mod


class TestSlackWorker(unittest.TestCase):

    def setUp(self):
        self.mod = _load_prometheus_module()
        self.mod.logger = MagicMock()
        self._orig_slack_sdk = sys.modules.get('slack_sdk')

    def tearDown(self):
        if self._orig_slack_sdk is not None:
            sys.modules['slack_sdk'] = self._orig_slack_sdk
        else:
            sys.modules.pop('slack_sdk', None)
        for mod in ('prometheus_configs', 'prometheus_state', 'prometheus_functions',
                   'prometheus_logger_setup', 'prometheus'):
            sys.modules.pop(mod, None)
        for d in (PROM_DIR, REPO_ROOT):
            if d in sys.path:
                sys.path.remove(d)

    def _install_fake_webclient(self, fake_client):
        fake_cls = MagicMock(return_value=fake_client)
        fake_module = type(sys)('slack_sdk')
        fake_module.WebClient = fake_cls
        sys.modules['slack_sdk'] = fake_module
        return fake_cls

    def test_webclient_constructed_with_explicit_timeout(self):
        fake_client = MagicMock()
        fake_cls = self._install_fake_webclient(fake_client)
        self.mod._slack_queue.put(('hello', '#trade-updates'))

        t = threading.Thread(target=self.mod._slack_worker, daemon=True)
        t.start()
        self.mod._slack_queue.join()

        fake_cls.assert_called_once_with(token=self.mod._SLACK_TOKEN, timeout=self.mod._SLACK_TIMEOUT_SEC)
        fake_client.chat_postMessage.assert_called_once_with(channel='#trade-updates', text='hello')
        self.mod.logger.error.assert_not_called()

    def test_failure_is_logged_and_worker_keeps_processing(self):
        """The exact 2026-09-11 gap: a failed send used to vanish silently
        and (per the incident) the failure mode could otherwise stall
        delivery -- confirm here that a raised exception is (a) logged and
        (b) does not stop the next queued message from being attempted."""
        fake_client = MagicMock()
        fake_client.chat_postMessage.side_effect = [RuntimeError('boom'), None]
        self._install_fake_webclient(fake_client)

        self.mod._slack_queue.put(('msg1', '#trade-updates'))
        self.mod._slack_queue.put(('msg2', '#trade-updates'))

        t = threading.Thread(target=self.mod._slack_worker, daemon=True)
        t.start()
        self.mod._slack_queue.join()

        self.assertEqual(fake_client.chat_postMessage.call_count, 2)
        self.mod.logger.error.assert_called_once()
        logged_msg = self.mod.logger.error.call_args[0][0]
        self.assertIn('#trade-updates', logged_msg)
        self.assertIn('boom', logged_msg)

    def test_success_logs_nothing(self):
        fake_client = MagicMock()
        self._install_fake_webclient(fake_client)
        self.mod._slack_queue.put(('all good', '#tradebot-updates'))

        t = threading.Thread(target=self.mod._slack_worker, daemon=True)
        t.start()
        self.mod._slack_queue.join()

        self.mod.logger.error.assert_not_called()


if __name__ == '__main__':
    unittest.main()
