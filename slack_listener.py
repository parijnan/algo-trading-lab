import os
import sys
import json
import subprocess
import logging
import re
import pandas as pd
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------------------------------------------------------------------
# Configuration & Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_FILE = os.path.join(BASE_DIR, "logs", "slack_listener.log")
FLAG_FILE = os.path.join(DATA_DIR, "SLACK_COMMAND.flag")
CREDS_FILE = os.path.join(DATA_DIR, "user_credentials.csv")

# Sizing override paths (gitignored JSON files, one per strategy)
SIZING_OVERRIDE_PATHS = {
    'Artemis':    os.path.join(BASE_DIR, 'artemis_production',    'data', 'sizing_override.json'),
    'Athena':     os.path.join(BASE_DIR, 'athena_production',     'data', 'sizing_override.json'),
    'Iris':       os.path.join(BASE_DIR, 'iris_production',       'data', 'sizing_override.json'),
    'Prometheus': os.path.join(BASE_DIR, 'prometheus_production', 'data', 'sizing_override.json'),
}
ROUTING_STATE_FILE = os.path.join(DATA_DIR, "routing_state.json")

# Strategy State File Paths
ATHENA_STATE     = os.path.join(BASE_DIR, "athena_production",     "data", "athena_state.csv")
IRIS_STATE       = os.path.join(BASE_DIR, "iris_production",       "data", "iris_state.csv")
ARTEMIS_DATA     = os.path.join(BASE_DIR, "artemis_production",    "data")
PROMETHEUS_STATE = os.path.join(BASE_DIR, "prometheus_production", "data", "prometheus_state.csv")

# Prometheus's own circuit breaker — deliberately separate from FLAG_FILE
# (plan §0/§5: Prometheus isn't Leto-routed, no VIX/regime coupling with the
# NSE/BSE strategies; an operator managing one side shouldn't accidentally
# also kill the other).
PROMETHEUS_COMMAND_FLAG = os.path.join(BASE_DIR, "prometheus_production", "data", "prometheus_command.flag")
# Once Hestia hosts Prometheus (hestia_config.SLACK_PROMETHEUS_VIA_HESTIA), the same buttons write Hestia's files instead.
import hestia_config          # noqa: E402  (repo-root module; a different name from every strategy's own configs)
from hestia_core import slack_bridge  # noqa: E402
PROMETHEUS_VIA_HESTIA = slack_bridge.via_hestia(hestia_config)
PROMETHEUS_COMMAND_FLAG = slack_bridge.command_flag_path(hestia_config, BASE_DIR)
from hestia_core.display import display_name  # noqa: E402
ENGINE_NAMES = slack_bridge.panel_engines(hestia_config)                      # registry order; only Prometheus in the standalone world
ENGINE_STRATEGIES = {display_name(n): n for n in ENGINE_NAMES}                # 'Prometheus' -> 'prometheus', ...
for _strategy, _engine in ENGINE_STRATEGIES.items():
    SIZING_OVERRIDE_PATHS[_strategy] = slack_bridge.engine_sizing_path(hestia_config, BASE_DIR, _engine)

# Ensure logs directory exists
os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Load credentials
try:
    creds = pd.read_csv(CREDS_FILE).iloc[0]
    bot_token = creds['slack_token']      # xoxb- token
    app_token = creds['slack_app_token']  # xapp- token
except Exception as e:
    logger.error(f"Failed to load credentials from {CREDS_FILE}: {e}")
    sys.exit(1)

app = App(token=bot_token)

_CH        = "#tradebot-updates"
_CH_ERRORS = "#error-alerts"

# ---------------------------------------------------------------------------
# Config Editors
# ---------------------------------------------------------------------------

def write_route_override(mode, strategy):
    """Write ROUTING_MODE and MANUAL_STRATEGY to data/routing_state.json."""
    try:
        import json
        with open(ROUTING_STATE_FILE, 'w') as f:
            json.dump({'routing_mode': mode, 'manual_strategy': strategy}, f)
        logger.info(f"Route override set: ROUTING_MODE={mode!r}, MANUAL_STRATEGY={strategy!r}")
        return True
    except Exception as e:
        logger.error(f"Failed to write routing_state.json: {e}")
        return False


def write_sizing_override(strategy, lot_calc, lot_count):
    """Write lot_calc and lot_count to data/sizing_override.json for the given strategy.
    For Prometheus, prometheus_configs.py reads these same two JSON keys into
    DYNAMIC_SIZING/STATIC_UNITS (plan §5/§6) — no separate write path needed."""
    try:
        path = SIZING_OVERRIDE_PATHS[strategy]
        with open(path, 'w') as f:
            payload = (slack_bridge.sizing_override_payload(hestia_config, lot_calc, lot_count) if strategy in ENGINE_STRATEGIES
                       else {'lot_calc': lot_calc, 'lot_count': lot_count})
            json.dump(payload, f)
        logger.info(f"Sizing override set for {strategy}: lot_calc={lot_calc}, lot_count={lot_count}")
        return True
    except Exception as e:
        logger.error(f"Failed to write sizing override for {strategy}: {e}")
        return False


