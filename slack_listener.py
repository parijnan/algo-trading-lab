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
PROMETHEUS_INSTRUMENT_OVERRIDE = os.path.join(BASE_DIR, "prometheus_production", "data", "instrument_override.json")

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
            json.dump({'lot_calc': lot_calc, 'lot_count': lot_count}, f)
        logger.info(f"Sizing override set for {strategy}: lot_calc={lot_calc}, lot_count={lot_count}")
        return True
    except Exception as e:
        logger.error(f"Failed to write sizing override for {strategy}: {e}")
        return False


def write_instrument_override(symbol, margin_per_unit):
    """Write symbol + margin_per_unit together to Prometheus's
    instrument_override.json (plan §5/§6) — coupled deliberately: CRUDEOILM
    and CRUDEOIL differ 10x in lot size, so switching one without the other
    invites trading the wrong contract at the wrong size."""
    try:
        with open(PROMETHEUS_INSTRUMENT_OVERRIDE, 'w') as f:
            json.dump({'symbol': symbol, 'margin_per_unit': margin_per_unit}, f)
        logger.info(f"Instrument override set: symbol={symbol}, margin_per_unit={margin_per_unit}")
        return True
    except Exception as e:
        logger.error(f"Failed to write instrument override: {e}")
        return False

# ---------------------------------------------------------------------------
# Control Panel UI (Block Kit)
# ---------------------------------------------------------------------------
CONTROL_PANEL_BLOCKS = [
    {
        "type": "header",
        "text": {"type": "plain_text", "text": "🕹️ Algo Trading Lab: Control Panel"}
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
        "text": {"type": "mrkdwn", "text": "*Prometheus (MCX):*\nSeparate circuit breaker — standalone cron, not Leto-routed, so this does NOT affect Artemis/Athena/Apollo/Iris and vice versa."}
    },
    {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "⚠️ Exit Prometheus"},
                "style": "danger",
                "action_id": "btn_prometheus_exit",
                "confirm": {
                    "title": {"type": "plain_text", "text": "Are you sure?"},
                    "text": {"type": "plain_text", "text": "This will liquidate any open Prometheus position and halt it."},
                    "confirm": {"type": "plain_text", "text": "Yes, Exit Prometheus"},
                    "deny": {"type": "plain_text", "text": "Cancel"}
                }
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🚨 Kill Prometheus"},
                "style": "danger",
                "action_id": "btn_prometheus_kill",
                "confirm": {
                    "title": {"type": "plain_text", "text": "Are you sure?"},
                    "text": {"type": "plain_text", "text": "This will drop control immediately. Any Prometheus position remains OPEN for manual management."},
                    "confirm": {"type": "plain_text", "text": "Yes, Kill Prometheus"},
                    "deny": {"type": "plain_text", "text": "Cancel"}
                }
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "⏸️ Disable Prometheus"},
                "action_id": "btn_prometheus_disable"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ Clear Prometheus Flag"},
                "style": "primary",
                "action_id": "btn_prometheus_clear"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🛢️ Switch Instrument"},
                "action_id": "btn_prometheus_instrument"
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

def write_prometheus_flag(command, user_id):
    try:
        with open(PROMETHEUS_COMMAND_FLAG, "w") as f:
            f.write(command)
        logger.info(f"Prometheus command '{command}' written by <@{user_id}>.")
        return True
    except Exception as e:
        logger.error(f"Failed to write Prometheus command flag: {e}")
        return False

