# Slack Position Sizing Workflow Architecture

## Objective
Implement a dynamic configuration interface via Slack, enabling manual updates to position sizing logic (Dynamic vs. Fixed) and lot counts for all core strategies (Artemis, Athena, Apollo). This workflow utilizes a clean Slack Block Kit Modal triggered from the existing `#actions` Control Panel, ensuring atomic updates to the source configuration files with full confirmation logging.

## Architecture Overview
1. **Control Panel Integration**: A new `⚙️ Position Sizing` interactive button added to the persistent `#actions` channel message created during the Circuit Breaker implementation.
2. **Block Kit Modal**: A Slack UI popup form to capture structured input (Strategy, Mode, Lot Count) without cluttering the chat history.
3. **Config Editors**: Logic inside `slack_listener.py` designed to securely open, modify, and save the configuration files specific to each strategy.
4. **Monitor & Confirm Loop**: Success and error notifications sent to `#tradebot-updates` and `#error-alerts`, respectively.

## UI Design (The Modal)

Triggered by the `⚙️ Position Sizing` button, the modal will display:
* **Strategy Selector (Dropdown)**: Artemis, Athena, Apollo.
* **Sizing Mode (Radio Buttons)**: 
  * `Dynamic Auto-Sizing` (Margin-based)
  * `Fixed Lots` (Manual entry)
* **Lot Count (Text Input)**: A numeric field (e.g., `41`). Optional if Dynamic is selected, required if Fixed is selected.

## Implementation Steps

### Step 1: Slack App Preparation
* In the Slack API Dashboard, under **Interactivity & Shortcuts**, configure the Request URL or ensure Socket Mode is fully routing `view_submission` events (Modal submissions).

### Step 2: Listener Daemon (`slack_listener.py`) Updates
* **Action Handler**: Add `@app.action("position_sizing_button")` to intercept the button click and call `client.views_open()` to render the Block Kit Modal payload to the user.
* **View Handler**: Add `@app.view("position_sizing_modal")` to handle the form submission payload.
* **Data Extraction**: Parse the selected strategy, sizing mode, and lot count from the `view_submission` payload.

### Step 3: Strategy Config Editors
Implement targeted file manipulation logic within the daemon to update the source of truth for each strategy.

* **Artemis (`artemis_production/data/trade_settings.csv`)**:
  * Read CSV using `pandas`.
  * Update `lot_calc` to `true` (Dynamic) or `false` (Fixed).
  * Update `lot_count` to the provided integer.
  * Save back to CSV.
  
* **Athena (`athena_production/configs_live.py`)**:
  * Read file as text.
  * Use Regex (`re.sub`) to safely replace the values of `LOT_CALC = ...` and `LOT_COUNT = ...`.
  * Save text file.
  
* **Apollo (`apollo_production/configs_live.py`)**:
  * Read file as text.
  * Use Regex (`re.sub`) to safely replace the values of `LOT_CALC = ...` and `LOT_COUNT = ...`.
  * Save text file.

### Step 4: Monitor & Confirm Loop
* **Validation**: If the user selects "Fixed Lots" but inputs "abc" or leaves it blank, return an inline form validation error (`response_action: "errors"`).
* **Success**: Upon successful file write, use `client.chat_postMessage` to send a summary to `#tradebot-updates` (e.g., `✅ *Position Sizing Updated* by @user: Athena set to Fixed Lots (41).`).
* **Failure**: Catch `IOError` or unexpected parsing errors, log them locally, and send a traceback summary to `#error-alerts`.

### Step 5: Documentation
* Update root `README.md` to document the Position Sizing workflow and the 5-minute Monday morning transition window.

## Expected Outcomes
* **Unified Management**: No need to SSH into the VPS or use an IDE to adjust sizes before the Monday open.
* **State Persistence**: Modifying the source config files directly guarantees that Leto and the strategies will inherently respect the new settings upon their next instantiation, surviving reboots.
* **Zero Channel Clutter**: Modals encapsulate the data-entry phase entirely outside the channel feed.