def clear_sizing_override(strategy):
    """Delete data/sizing_override.json for the given strategy, reverting it
    to whatever its own *_configs.py currently hardcodes. Returns
    (success, existed) — existed=False means there was nothing to clear
    (already on the configs.py default), not an error worth alarming over.
    2026-09-07: no prior 'clear' path existed for any of the four strategies
    (only write_sizing_override, always overwriting) — added specifically
    so a Slack sizing override, once pushed permanently into configs.py,
    can be cleanly removed rather than silently continuing to shadow it."""
    try:
        path = SIZING_OVERRIDE_PATHS[strategy]
        if os.path.exists(path):
            os.remove(path)
            logger.info(f"Sizing override cleared for {strategy}.")
            return True, True
        return True, False
    except Exception as e:
        logger.error(f"Failed to clear sizing override for {strategy}: {e}")
        return False, False


# ---------------------------------------------------------------------------
# Control Panel UI (Block Kit)
# ---------------------------------------------------------------------------
def _confirm(body, yes):
    return {"title": {"type": "plain_text", "text": "Are you sure?"}, "text": {"type": "plain_text", "text": body},
            "confirm": {"type": "plain_text", "text": yes}, "deny": {"type": "plain_text", "text": "Cancel"}}


def _engine_panel_blocks(engine):
    name = display_name(engine)
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{name}:* Exit liquidates its position and it keeps watching. Kill/Disable stops it "
                                                              f"(position stays OPEN) and keeps it out at the next Hestia start. Clear removes "
                                                              f"only {name}'s own flag."}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": f"⚠️ Exit {name}"}, "style": "danger", "action_id": f"btn_eng_{engine}_exit",
             "confirm": _confirm(f"This liquidates any open {name} position. The engine keeps running and watches for its next entry.", f"Yes, Exit {name}")},
            {"type": "button", "text": {"type": "plain_text", "text": f"🚨 Kill/Disable {name}"}, "style": "danger", "action_id": f"btn_eng_{engine}_kill",
             "confirm": _confirm(f"This stops {name} now if it is running (any position stays OPEN, unmanaged) and keeps it out at the next Hestia start until you Clear it.", f"Yes, Kill {name}")},
            {"type": "button", "text": {"type": "plain_text", "text": f"✅ Clear {name} Flag"}, "style": "primary", "action_id": f"btn_eng_{engine}_clear"},
        ]},
    ]


def _hestia_panel_blocks():
    """Hestia's own section (hosted world), then one section per engine. Standalone world: just Prometheus and a Start button."""
    blocks = []
    if PROMETHEUS_VIA_HESTIA:
        blocks += [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Hestia (MCX host for all engines):* Start launches it. Stop/Disable shuts it down "
                                                                  "gracefully (every position stays OPEN, unmanaged) and blocks any start, cron included, "
                                                                  "until Clear. Clear removes only Hestia's own flag."}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "🚀 Start Hestia"}, "style": "primary", "action_id": "btn_hestia_start"},
                {"type": "button", "text": {"type": "plain_text", "text": "🛑 Stop/Disable Hestia"}, "style": "danger", "action_id": "btn_hestia_stop",
                 "confirm": _confirm("This stops ALL engines now. Every open position stays OPEN and nothing manages it. Hestia will not start again until you Clear.", "Yes, Stop Hestia")},
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Clear Hestia Flag"}, "style": "primary", "action_id": "btn_hestia_clear"},
            ]},
        ]
    for engine in ENGINE_NAMES:
        blocks += _engine_panel_blocks(engine)
    if not PROMETHEUS_VIA_HESTIA:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🚀 Start Prometheus"}, "style": "primary", "action_id": "btn_hestia_start"}]})
    return blocks