@app.action("btn_prometheus_exit")
def handle_prometheus_exit(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_prometheus_flag("EXIT", user_id):
        say(channel=_CH, text=f"⚠️ *PROMETHEUS EXIT INITIATED* by <@{user_id}>. Liquidating and halting...")

@app.action("btn_prometheus_kill")
def handle_prometheus_kill(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_prometheus_flag("KILL", user_id):
        say(channel=_CH, text=f"🚨 *PROMETHEUS KILL SWITCH* engaged by <@{user_id}>. Control dropped. Position remains OPEN.")

@app.action("btn_prometheus_disable")
def handle_prometheus_disable(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_prometheus_flag("DISABLE", user_id):
        say(channel=_CH, text=f"⏸️ *PROMETHEUS DISABLED* by <@{user_id}>. Future runs paused.")

@app.action("btn_prometheus_clear")
def handle_prometheus_clear(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if os.path.exists(PROMETHEUS_COMMAND_FLAG):
        os.remove(PROMETHEUS_COMMAND_FLAG)
        logger.info(f"Prometheus flag cleared by <@{user_id}>.")
        say(channel=_CH, text=f"✅ *PROMETHEUS CIRCUIT BREAKER CLEARED* by <@{user_id}>. Resuming normal operations.")
    else:
        say(channel=_CH, text="No active Prometheus circuit breaker flag found.")

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
                "options": [
                    {"text": {"type": "plain_text", "text": "Artemis (Sensex IC)"}, "value": "Artemis"},
                    {"text": {"type": "plain_text", "text": "Athena (Nifty Calendar)"}, "value": "Athena"},
                    {"text": {"type": "plain_text", "text": "Iris (Nifty Scalping)"}, "value": "Iris"},
                    {"text": {"type": "plain_text", "text": "Prometheus (Crude Oil)"}, "value": "Prometheus"}
                ]
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
    lots_label = "Units (1 unit = 2 lots)" if selected == "Prometheus" else "Lot Count"
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
    unit_label = "unit(s)" if strategy == "Prometheus" else "lot(s)"

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
# Prometheus Instrument Switch Modal (plan §5/§6) — instrument and
# margin-per-unit submitted together, deliberately coupled: CRUDEOILM and
# CRUDEOIL differ 10x in lot size (10 vs 100 barrels), so a margin figure
# sane for one is wrong by roughly an order of magnitude for the other.
# ---------------------------------------------------------------------------

@app.action("btn_prometheus_instrument")
def handle_prometheus_instrument_btn(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "view_prometheus_instrument",
            "title": {"type": "plain_text", "text": "Prometheus Instrument"},
            "submit": {"type": "plain_text", "text": "Apply Changes"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "Switching instrument requires a matching margin-per-unit figure — the two are submitted together so Prometheus never trades one contract sized for the other."}
                },
                {
                    "type": "input",
                    "block_id": "block_instrument",
                    "label": {"type": "plain_text", "text": "Instrument"},
                    "element": {
                        "type": "static_select",
                        "action_id": "select_instrument",
                        "options": [
                            {"text": {"type": "plain_text", "text": "CRUDEOILM"}, "value": "CRUDEOILM"},
                            {"text": {"type": "plain_text", "text": "CRUDEOIL"}, "value": "CRUDEOIL"}
                        ]
                    }
                },
                {
                    "type": "input",
                    "block_id": "block_margin",
                    "label": {"type": "plain_text", "text": "Margin per Unit (Rs)"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "input_margin",
                        "placeholder": {"type": "plain_text", "text": "e.g. 100000"}
                    }
                }
            ]
        }
    )

@app.view("view_prometheus_instrument")
def handle_prometheus_instrument_submission(ack, body, view, say, client):
    symbol = view["state"]["values"]["block_instrument"]["select_instrument"]["selected_option"]["value"]
    margin_str = view["state"]["values"]["block_margin"]["input_margin"]["value"]
    user_id = body["user"]["id"]

    try:
        margin = float(margin_str)
        if margin <= 0: raise ValueError
    except ValueError:
        ack(response_action="errors", errors={"block_margin": "Please enter a positive number for margin per unit."})
        return

    ack()
    if write_instrument_override(symbol, margin):
        msg = (f"🛢️ *Prometheus Instrument Updated* by <@{user_id}>\n"
              f"*Instrument:* {symbol}\n*Margin per Unit:* Rs.{margin:,.0f}\n"
              f"_Takes effect on Prometheus's next session start._")
        client.chat_postMessage(channel=_CH, text=msg)
        logger.info(f"Prometheus instrument updated by <@{user_id}>: symbol={symbol}, margin={margin}")
    else:
        client.chat_postMessage(channel=_CH_ERRORS, text="❌ *Error*: Failed to update Prometheus instrument override. Check daemon logs on VPS.")

# ---------------------------------------------------------------------------
# Initializer
# ---------------------------------------------------------------------------

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
