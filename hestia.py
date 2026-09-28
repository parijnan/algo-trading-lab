"""
hestia.py: the Hestia entry point (cron: one entry, Mon-Fri at 09:00; see plans/selene-production.md section 9).

    cd /home/parijnan/scripts/algo-trading-lab && python hestia.py

Builds the real dependencies for hestia_core.host.HestiaHost (the Angel One login, the WebSocket feeds, the Slack poster) and runs
it. `real_login` below is the ONLY place in the repo's Hestia code that calls `generateSession`, and nothing runs it on import: a
second session on the account evicts the order capability of whatever else is logged in (AB1007), so it happens only when this file
is run as a program, after Hestia's own gates (MCX closed, no engine enabled, another owner holding the session lock) have passed.
With every entry in hestia_config.ENGINES disabled (the state until the engines are ported), Hestia exits before logging in.

Module name: repo-root hestia.py never collides with hestia_core (a package) or any strategy directory module.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).parent
sys.path.insert(0, str(REPO_ROOT))

import hestia_config as cfg  # noqa: E402
from hestia_core.feed_port import SharedFeedAdapter  # noqa: E402
from hestia_core.host import HestiaHost, HostDeps, LoginResult  # noqa: E402
from hestia_core.order_feed import OrderUpdateFeed  # noqa: E402


def _slack_token() -> str:
    try:
        return str(pd.read_csv(cfg.CREDS_FILE).iloc[0]['slack_token'])
    except Exception:                                                         # noqa: BLE001
        return ''


def real_login() -> LoginResult:
    import pyotp
    from SmartApi import SmartConnect

    creds = pd.read_csv(cfg.CREDS_FILE)
    row = creds.iloc[0]
    api_key, client_code = str(row['api_key']), str(row['user_name'])
    obj = SmartConnect(api_key=api_key)
    # SmartConnect.__init__ resets the SDK's internal logger level, so the suppression must come after construction.
    logging.getLogger('logzero_default').setLevel(logging.CRITICAL)
    resp = obj.generateSession(client_code, str(row['password']), pyotp.TOTP(str(row['qr_code'])).now())
    if not resp.get('status'):
        raise RuntimeError(f'Angel One login failed: {resp}')
    return LoginResult(obj, resp['data']['jwtToken'], obj.getfeedToken(), client_code, api_key)


_feeds = []


def make_feed(login: LoginResult, alert):
    from websocket_feed import SharedFeed
    feed = SharedFeed()
    feed.start(auth_token=login.auth_token, api_key=login.api_key, client_code=login.client_code, feed_token=login.feed_token,
               alert_callback=alert)
    _feeds.append(feed)
    return SharedFeedAdapter(feed, cfg.MCX_FO_WS_EXCHANGE_TYPE)


def make_order_feed(login: LoginResult):
    feed = OrderUpdateFeed()
    feed.start(auth_token=login.auth_token, api_key=login.api_key, client_code=login.client_code, feed_token=login.feed_token)
    return feed


def stop_feeds() -> None:
    for f in _feeds:
        f.stop()


def main() -> int:
    cfg.LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s',
                        handlers=[logging.FileHandler(cfg.LOG_DIR / f'hestia_{datetime.now():%Y%m%d}.log'),
                                  logging.StreamHandler()])
    deps = HostDeps(login=real_login, make_feed=make_feed, make_order_feed=make_order_feed, stop_feeds=stop_feeds,
                    slack_token=_slack_token())
    result = HestiaHost(cfg, deps).run()
    logging.getLogger('hestia').info('Hestia finished: started=%s reason=%s', result.started, result.reason)
    return 0


if __name__ == '__main__':
    sys.exit(main())