CONTROL_PANEL_BLOCKS = [
    {
        "type": "header",
        "text": {"type": "plain_text", "text": "🕹️ Algo Trading Lab: Control Panel"}
    },
    *_hestia_panel_blocks(),
    {
        "type": "divider"
    },
    {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*Circuit Breakers:*\nHalt or liquidate active trades."}
    },
    {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "⚠️ Exit Trade"},
                "style": "danger",
                "action_id": "btn_exit_trade",
                "confirm": {
                    "title": {"type": "plain_text", "text": "Are you sure?"},
                    "text": {"type": "plain_text", "text": "This will liquidate ALL open positions and halt the bot."},
                    "confirm": {"type": "plain_text", "text": "Yes, Exit Everything"},
                    "deny": {"type": "plain_text", "text": "Cancel"}
                }
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🚨 Kill Switch"},
                "style": "danger",
                "action_id": "btn_kill_switch",
                "confirm": {
                    "title": {"type": "plain_text", "text": "Are you sure?"},
                    "text": {"type": "plain_text", "text": "This will drop control immediately. Positions will remain OPEN for manual management."},
                    "confirm": {"type": "plain_text", "text": "Yes, Kill Bot"},
                    "deny": {"type": "plain_text", "text": "Cancel"}
                }
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "⏸️ Disable Algo"},
                "action_id": "btn_disable_algo"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔄 Reset State"},
                "style": "danger",
                "action_id": "btn_reset_state",
                "confirm": {
                    "title": {"type": "plain_text", "text": "Are you sure?"},
                    "text": {"type": "plain_text", "text": "This resets ALL strategy state files to idle. Use this ONLY after manually closing positions via the broker. No orders will be placed."},
                    "confirm": {"type": "plain_text", "text": "Yes, Reset State"},
                    "deny": {"type": "plain_text", "text": "Cancel"}
                }
            }
        ]
    },
    {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ Clear Flag"},
                "style": "primary",
                "action_id": "btn_clear_flag"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🚀 Start Leto"},
                "style": "primary",
                "action_id": "btn_start_leto"
            }
        ]
    },
    {
        "type": "divider"
    },
    {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*Manual Adjustment:*\nTrigger mid-session adjustments for Artemis or Athena. Executed via the algo's own order engine using the same logic as an automatic trigger."}
    },
    {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔧 Adjust Artemis"},
                "action_id": "btn_artemis_adjust"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🪂 Adjust Athena"},
                "action_id": "btn_athena_adjust"
            }
        ]
    },
    {
        "type": "divider"
    },
    {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*Routing and Sizing Override:*\nForce strategy selection for the next Mon–Thu entry and manage position sizing across strategies. Force overrides bypass VIX — route unconditionally to the selected strategy."}
    },
    {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "⚡ Auto (VIX)"},
                "style": "primary",
                "action_id": "btn_route_auto"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔵 Force Artemis"},
                "action_id": "btn_route_artemis"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🟢 Force Athena"},
                "action_id": "btn_route_athena"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🟣 Force Iris"},
                "action_id": "btn_route_iris"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "⚙️ Manage Sizing"},
                "action_id": "btn_pos_sizing"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🧹 Clear Sizing Override"},
                "action_id": "btn_clear_sizing"
            }
        ]
    },
    {
        "type": "divider"
    },
    {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*Maintenance:*\nPull the latest code from GitHub to the VPS. Note: if slack_listener.py itself is updated, a manual service restart is required to pick up the changes."}
    },
    {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "⬇️ Git Pull"},
                "action_id": "btn_git_pull"
            }
        ]
    }
]

# ---------------------------------------------------------------------------
# Action Handlers
# ---------------------------------------------------------------------------

def _archive_artemis():
    """
    Mirror Artemis's _archive_trade() for use outside the strategy process.
    If both trade_params files exist: sets spread_status=closed, marks
    trade_book rows active→expired, then moves all files to archived/.
    If already archived: cleans up any orphaned support files.
    Returns a result string.
    """
    pe_path  = os.path.join(ARTEMIS_DATA, "pe_trade_params.csv")
    ce_path  = os.path.join(ARTEMIS_DATA, "ce_trade_params.csv")
    tb_path  = os.path.join(ARTEMIS_DATA, "trade_book.csv")
    tl_path  = os.path.join(ARTEMIS_DATA, "trade_log.csv")
    arch_dir = os.path.join(ARTEMIS_DATA, "archived")

    if all(os.path.exists(p) for p in [pe_path, ce_path, tb_path, tl_path]):
        try:
            pe_df = pd.read_csv(pe_path)
            ce_df = pd.read_csv(ce_path)

            # Get archive prefix from expiry (column index 3)
            prefix = pd.to_datetime(pe_df.iloc[0, 3]).strftime('%Y-%m-%d')

            # Mark spread as closed
            pe_df.at[0, 'spread_status'] = 'closed'
            ce_df.at[0, 'spread_status'] = 'closed'
            pe_df.to_csv(pe_path, index=False)
            ce_df.to_csv(ce_path, index=False)

            # Mark trade_book rows active → expired
            tb_df = pd.read_csv(tb_path)
            tb_df.loc[tb_df['status'] == 'active', 'status'] = 'expired'
            tb_df.to_csv(tb_path, index=False)

            # Move files to archived/
            os.makedirs(arch_dir, exist_ok=True)
            for src, name in [
                (pe_path, f"{prefix} pe_trade_params.csv"),
                (ce_path, f"{prefix} ce_trade_params.csv"),
                (tb_path, f"{prefix} trade_book.csv"),
                (tl_path, f"{prefix} trade_log.csv"),
            ]:
                os.rename(src, os.path.join(arch_dir, name))

            for extra in ['instrument_master.csv', 'scrip_master.csv']:
                p = os.path.join(ARTEMIS_DATA, extra)
                if os.path.exists(p):
                    os.remove(p)

            logger.info(f"Artemis trade archived under prefix {prefix}")
            return f"Artemis: archived to `{prefix}/`"

        except Exception as e:
            logger.error(f"Failed to archive Artemis state: {e}")
            return f"Artemis: ERROR — {e}"

    else:
        # No active trade — clean up any orphaned support files
        cleaned = []
        for name in ['trade_book.csv', 'instrument_master.csv', 'scrip_master.csv']:
            p = os.path.join(ARTEMIS_DATA, name)
            if os.path.exists(p):
                os.remove(p)
                cleaned.append(name)
        if cleaned:
            return f"Artemis: no active trade; removed {', '.join(cleaned)}"
        return "Artemis: no active trade (nothing to reset)"


