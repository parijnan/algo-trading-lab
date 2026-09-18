#!/bin/bash

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_DIR="/home/parijnan/scripts/algo-trading-lab"
PIPELINE_DIR="$REPO_DIR/data_pipeline"
PYTHON="/home/parijnan/anaconda3/bin/python"
SCRIPT="$PIPELINE_DIR/data_downloader_mcx.py"
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
SLACK_ERROR_CHANNEL="#error-alerts"
SLACK_URL="https://slack.com/api/chat.postMessage"

send_slack_error() {
    curl -s -X POST "$SLACK_URL" \
        -H "Authorization: Bearer $SLACK_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"channel\": \"$SLACK_ERROR_CHANNEL\", \"text\": \"$1\"}" > /dev/null
}

# ---------------------------------------------------------------------------
# Step 1 — Git pull (picks up code/config changes, e.g. mcx_underlyings.csv
# edits, before running — no data files live in git so there is nothing of
# this script's own output to push back, unlike run_angelone_downloader.sh)
# ---------------------------------------------------------------------------
echo "$(date '+%Y-%m-%d %H:%M:%S') Pulling latest from GitHub..." >> "$LOG"
cd "$REPO_DIR"
git pull >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: git pull failed." >> "$LOG"
    send_slack_error "🚨 *MCX Data Downloader* – git pull failed. Check cron.log on VPS."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 2 — Run the Python downloader
# (Slack success/roll/error notifications to #data-alerts / #error-alerts
# are sent by data_downloader_mcx.py itself, not this wrapper.)
# ---------------------------------------------------------------------------
echo "$(date '+%Y-%m-%d %H:%M:%S') Starting MCX futures downloader..." >> "$LOG"
$PYTHON "$SCRIPT" >> "$LOG" 2>&1
PY_EXIT_CODE=$?

if [ $PY_EXIT_CODE -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: MCX downloader exited non-zero ($PY_EXIT_CODE)." >> "$LOG"
    send_slack_error "🚨 *MCX Data Downloader* – Run failed (exit $PY_EXIT_CODE). Check cron.log on VPS."
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') Wrapper script complete." >> "$LOG"
