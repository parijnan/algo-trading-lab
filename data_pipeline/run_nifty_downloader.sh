#!/bin/bash

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_DIR="/home/parijnan/scripts/algo-trading-lab"
PIPELINE_DIR="$REPO_DIR/data_pipeline"
PYTHON="/home/parijnan/anaconda3/bin/python"
SCRIPT="$PIPELINE_DIR/weekly_option_data_nifty.py"
CONFIG_FILE="$PIPELINE_DIR/config/options_list_nf.csv"
CREDENTIALS="$PIPELINE_DIR/data/user_credentials_icici.csv"
LOG="$PIPELINE_DIR/cron.log"

# ---------------------------------------------------------------------------
# Read Slack token from credentials CSV
# ---------------------------------------------------------------------------
SLACK_TOKEN=$(python3 -c "
import csv
with open('$CREDENTIALS') as f:
    reader = csv.DictReader(f)
    print(next(reader)['slack_token'])
")
SLACK_CHANNEL="#data-alerts"
SLACK_URL="https://slack.com/api/chat.postMessage"

send_slack() {
    curl -s -X POST "$SLACK_URL" \
        -H "Authorization: Bearer $SLACK_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"channel\": \"$SLACK_CHANNEL\", \"text\": \"$1\"}" > /dev/null
}

# ---------------------------------------------------------------------------
# Step 1 — Git pull
# ---------------------------------------------------------------------------
echo "$(date '+%Y-%m-%d %H:%M:%S') Pulling latest from GitHub..." >> "$LOG"
cd "$REPO_DIR"
git pull >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: git pull failed." >> "$LOG"
    send_slack "🚨 *Nifty Downloader* – git pull failed. Check cron.log on laptop."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 2 — Run the Python downloader
# ---------------------------------------------------------------------------
echo "$(date '+%Y-%m-%d %H:%M:%S') Starting Nifty downloader..." >> "$LOG"
$PYTHON "$SCRIPT" >> "$LOG" 2>&1

# ---------------------------------------------------------------------------
# Step 3 — Push options_list_nf.csv only if it was modified
# ---------------------------------------------------------------------------
cd "$REPO_DIR"
if ! git diff --quiet "$CONFIG_FILE"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') options_list_nf.csv modified – pushing to GitHub..." >> "$LOG"
    git add "$CONFIG_FILE"
    git commit -m "Update options_list_nf.csv – $(date '+%Y-%m-%d') run" >> "$LOG" 2>&1
    git push >> "$LOG" 2>&1
    if [ $? -ne 0 ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: git push failed." >> "$LOG"
        send_slack "🚨 *Nifty Downloader* – git push failed after download. Manual push required."
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') options_list_nf.csv pushed to GitHub." >> "$LOG"
    fi
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') options_list_nf.csv unchanged – no push needed." >> "$LOG"
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') Wrapper script complete." >> "$LOG"
