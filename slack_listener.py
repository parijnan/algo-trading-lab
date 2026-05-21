import os
import sys
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

# Strategy Config Paths
ATHENA_CONFIG = os.path.join(BASE_DIR, "athena_production", "configs_live.py")
APOLLO_CONFIG = os.path.join(BASE_DIR, "apollo_production", "configs_live.py")
ARTEMIS_CONFIG = os.path.join(BASE_DIR, "artemis_production", "data", "trade_settings.csv")

# Strategy State File Paths
ATHENA_STATE  = os.path.join(BASE_DIR, "athena_production",  "data", "athena_state.csv")
APOLLO_STATE  = os.path.join(BASE_DIR, "apollo_production",  "data", "apollo_state.csv")
ARTEMIS_DATA  = os.path.join(BASE_DIR, "artemis_production", "data")

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

# ---------------------------------------------------------------------------
# Config Editors
# ---------------------------------------------------------------------------

def update_python_config(file_path, lot_calc, lot_count):
    """Surgically update LOT_CALC and LOT_COUNT in a python config file."""
    try:
        with open(file_path, 'r') as f:
            content = f.read()
        
        # Replace LOT_CALC - Use \g<1> to avoid octal escape ambiguity
        calc_val = "True" if lot_calc else "False"
        content = re.sub(r'(LOT_CALC\s*=\s*)(True|False)', f'\\g<1>{calc_val}', content)
        
        # Replace LOT_COUNT - Use \g<1> to avoid octal escape ambiguity
        content = re.sub(r'(LOT_COUNT\s*=\s*)(\d+)', f'\\g<1>{lot_count}', content)
        
        with open(file_path, 'w') as f:
            f.write(content)
        return True
    except Exception as e:
        logger.error(f"Failed to update Python config {file_path}: {e}")
        return False