def reset_all_states():
    """
    Reset all strategy state files without placing any orders.
    Athena/Iris: set status=idle. Artemis: full archive (mirrors _archive_trade).
    Returns a list of result strings for the Slack confirmation message.
    """
    results = []

    for label, path, col in [
        ("Athena",     ATHENA_STATE,     "status"),
        ("Iris",       IRIS_STATE,       "status"),
        ("Prometheus", PROMETHEUS_STATE, "status"),
    ]:
        if not os.path.exists(path):
            results.append(f"{label}: not found (skipped)")
            continue
        try:
            df = pd.read_csv(path)
            if df.empty or col not in df.columns:
                results.append(f"{label}: nothing to reset")
                continue
            current = str(df.at[0, col])
            df.at[0, col] = 'idle'
            df.to_csv(path, index=False)
            results.append(f"{label}: `{current}` → `idle`")
            logger.info(f"Reset {label} state: {current} → idle")
        except Exception as e:
            logger.error(f"Failed to reset {label} state: {e}")
            results.append(f"{label}: ERROR — {e}")

    results.append(_archive_artemis())
    return results


def write_flag(command, user_id):
    try:
        with open(FLAG_FILE, "w") as f:
            f.write(command)
        logger.info(f"Command '{command}' written to flag file by <@{user_id}>.")
        return True
    except Exception as e:
        logger.error(f"Failed to write flag file: {e}")
        return False

