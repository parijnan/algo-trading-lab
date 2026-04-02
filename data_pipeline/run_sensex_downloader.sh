#!/bin/bash

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_DIR="/home/parijnan/scripts/algo-trading-lab"
PIPELINE_DIR="$REPO_DIR/data_pipeline"
PYTHON="/home/parijnan/anaconda3/bin/python"
SCRIPT="$PIPELINE_DIR/weekly_option_data_sensex.py"
CONFIG_FILE="$PIPELINE_DIR/config/options_list_sensex.csv"
CREDENTIALS="$PIPELINE_DIR/data/user_credentials_angel.csv"
LOG="$PIPELINE_DIR/cron.log"

# ---------------------------------------------------------------------------
# Read Slack token from credentials CSV (header row: ...,slack_token,...)
# ---------------------------------------------------------------------------
SLACK_TOKEN=$(python3 -c "
import csv
with open('$CREDENTIALS') as f:
    reader = csv.DictReader(f)
    print(next(reader)['slack_token'])
")
SLACK_MEMBER_ID=$(python3 -c "
import csv
with open('$CREDENTIALS') as f:
    reader = csv.DictReader(f)
    print(next(reader)['slack_member_id'])
")
SLACK_DATA_CHANNEL="#data-alerts"
SLACK_ERROR_CHANNEL="#error-alerts"
SLACK_URL="https://slack.com/api/chat.postMessage"

send_slack_msg() {
    curl -s -X POST "$SLACK_URL" \
        -H "Authorization: Bearer $SLACK_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"channel\": \"$SLACK_DATA_CHANNEL\", \"text\": \"$1\"}" > /dev/null
}

send_slack_error() {
    curl -s -X POST "$SLACK_URL" \
        -H "Authorization: Bearer $SLACK_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"channel\": \"$SLACK_ERROR_CHANNEL\", \"text\": \"$1\"}" > /dev/null
}

# ---------------------------------------------------------------------------
# Step 1 — Send Slack warning
# ---------------------------------------------------------------------------
send_slack_msg "<@$SLACK_MEMBER_ID> ⚠️ *Sensex Downloader* – Run started. Do not push updates to GitHub until downloads are complete."
echo "$(date '+%Y-%m-%d %H:%M:%S') Slack warning sent." >> "$LOG"

# ---------------------------------------------------------------------------
# Step 2 — Git pull
# ---------------------------------------------------------------------------
echo "$(date '+%Y-%m-%d %H:%M:%S') Pulling latest from GitHub..." >> "$LOG"
cd "$REPO_DIR"
git pull >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: git pull failed." >> "$LOG"
    send_slack_error "🚨 *Sensex Downloader* – git pull failed. Check cron.log on VPS."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 3 — Run the Python downloader
# ---------------------------------------------------------------------------
echo "$(date '+%Y-%m-%d %H:%M:%S') Starting Sensex downloader..." >> "$LOG"
$PYTHON "$SCRIPT" >> "$LOG" 2>&1
PY_EXIT_CODE=$?

# ---------------------------------------------------------------------------
# Step 4 — Push options_list_sensex.csv only if it was modified
# ---------------------------------------------------------------------------
cd "$REPO_DIR"
if ! git diff --quiet "$CONFIG_FILE"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') options_list_sensex.csv modified – pushing to GitHub..." >> "$LOG"
    git add "$CONFIG_FILE"
    git commit -m "Update options_list_sensex.csv – $(date '+%Y-%m-%d') run" >> "$LOG" 2>&1
    git push >> "$LOG" 2>&1
    if [ $? -ne 0 ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: git push failed." >> "$LOG"
        send_slack_error "🚨 *Sensex Downloader* – git push failed after download. Manual push required."
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') options_list_sensex.csv pushed to GitHub." >> "$LOG"
    fi
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') options_list_sensex.csv unchanged – no push needed." >> "$LOG"
fi

# ---------------------------------------------------------------------------
# Step 5 — Final Slack notification
# ---------------------------------------------------------------------------
if [ $PY_EXIT_CODE -eq 0 ]; then
    send_slack_msg "<@$SLACK_MEMBER_ID> ✅ *Sensex Downloader* – Run completed successfully. Safe to push updates to GitHub."
else
    send_slack_msg "<@$SLACK_MEMBER_ID> 🚨 *Sensex Downloader* – Run completed with errors. Check cron.log on VPS."
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') Wrapper script complete." >> "$LOG"
