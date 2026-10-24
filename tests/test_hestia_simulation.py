"""Tests for hestia_simulation: the capital rule, per-order charges and the exit-order count (all on synthetic numbers)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hestia_simulation'))
import sim_config as cfg  # noqa: E402
import simulate as sim  # noqa: E402


def test_units_round_down_and_skip_below_one():
    assert sim.units_for(400000, 100000) == 1
    assert sim.units_for(399999, 100000) == 0                   # a quarter of the account is short of one unit: the entry is skipped
    assert sim.units_for(1e12, 100000) == cfg.UNIT_CAP


def test_charges_on_one_round_trip():
    # Selene, one unit at Rs 2,00,000 a lot, a long: buy on entry (stamp duty), sell on exit (CTT)
    ch = sim.order_charges('Selene', 'bullish', 200000, 1, 1)
    assert ch['brokerage'] == 40.0
    assert abs(ch['ctt'] - 20.0) < 1e-9                                         # 0.01% of the sell order
    assert abs(ch['other'] - (4.2 + 0.2 + 4.0 + 4.2 + 0.2)) < 1e-9              # exchange fee and SEBI fee on both orders, stamp duty on the buy
    assert abs(ch['gst'] - 0.18 * (2 * 20 + 2 * 4.2 + 2 * 0.2)) < 1e-9
    short = sim.order_charges('Selene', 'bearish', 200000, 1, 1)               # a short sells first: CTT on the entry, stamp duty on the exit
    assert abs(short['ctt'] - 20.0) < 1e-9 and abs(sum(short.values()) - sum(ch.values())) < 1e-9


def test_exit_order_counts():
    assert sim.exit_orders('Prometheus', 'stop_loss/stop_loss') == 1
    assert sim.exit_orders('Prometheus', 'target1/trend_flip') == 2
    assert sim.exit_orders('Janus', 'hard_exit,hard_exit,hard_exit') == 1
    assert sim.exit_orders('Janus', 'target1,target2,hard_exit') == 3
    assert sim.exit_orders('Selene', 'trend_flip') == 1


def test_margin_per_unit_formulas():
    assert abs(cfg.MARGIN_PER_UNIT['Prometheus'](9000) - 9000 * 10 / 3 * 4) < 1e-9
    assert abs(cfg.MARGIN_PER_UNIT['Janus'](9000, 6.0) - 9000 * 10 / 3 * 6) < 1e-9
    assert cfg.ORDER_VALUE['Janus'](9000) == 9000 * 10 * 3


def test_janus_sync_status():
    import janus_sync as js
    ts = js.pd.Timestamp
    assert js.status(ts('2026-10-23'), ts('2026-10-23'))[0] == 'ok'
    assert js.status(ts('2026-10-20'), ts('2026-10-23'))[0] == 'behind'             # the Janus run has to reach the integrated backtest's last day
    assert js.status(ts('2026-10-27'), ts('2026-10-23'))[0] == 'ahead'
    original = js.janus_last_session
    js.janus_last_session = lambda: None                                            # no local Janus run
    try:
        assert js.status(None, ts('2026-10-23'))[0] == 'missing'
    finally:
        js.janus_last_session = original