@app.action("btn_exit_trade")
def handle_exit(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_flag("EXIT", user_id):
        say(channel=_CH, text=f"⚠️ *EXIT INITIATED* by <@{user_id}>. Liquidating and halting...")

@app.action("btn_kill_switch")
def handle_kill(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_flag("KILL", user_id):
        say(channel=_CH, text=f"🚨 *KILL SWITCH ENGAGED* by <@{user_id}>. Control dropped. Positions remain OPEN.")

@app.action("btn_reset_state")
def handle_reset_state(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    results = reset_all_states()
    lines = "\n".join(f"• {r}" for r in results)
    say(channel=_CH, text=f"🔄 *STATE RESET* by <@{user_id}>:\n{lines}")

@app.action("btn_disable_algo")
def handle_disable(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_flag("DISABLE", user_id):
        say(channel=_CH, text=f"⏸️ *ALGO DISABLED* by <@{user_id}>. Future runs paused.")

@app.action("btn_clear_flag")
def handle_clear(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if os.path.exists(FLAG_FILE):
        os.remove(FLAG_FILE)
        logger.info(f"Flag cleared by <@{user_id}>.")
        say(channel=_CH, text=f"✅ *CIRCUIT BREAKER CLEARED* by <@{user_id}>. Resuming normal operations.")
    else:
        say(channel=_CH, text="No active circuit breaker flag found.")

# ---------------------------------------------------------------------------
# Hestia and per-engine controls (2026-10-05). Every button acts on its OWN flag file only (see hestia_core/slack_bridge.py): an engine's
# Exit / Kill-Disable / Clear touch `<engine>_command.flag`; Hestia's Stop-Disable / Clear touch `hestia_disabled.flag` (Stop also
# removes `hestia_active.flag`); Start launches the process unless the gate is set or Hestia is already running.
# ---------------------------------------------------------------------------

_ENGINE_ACTION_RE = re.compile(r"^btn_eng_(?P<engine>[a-z0-9]+)_(?P<action>exit|kill|clear)$")


def _hestia_running():
    try:
        _, pattern, _ = slack_bridge.start_command(hestia_config, sys.executable)
        return bool(subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.strip())
    except Exception as e:
        logger.error(f"pgrep failed: {e}")
        return False


@app.action(_ENGINE_ACTION_RE)
def handle_engine_button(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    m = _ENGINE_ACTION_RE.match(body["actions"][0]["action_id"])
    engine, action = m.group("engine"), m.group("action")
    if engine not in ENGINE_NAMES:
        say(channel=_CH_ERRORS, text=f"❌ Unknown engine {engine!r} on the control panel; repost the panel.")
        return
    try:
        ok, msg = slack_bridge.apply_engine_action(hestia_config, BASE_DIR, engine, action,
                                                   _hestia_running() if action == "exit" else False)
    except Exception as e:
        logger.error(f"engine {engine} {action} failed: {e}")
        say(channel=_CH_ERRORS, text=f"🚨 {engine} {action} failed: {e}")
        return
    logger.info(f"{engine} {action} by <@{user_id}>: ok={ok}")
    say(channel=_CH, text=f"{msg} (by <@{user_id}>)")


@app.action("btn_hestia_stop")
def handle_hestia_stop(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    try:
        _, msg = slack_bridge.apply_hestia_action(hestia_config, "stop")
    except Exception as e:
        logger.error(f"hestia stop failed: {e}")
        say(channel=_CH_ERRORS, text=f"🚨 Hestia stop failed: {e}")
        return
    logger.info(f"Hestia stop/disable by <@{user_id}>.")
    say(channel=_CH, text=f"{msg} (by <@{user_id}>)")


@app.action("btn_hestia_clear")
def handle_hestia_clear(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    try:
        _, msg = slack_bridge.apply_hestia_action(hestia_config, "clear")
    except Exception as e:
        logger.error(f"hestia clear failed: {e}")
        say(channel=_CH_ERRORS, text=f"🚨 Hestia clear failed: {e}")
        return
    logger.info(f"Hestia flag cleared by <@{user_id}>.")
    say(channel=_CH, text=f"{msg} (by <@{user_id}>)")


@app.action("btn_hestia_start")
def handle_hestia_start(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    label = "Hestia" if PROMETHEUS_VIA_HESTIA else "Prometheus"
    why = slack_bridge.start_refusal(hestia_config, _hestia_running())
    if why:
        say(channel=_CH, text=f"❌ Cannot start {label}: {why}.")
        return
    try:
        argv, _, log_prefix = slack_bridge.start_command(hestia_config, sys.executable)
        timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
        log_dir = os.path.join(BASE_DIR, "logs" if PROMETHEUS_VIA_HESTIA else os.path.join("prometheus_production", "logs"))
        log_name = os.path.join(log_dir, f"{log_prefix}_manual_{timestamp}.log")
        with open(log_name, "w") as log_f:
            subprocess.Popen(argv, stdout=log_f, stderr=log_f, start_new_session=True, cwd=BASE_DIR)
        say(channel=_CH, text=f"🚀 *{label.upper()} STARTED* manually by <@{user_id}>. Log: `{os.path.basename(log_name)}`")
        logger.info(f"{label} manually started by <@{user_id}>.")
    except Exception as e:
        err_msg = f"Failed to start {label}: {e}"
        logger.error(err_msg)
        say(channel=_CH_ERRORS, text=f"🚨 {err_msg}")

@app.action("btn_start_leto")
def handle_start(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    
    # Check for blocking flag
    if os.path.exists(FLAG_FILE):
        with open(FLAG_FILE, "r") as f:
            cmd = f.read().strip()
        if cmd in ["EXIT", "KILL", "DISABLE"]:
            say(channel=_CH, text=f"❌ Cannot start Leto. Persistent flag *{cmd}* is active. Clear it first.")
            return

    # Check if Leto is already running
    try:
        pgrep = subprocess.run(["pgrep", "-f", "python.*leto.py"], capture_output=True, text=True)
        if pgrep.stdout.strip():
            say(channel=_CH, text="❌ Leto is already running. Duplicate process prevented.")
            return
    except Exception as e:
        logger.error(f"pgrep failed: {e}")

    # Launch Leto
    try:
        timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
        log_name = os.path.join(BASE_DIR, "logs", f"leto_manual_{timestamp}.log")
        with open(log_name, "w") as log_f:
            subprocess.Popen(
                [sys.executable, "leto.py"],
                stdout=log_f,
                stderr=log_f,
                start_new_session=True,
                cwd=BASE_DIR
            )
        say(channel=_CH, text=f"🚀 *LETO STARTED* manually by <@{user_id}>. Log: `{os.path.basename(log_name)}`")
        logger.info(f"Leto manually started by <@{user_id}>.")
    except Exception as e:
        err_msg = f"Failed to start Leto: {e}"
        logger.error(err_msg)
        say(channel=_CH_ERRORS, text=f"🚨 {err_msg}")

@app.action("btn_git_pull")
def handle_git_pull(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    say(channel=_CH, text=f"⬇️ *Git pull* initiated by <@{user_id}>...")
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=30
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            say(channel=_CH, text=f"✅ *Git pull succeeded:*\n```{output}```")
        else:
            say(channel=_CH, text=f"❌ *Git pull failed:*\n```{output}```")
        logger.info(f"Git pull by <@{user_id}>: rc={result.returncode}")
    except subprocess.TimeoutExpired:
        say(channel=_CH, text="❌ Git pull timed out after 30s.")
        logger.error("Git pull timed out.")
    except Exception as e:
        say(channel=_CH, text=f"❌ Git pull error: {e}")
        logger.error(f"Git pull error: {e}")

@app.action("btn_route_auto")
def handle_route_auto(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_route_override("auto", "artemis"):
        say(channel=_CH, text=(
            f"⚡ *Routing Override Cleared* by <@{user_id}>\n"
            f"*Mode:* Auto (VIX-based)\n"
            f"_Next entry follows standard VIX routing._"
        ))
    else:
        say(channel=_CH_ERRORS, text="❌ *Error*: Failed to clear routing override. Check daemon logs on VPS.")

@app.action("btn_route_artemis")
def handle_route_artemis(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_route_override("manual", "artemis"):
        say(channel=_CH, text=(
            f"🔵 *Routing Override Set* by <@{user_id}>\n"
            f"*Mode:* Manual\n"
            f"*Strategy:* Artemis (Sensex IC)\n"
            f"_Override is unconditional — Artemis routes regardless of VIX._"
        ))
    else:
        say(channel=_CH_ERRORS, text="❌ *Error*: Failed to set routing override. Check daemon logs on VPS.")

@app.action("btn_route_athena")
def handle_route_athena(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_route_override("manual", "athena"):
        say(channel=_CH, text=(
            f"🟢 *Routing Override Set* by <@{user_id}>\n"
            f"*Mode:* Manual\n"
            f"*Strategy:* Athena (Nifty Calendar)\n"
            f"_Override is unconditional — Athena routes regardless of VIX._"
        ))
    else:
        say(channel=_CH_ERRORS, text="❌ *Error*: Failed to set routing override. Check daemon logs on VPS.")

@app.action("btn_route_iris")
def handle_route_iris(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_route_override("manual", "iris"):
        say(channel=_CH, text=(
            f"🟣 *Routing Override Set* by <@{user_id}>\n"
            f"*Mode:* Manual\n"
            f"*Strategy:* Iris (Nifty Scalping)\n"
            f"_Override is unconditional — Iris routes regardless of VIX._"
        ))
    else:
        say(channel=_CH_ERRORS, text="❌ *Error*: Failed to set routing override. Check daemon logs on VPS.")

# ---------------------------------------------------------------------------
# Artemis Manual Adjustment Modal
# ---------------------------------------------------------------------------

@app.action("btn_artemis_adjust")
def handle_artemis_adjust_btn(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "view_artemis_adjust",
            "title": {"type": "plain_text", "text": "Artemis Adjustment"},
            "submit": {"type": "plain_text", "text": "Trigger Adjustment"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "Select the side to *exit*. The algo will exit that spread and roll the other side's sell according to trade_settings.csv logic."}
                },
                {
                    "type": "input",
                    "block_id": "block_side",
                    "label": {"type": "plain_text", "text": "Side to Exit"},
                    "element": {
                        "type": "radio_buttons",
                        "action_id": "radio_side",
                        "options": [
                            {"text": {"type": "plain_text", "text": "PE — exit PE, roll CE sell inward"}, "value": "pe"},
                            {"text": {"type": "plain_text", "text": "CE — exit CE, roll PE sell inward"}, "value": "ce"}
                        ]
                    }
                }
            ]
        }
    )

@app.view("view_artemis_adjust")
def handle_artemis_adjust_submission(ack, body, view, say):
    side = view["state"]["values"]["block_side"]["radio_side"]["selected_option"]["value"]
    user_id = body["user"]["id"]
    ack()
    if write_flag(f"ADJUST:{side}", user_id):
        say(channel=_CH, text=(
            f"🔧 *Artemis Manual Adjustment* triggered by <@{user_id}>\n"
            f"*Side to exit:* {side.upper()}\n"
            f"_Adjustment will execute on the next monitoring cycle._"
        ))
    else:
        say(channel=_CH_ERRORS, text="❌ *Error*: Failed to write adjustment flag. Check daemon logs on VPS.")

@app.action("btn_athena_adjust")
def handle_athena_adjust_btn(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "view_athena_adjust",
            "title": {"type": "plain_text", "text": "Athena Adjustment"},
            "submit": {"type": "plain_text", "text": "Trigger Adjustment"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "Select the adjustment action. The algo will execute it on the next monitoring cycle using the same order engine as the automatic trigger."}
                },
                {
                    "type": "input",
                    "block_id": "block_action",
                    "label": {"type": "plain_text", "text": "Action"},
                    "element": {
                        "type": "radio_buttons",
                        "action_id": "radio_action",
                        "options": [
                            {"text": {"type": "plain_text", "text": "Enter CE Parachute — buy OTM CE hedge (delta-targeted, bypasses spot trigger condition)"}, "value": "enter_parachute"},
                            {"text": {"type": "plain_text", "text": "Exit CE Parachute — close the active CE hedge position (bypasses spot exit condition)"}, "value": "exit_parachute"},
                            {"text": {"type": "plain_text", "text": "Enter PE Wing — buy PE protective wing (delta-targeted, bypasses spot trigger condition)"}, "value": "enter_wing"},
                            {"text": {"type": "plain_text", "text": "Exit PE Wing — close the active PE wing position (bypasses spot recovery condition)"}, "value": "exit_wing"}
                        ]
                    }
                }
            ]
        }
    )

@app.view("view_athena_adjust")
def handle_athena_adjust_submission(ack, body, view, say):
    action = view["state"]["values"]["block_action"]["radio_action"]["selected_option"]["value"]
    user_id = body["user"]["id"]
    ack()
    _FLAG_MAP = {
        "enter_parachute": ("ATHENA_PARACHUTE:enter", "Enter CE Parachute"),
        "exit_parachute":  ("ATHENA_PARACHUTE:exit",  "Exit CE Parachute"),
        "enter_wing":      ("ATHENA_PE_WING:enter",   "Enter PE Wing"),
        "exit_wing":       ("ATHENA_PE_WING:exit",    "Exit PE Wing"),
    }
    flag_cmd, action_str = _FLAG_MAP.get(action, (None, None))
    if flag_cmd and write_flag(flag_cmd, user_id):
        say(channel=_CH, text=(
            f"🪂 *Athena Manual Adjustment* triggered by <@{user_id}>\n"
            f"*Action:* {action_str}\n"
            f"_Adjustment will execute on the next monitoring cycle._"
        ))
    else:
        say(channel=_CH_ERRORS, text="❌ *Error*: Failed to write adjustment flag. Check daemon logs on VPS.")

# ---------------------------------------------------------------------------
# Position Sizing Modal
# ---------------------------------------------------------------------------

_ENGINE_OPTION_LABELS = {'prometheus': 'Prometheus (Crude Oil)', 'selene': 'Selene (Silver Mic)', 'helios': 'Helios (Gold Petal)',
                         'typhon': 'Typhon (Natural Gas Mini)'}


def _strategy_options():
    """The sizing modals' strategy list: the three Leto-era strategies, then every engine on the panel."""
    legacy = [("Artemis (Sensex IC)", "Artemis"), ("Athena (Nifty Calendar)", "Athena"), ("Iris (Nifty Scalping)", "Iris")]
    engines = [(_ENGINE_OPTION_LABELS.get(e, display_name(e)), display_name(e)) for e in ENGINE_NAMES]
    return [{"text": {"type": "plain_text", "text": label}, "value": value} for label, value in legacy + engines]


def _lots_label(strategy):
    """'Units (1 unit = N lots)' for a Hestia engine (N from the registry), 'Lot Count' for the Leto-era strategies."""
    if strategy in ENGINE_STRATEGIES:
        return slack_bridge.engine_units_label(hestia_config, ENGINE_STRATEGIES[strategy])
    return "Lot Count"


def _pos_sizing_blocks(lots_label):
    """Build the modal's blocks with block_lots' label parameterized —
    Prometheus counts in 'Units' (1 unit = 2 lots, plan §6), the other three
    in 'Lot Count'. Shared by btn_pos_sizing (opens with the default label)
    and the label-swap handler below (rebuilds on strategy change)."""
    return [
        {
            "type": "input",
            "block_id": "block_strategy",
            "label": {"type": "plain_text", "text": "Strategy"},
            "element": {
                "type": "static_select",
                "action_id": "select_strategy",
                "options": _strategy_options()
            }
        },
        {
            "type": "input",
            "block_id": "block_mode",
            "label": {"type": "plain_text", "text": "Sizing Mode"},
            "element": {
                "type": "radio_buttons",
                "action_id": "radio_mode",
                "options": [
                    {"text": {"type": "plain_text", "text": "Dynamic Auto-Sizing"}, "value": "dynamic"},
                    {"text": {"type": "plain_text", "text": "Fixed Lots"}, "value": "fixed"}
                ]
            }
        },
        {
            "type": "input",
            "block_id": "block_lots",
            "label": {"type": "plain_text", "text": lots_label},
            "element": {
                "type": "plain_text_input",
                "action_id": "input_lots",
                "placeholder": {"type": "plain_text", "text": "e.g. 41"}
            }
        }
    ]

@app.action("btn_pos_sizing")
def handle_pos_sizing_btn(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "view_pos_sizing",
            "title": {"type": "plain_text", "text": "Position Sizing"},
            "submit": {"type": "plain_text", "text": "Apply Changes"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": _pos_sizing_blocks("Lot Count"),
        }
    )

@app.action("select_strategy")
def handle_pos_sizing_strategy_select(ack, body, client):
    """Swap the block_lots label between 'Lot Count' and 'Units' as the
    strategy dropdown changes — a static field is wrong 1-of-4 times once
    Prometheus is an option (plan §5)."""
    ack()
    selected = body["actions"][0]["selected_option"]["value"]
    lots_label = _lots_label(selected)
    client.views_update(
        view_id=body["view"]["id"],
        hash=body["view"]["hash"],
        view={
            "type": "modal",
            "callback_id": "view_pos_sizing",
            "title": {"type": "plain_text", "text": "Position Sizing"},
            "submit": {"type": "plain_text", "text": "Apply Changes"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": _pos_sizing_blocks(lots_label),
        }
    )

@app.view("view_pos_sizing")
def handle_pos_sizing_submission(ack, body, view, say, client):
    # Extract values
    strategy = view["state"]["values"]["block_strategy"]["select_strategy"]["selected_option"]["value"]
    mode = view["state"]["values"]["block_mode"]["radio_mode"]["selected_option"]["value"]
    lots_str = view["state"]["values"]["block_lots"]["input_lots"]["value"]
    user_id = body["user"]["id"]
    unit_label = "unit(s)" if strategy in ENGINE_STRATEGIES else "lot(s)"

    # Validate Lot Count / Units
    try:
        lots = int(lots_str)
        if lots <= 0: raise ValueError
    except ValueError:
        ack(response_action="errors", errors={"block_lots": f"Please enter a positive integer for {unit_label}."})
        return

    ack()

    lot_calc = (mode == "dynamic")
    success = False

    success = write_sizing_override(strategy, lot_calc, lots)

    if success:
        mode_text = "Dynamic Auto-Sizing" if lot_calc else "Fixed Lots"
        msg = f"✅ *Position Sizing Updated* by <@{user_id}>\n*Strategy:* {strategy}\n*Mode:* {mode_text}\n*{unit_label.capitalize()}:* {lots}"
        client.chat_postMessage(channel=_CH, text=msg)
        logger.info(f"Position sizing updated for {strategy} by <@{user_id}>: Mode={mode_text}, {unit_label}={lots}")
    else:
        err_msg = f"❌ *Error*: Failed to update configuration for {strategy}. Check daemon logs on VPS."
        client.chat_postMessage(channel=_CH_ERRORS, text=err_msg)

# ---------------------------------------------------------------------------
# Clear Sizing Override modal (2026-09-07) — the counterpart to Manage
# Sizing above. Intended use (per the user): a sizing override is set rarely
# and typically while away from a laptop; once the same change is pushed
# permanently into that strategy's own configs.py, the override file must be
# cleared or it keeps silently shadowing the new configs.py value forever.
# ---------------------------------------------------------------------------

def _clear_sizing_blocks():
    return [
        {
            "type": "input",
            "block_id": "block_strategy",
            "label": {"type": "plain_text", "text": "Strategy"},
            "element": {
                "type": "static_select",
                "action_id": "select_strategy",
                "options": _strategy_options()
            }
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "Deletes the sizing override file — the strategy reverts to whatever "
                    "its own configs.py currently has. *The Hestia engines* apply this live, on their very next entry, "
                    "no restart needed. *Artemis/Athena/Iris* apply it on their next restart, same as any other "
                    "sizing change for those three today."}
        }
    ]

@app.action("btn_clear_sizing")
def handle_clear_sizing_btn(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "view_clear_sizing",
            "title": {"type": "plain_text", "text": "Clear Sizing Override"},
            "submit": {"type": "plain_text", "text": "Clear"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": _clear_sizing_blocks(),
        }
    )

@app.view("view_clear_sizing")
def handle_clear_sizing_submission(ack, body, view, client):
    strategy = view["state"]["values"]["block_strategy"]["select_strategy"]["selected_option"]["value"]
    user_id = body["user"]["id"]
    ack()

    success, existed = clear_sizing_override(strategy)
    if not success:
        client.chat_postMessage(channel=_CH_ERRORS,
                                text=f"❌ *Error*: Failed to clear sizing override for {strategy}. "
                                     f"Check daemon logs on VPS.")
    elif existed:
        client.chat_postMessage(channel=_CH,
                                text=f"🧹 *Sizing Override Cleared* by <@{user_id}>\n*Strategy:* {strategy}\n"
                                     f"Reverted to configs.py's own value.")
        logger.info(f"Sizing override cleared for {strategy} by <@{user_id}>.")
    else:
        client.chat_postMessage(channel=_CH,
                                text=f"ℹ️ No active sizing override found for {strategy} — "
                                     f"already using configs.py's default.")

def post_control_panel():
    try:
        # Find #actions channel ID
        result = app.client.conversations_list(types="public_channel,private_channel")
        actions_channel_id = None
        for channel in result["channels"]:
            if channel["name"] == "actions":
                actions_channel_id = channel["id"]
                break
        
        if not actions_channel_id:
            logger.error("Could not find #actions channel. Make sure the bot is invited to it.")
            return

        # Post the control panel
        app.client.chat_postMessage(
            channel=actions_channel_id,
            text="Algo Trading Lab Control Panel",
            blocks=CONTROL_PANEL_BLOCKS
        )
        logger.info(f"Control Panel posted to #actions ({actions_channel_id}).")
    except Exception as e:
        logger.error(f"Failed to post Control Panel: {e}")

if __name__ == "__main__":
    # Post control panel on start
    post_control_panel()
    
    # Start Socket Mode Handler
    handler = SocketModeHandler(app, app_token)
    handler.start()