def update_artemis_config(lot_calc, lot_count):
    """Update trade_settings.csv for Artemis."""
    try:
        df = pd.read_csv(ARTEMIS_CONFIG)
        df.loc[0, 'lot_calc'] = lot_calc
        df.loc[0, 'lot_count'] = int(lot_count)
        df.to_csv(ARTEMIS_CONFIG, index=False)
        return True
    except Exception as e:
        logger.error(f"Failed to update Artemis config: {e}")
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
        "text": {"type": "mrkdwn", "text": "*Circuit Breakers:*\nManage active trades and automated routing."}
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
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "⚙️ Manage Sizing"},
                "action_id": "btn_pos_sizing"
            },
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
    Apollo/Athena: set status=idle. Artemis: full archive (mirrors _archive_trade).
    Returns a list of result strings for the Slack confirmation message.
    """
    results = []

    for label, path, col in [
        ("Athena", ATHENA_STATE, "status"),
        ("Apollo", APOLLO_STATE, "status"),
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
        say(channel="#tradebot-updates", text=f"⚠️ *EXIT INITIATED* by <@{user_id}>. Liquidating and halting...")

@app.action("btn_kill_switch")
def handle_kill(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_flag("KILL", user_id):
        say(channel="#tradebot-updates", text=f"🚨 *KILL SWITCH ENGAGED* by <@{user_id}>. Control dropped. Positions remain OPEN.")

@app.action("btn_reset_state")
def handle_reset_state(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    results = reset_all_states()
    lines = "\n".join(f"• {r}" for r in results)
    say(channel="#tradebot-updates", text=f"🔄 *STATE RESET* by <@{user_id}>:\n{lines}")

@app.action("btn_disable_algo")
def handle_disable(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if write_flag("DISABLE", user_id):
        say(channel="#tradebot-updates", text=f"⏸️ *ALGO DISABLED* by <@{user_id}>. Future runs paused.")

@app.action("btn_clear_flag")
def handle_clear(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    if os.path.exists(FLAG_FILE):
        os.remove(FLAG_FILE)
        logger.info(f"Flag cleared by <@{user_id}>.")
        say(channel="#tradebot-updates", text=f"✅ *CIRCUIT BREAKER CLEARED* by <@{user_id}>. Resuming normal operations.")
    else:
        say(channel="#tradebot-updates", text="No active circuit breaker flag found.")

@app.action("btn_start_leto")
def handle_start(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    
    # Check for blocking flag
    if os.path.exists(FLAG_FILE):
        with open(FLAG_FILE, "r") as f:
            cmd = f.read().strip()
        if cmd in ["EXIT", "KILL", "DISABLE"]:
            say(channel="#tradebot-updates", text=f"❌ Cannot start Leto. Persistent flag *{cmd}* is active. Clear it first.")
            return

    # Check if Leto is already running
    try:
        pgrep = subprocess.run(["pgrep", "-f", "python.*leto.py"], capture_output=True, text=True)
        if pgrep.stdout.strip():
            say(channel="#tradebot-updates", text="❌ Leto is already running. Duplicate process prevented.")
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
        say(channel="#tradebot-updates", text=f"🚀 *LETO STARTED* manually by <@{user_id}>. Log: `{os.path.basename(log_name)}`")
        logger.info(f"Leto manually started by <@{user_id}>.")
    except Exception as e:
        err_msg = f"Failed to start Leto: {e}"
        logger.error(err_msg)
        say(channel="#error-alerts", text=f"🚨 {err_msg}")

@app.action("btn_git_pull")
def handle_git_pull(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    say(channel="#tradebot-updates", text=f"⬇️ *Git pull* initiated by <@{user_id}>...")
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
            say(channel="#tradebot-updates", text=f"✅ *Git pull succeeded:*\n```{output}```")
        else:
            say(channel="#tradebot-updates", text=f"❌ *Git pull failed:*\n```{output}```")
        logger.info(f"Git pull by <@{user_id}>: rc={result.returncode}")
    except subprocess.TimeoutExpired:
        say(channel="#tradebot-updates", text="❌ Git pull timed out after 30s.")
        logger.error("Git pull timed out.")
    except Exception as e:
        say(channel="#tradebot-updates", text=f"❌ Git pull error: {e}")
        logger.error(f"Git pull error: {e}")

# ---------------------------------------------------------------------------
# Position Sizing Modal
# ---------------------------------------------------------------------------

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
            "blocks": [
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
                            {"text": {"type": "plain_text", "text": "Apollo (Nifty Trend)"}, "value": "Apollo"}
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
                    "label": {"type": "plain_text", "text": "Lot Count"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "input_lots",
                        "placeholder": {"type": "plain_text", "text": "e.g. 41"}
                    }
                }
            ]
        }
    )

@app.view("view_pos_sizing")
def handle_pos_sizing_submission(ack, body, view, say, client):
    # Extract values
    strategy = view["state"]["values"]["block_strategy"]["select_strategy"]["selected_option"]["value"]
    mode = view["state"]["values"]["block_mode"]["radio_mode"]["selected_option"]["value"]
    lots_str = view["state"]["values"]["block_lots"]["input_lots"]["value"]
    user_id = body["user"]["id"]

    # Validate Lot Count
    try:
        lots = int(lots_str)
        if lots <= 0: raise ValueError
    except ValueError:
        ack(response_action="errors", errors={"block_lots": "Please enter a positive integer for lot count."})
        return

    ack()
    
    lot_calc = (mode == "dynamic")
    success = False

    if strategy == "Artemis":
        success = update_artemis_config(lot_calc, lots)
    elif strategy == "Athena":
        success = update_python_config(ATHENA_CONFIG, lot_calc, lots)
    elif strategy == "Apollo":
        success = update_python_config(APOLLO_CONFIG, lot_calc, lots)

    if success:
        mode_text = "Dynamic Auto-Sizing" if lot_calc else "Fixed Lots"
        msg = f"✅ *Position Sizing Updated* by <@{user_id}>\n*Strategy:* {strategy}\n*Mode:* {mode_text}\n*Lots:* {lots}"
        client.chat_postMessage(channel="#tradebot-updates", text=msg)
        logger.info(f"Position sizing updated for {strategy} by <@{user_id}>: Mode={mode_text}, Lots={lots}")
    else:
        err_msg = f"❌ *Error*: Failed to update configuration for {strategy}. Check daemon logs on VPS."
        client.chat_postMessage(channel="#error-alerts", text=err_msg)

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
