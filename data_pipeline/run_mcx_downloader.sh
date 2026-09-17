#!/bin/bash

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_DIR="/home/parijnan/scripts/algo-trading-lab"
PIPELINE_DIR="$REPO_DIR/data_pipeline"
PYTHON="/home/parijnan/anaconda3/bin/python"
SCRIPT="$PIPELINE_DIR/data_downloader_mcx.py"
CREDENTIALS="$PIPELINE_DIR/data/user_credentials_angel.csv"
CONFIG_FILE="$PIPELINE_DIR/config/options_list_sensex.csv"
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
# Step 1 — Send Slack warning (2026-09-17: this run can now modify
# options_list_sensex.csv, a git-tracked config, via the AngelOne equities/
# options phase folded in below — same reason run_angelone_downloader.sh
# has always sent this)
# ---------------------------------------------------------------------------
send_slack_msg "<@$SLACK_MEMBER_ID> ⚠️ *MCX+AngelOne Data Downloader* – Run started. Do not push updates to GitHub until downloads are complete."
echo "$(date '+%Y-%m-%d %H:%M:%S') Slack warning sent." >> "$LOG"

# ---------------------------------------------------------------------------
# Step 2 — Git pull (picks up code/config changes, e.g. mcx_underlyings.csv
# edits, before running)
# ---------------------------------------------------------------------------
echo "$(date '+%Y-%m-%d %H:%M:%S') Pulling latest from GitHub..." >> "$LOG"
cd "$REPO_DIR"
git pull >> "$LOG" 2>&1
if [ $? -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: git pull failed." >> "$LOG"
    send_slack_error "🚨 *MCX+AngelOne Data Downloader* – git pull failed. Check cron.log on VPS."
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 3 — Run the Python downloader (MCX futures, then — same session, no
# second AngelOne login — Sensex/Nifty/India VIX indices and Sensex options,
# folded in 2026-09-17; see data_downloader_mcx.py's own comment at the call
# site and plans/fyers-mcx-data-integration.md §7 for why).
# Slack success/roll/error notifications for both phases are sent by the
# Python scripts themselves, not this wrapper.
# ---------------------------------------------------------------------------
echo "$(date '+%Y-%m-%d %H:%M:%S') Starting MCX+AngelOne downloader..." >> "$LOG"
$PYTHON "$SCRIPT" >> "$LOG" 2>&1
PY_EXIT_CODE=$?

if [ $PY_EXIT_CODE -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: downloader exited non-zero ($PY_EXIT_CODE)." >> "$LOG"
    send_slack_error "🚨 *MCX+AngelOne Data Downloader* – Run failed (exit $PY_EXIT_CODE). Check cron.log on VPS."
fi

# ---------------------------------------------------------------------------
# Step 4 — Push options_list_sensex.csv only if it was modified (its
# download_status column is rewritten by the AngelOne options phase above —
# same push-only-if-changed pattern run_angelone_downloader.sh used to do
# on its own, before this cron slot took over its job)
# ---------------------------------------------------------------------------
cd "$REPO_DIR"
if ! git diff --quiet "$CONFIG_FILE"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') options_list_sensex.csv modified – pushing to GitHub..." >> "$LOG"
    git add "$CONFIG_FILE"
    git commit -m "Update options_list_sensex.csv – $(date '+%Y-%m-%d') run" >> "$LOG" 2>&1
    git push >> "$LOG" 2>&1
    if [ $? -ne 0 ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: git push failed." >> "$LOG"
        send_slack_error "🚨 *MCX+AngelOne Data Downloader* – git push failed after download. Manual push required."
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
    send_slack_msg "<@$SLACK_MEMBER_ID> ✅ *MCX+AngelOne Data Downloader* – Run completed successfully. Safe to push updates to GitHub."
else
    send_slack_msg "<@$SLACK_MEMBER_ID> 🚨 *MCX+AngelOne Data Downloader* – Run completed with errors. Check cron.log on VPS."
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') Wrapper script complete." >> "$LOG"
