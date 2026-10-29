"""Oleus's parameters are pinned to the research config they copy, to Prometheus's Supertrend settings (shared cached bars), and to the registry."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'janus_backtest'))
import janus_configs as jc   # noqa: E402

import hestia_config as hc                                             # noqa: E402
from oleus_engine import levels as lv                                  # noqa: E402
from oleus_engine.engine import OleusEngine, build                     # noqa: E402
from oleus_engine.engine_configs import DEFAULT                        # noqa: E402
from prometheus_engine.engine_configs import DEFAULT as PROMETHEUS     # noqa: E402


def test_every_rule_value_equals_the_research_config():
    assert DEFAULT.camarilla_factor == jc.CAMARILLA_FACTOR
    assert dict(DEFAULT.level_divisors) == jc.LEVEL_DIVISORS
    assert (DEFAULT.r6_factor, DEFAULT.r7_factor) == (jc.R6_FACTOR, jc.R7_FACTOR)
    assert DEFAULT.max_prev_session_gap_days == jc.MAX_PREV_SESSION_GAP_DAYS and DEFAULT.min_bars_per_session == jc.MIN_BARS_PER_SESSION
    assert (DEFAULT.at_open_minutes, DEFAULT.entry_cutoff_min, DEFAULT.hard_exit_min) == (jc.AT_OPEN_MINUTES, jc.ENTRY_CUTOFF_MIN, jc.HARD_EXIT_MIN)
    assert jc.LEVEL_SOURCE == 'previous_session' and jc.TICK_THROUGH == 0 and jc.STOP_SCALE == 1.0 and jc.MAX_ENTRIES_PER_SETUP == 0
    assert not (jc.MACHINE_REBUY or jc.MACHINE_COUNTER or jc.MACHINE_TSL), 'Oleus is the plain-thirds config; the research defaults must stay plain'
    assert DEFAULT.instrument == jc.MACHINE_SYMBOL == 'CRUDEOILM'


def test_the_data_spec_shares_prometheus_supertrend_settings_so_both_engines_use_one_cached_series():
    spec = OleusEngine().spec
    assert (spec.instrument, spec.timeframe_min, spec.st_period, spec.st_multiplier) == ('CRUDEOILM', 15, PROMETHEUS.st_period, PROMETHEUS.st_multiplier)
    assert spec.provisional.enabled is False


def test_the_registry_entry_matches_the_engine():
    e = hc.ENGINES['oleus']
    assert (e.instrument, e.lots_per_unit, e.product) == ('CRUDEOILM', 3, 'INTRADAY') and e.paper is True
    assert e.factory == 'oleus_engine.engine:build' and isinstance(build(), OleusEngine)
    assert hc.ENGINES['prometheus'].product == 'CARRYFORWARD', 'the live engines keep the default product'
    assert all(hc.ENGINES[n].product == 'CARRYFORWARD' for n in ('prometheus', 'selene', 'helios', 'typhon'))
    assert hc.TRADING_HOSTS['delos']['ENGINES']['oleus'] == dict(enabled=True, paper=True, static_units=1, unit_cap=100)


def test_margin_per_unit_has_the_shape_of_prometheus_with_no_separate_lots_factor():
    assert lv.margin_per_unit(9700.0, 10, DEFAULT) == 9700.0 * 10 / 3 * 4
    assert lv.margin_per_unit(None, 10, DEFAULT) == DEFAULT.fallback_margin_per_unit
