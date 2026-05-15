# Slack Circuit Breaker Architecture

## Objective
Implement a highly resilient, file-based circuit breaker controlled via Slack. This enables manual intervention (`!kill`, `!exit`, `!disable`) without risking Leto automatically taking back control on subsequent cron runs. It cleanly separates human management from automated cron jobs.

## Architecture Overview
1. **Slack Listener Daemon (`slack_listener.py`)**: A standalone, lightweight process utilizing Slack's Socket Mode to listen for commands 24/7 without exposing webhooks.
2. **Systemd Service**: A Linux service configuration ensuring the daemon runs continuously and auto-restarts on failure or server reboot.
3. **File-Based Flag (`SLACK_COMMAND.flag`)**: A persistent state file in the `data/` directory. By persisting the flag to disk, the circuit breaker remains active even if the VPS reboots.
4. **Leto Gatekeeper**: `leto.py` checks for the flag *before* initiating any broker login or strategy routing.
5. **Strategy Polling Integration**: Active strategies (Apollo, Artemis, Athena) poll the flag file during their existing loops to halt or liquidate if a command is received mid-trade.

## Command Definitions

| Command | Action on Active Trade (Strategy Loop) | Action on Leto Startup (`leto.py`) |
| :--- | :--- | :--- |
| **`!exit`** | Liquidates open positions safely, drops control. | Aborts startup. |
| **`!kill`** | Drops control immediately. Positions remain open. | Aborts startup. |
| **`!disable`** | Ignores flag (lets active trades run safely). | Aborts startup. |
| **`!clear`** | Clears flag. | Resumes normal operations. |

## Implementation Steps

### Step 1: Slack App Configuration
* Go to the Slack API Dashboard for the existing bot.
* Enable **Socket Mode**.
* Generate an App-Level Token (`xapp-...`).
* Under Event Subscriptions, subscribe to `message.channels` or `app_mention`.

### Step 2: Listener Daemon
* Create a new script: `slack_listener.py` (preferably in `data_pipeline/` or root).
* Utilize the `slack_bolt` App framework.
* Add listeners for the 4 commands.
* Implement the Monitor & Confirm loop: the daemon must write the command to `data/SLACK_COMMAND.flag` and immediately send a confirmation message back to `#tradebot-updates`.

### Step 3: Systemd Service Setup
* Create a service unit file at `/etc/systemd/system/slack_listener.service`.
* Configure `ExecStart` to use the Conda Python environment (`/home/parijnan/anaconda3/bin/python`).
* Set `Restart=always` for daemon resilience.
* Enable and start the service: `sudo systemctl daemon-reload && sudo systemctl enable slack_listener && sudo systemctl start slack_listener`.

### Step 4: Leto Integration
* Add a `check_circuit_breaker()` function to the very beginning of `leto.py`.
* If `EXIT`, `KILL`, or `DISABLE` is found in the flag file, log the maintenance mode, send a Slack message, and `sys.exit(0)` before creating a session.

### Step 5: Strategy Integration
* Add a `_check_slack_commands()` method to the polling loops of `Apollo`, `Athena`, and `IronCondor` (Artemis).
* Map `EXIT` to existing `_execute_exit()` logic, then raise an exception to hand control back to Leto.
* Map `KILL` to immediately raising an exception (bypassing exit execution).
* Explicitly ignore `DISABLE` inside the strategy loop so active trades can finish cleanly.

## Expected Outcomes
* **Zero SSH Management:** Full lifecycle control directly from mobile via Slack.
* **Human-Bot Separation:** Eliminates race conditions between manual intervention and automated cron jobs.
* **High Reliability:** Immune to single-process crashes or VPS reboots. Leto stays disabled until explicitly cleared.
