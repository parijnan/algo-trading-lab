#!/bin/bash

# ---------------------------------------------------------------------------
# Fyers auto-token wrapper -- cron entry (daily 06:35 IST, laptop).
#
#   1. Clean stale Chrome profile locks (a killed run leaves Singleton*)
#   2. Run data_pipeline/fyers_auto_token.py (token via persistent-session
#      straight-through or full TOTP auto-login; writes local creds +
#      hestia_data/fyers_token.json and sends its own Slack report)
#   3. Only on success: push hestia_data/fyers_token.json to Delos over ssh
#      stdin (atomic, mode 600 -- same mechanics as the fyers-token skill,
#      which this unattended path supersedes for the daily case) and run the
#      remote `verify` there. delos-ipv6 first, delos-ipv4 on failure.
# ---------------------------------------------------------------------------

set -u
REPO_DIR="/home/parijnan/scripts/algo-trading-lab"
PYTHON="/home/parijnan/anaconda3/bin/python"
SCRIPT="$REPO_DIR/data_pipeline/fyers_auto_token.py"
TOKEN_FILE="$REPO_DIR/hestia_data/fyers_token.json"
PROFILE_DIR="$REPO_DIR/data_pipeline/data/fyers_browser_profile"
LOG="$REPO_DIR/data_pipeline/fyers_auto_token.log"

# Chrome needs the desktop session's display (Garuda/Wayland: both vars).
# If the session isn't up at run time, the Python script's own failure Slack
# alert fires -- no silent fallback to a half-working mode.
export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"

# Single-instance guard (the fd must exist before flock, not be opened by it)
# Lock lives under data_pipeline/data/ (gitignored) so it never shows in git status.
LOCK_FILE="$REPO_DIR/data_pipeline/data/.fyers_auto_token.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date '+%F %T') another run is active; exiting" >> "$LOG"
    exit 0
fi

echo "$(date '+%F %T') === fyers auto-token run starting ===" >> "$LOG"

# 1 -- stale profile locks (a previous killed run leaves these, and Chrome
# then refuses to start: "profile in use")
rm -f "$PROFILE_DIR"/Singleton* 2>>"$LOG"

# 2 -- the token run itself (Python sends success/failure Slack itself)
"$PYTHON" "$SCRIPT" >> "$LOG" 2>&1
PY_EXIT=$?

if [ $PY_EXIT -ne 0 ]; then
    echo "$(date '+%F %T') token run failed (exit $PY_EXIT) -- NOT pushing to Delos." >> "$LOG"
    echo "$(date '+%F %T') === run complete (failure) ===" >> "$LOG"
    exit $PY_EXIT
fi

# 3 -- push to Delos (stdin, never a command line; atomic; mode 600)
echo "$(date '+%F %T') pushing token to delos..." >> "$LOG"
PUSH_OK=0
for HOST in delos-ipv6 delos-ipv4; do
    if ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" \
        'cd ~/scripts/algo-trading-lab && umask 077 && cat > hestia_data/fyers_token.json.tmp && mv hestia_data/fyers_token.json.tmp hestia_data/fyers_token.json' \
        < "$TOKEN_FILE" >> "$LOG" 2>&1; then
        echo "$(date '+%F %T') pushed via $HOST." >> "$LOG"
        PUSH_OK=1
        LAST_HOST="$HOST"
        break
    fi
    echo "$(date '+%F %T') push via $HOST failed; trying fallback." >> "$LOG"
done

if [ $PUSH_OK -eq 0 ]; then
    echo "$(date '+%F %T') ERROR: both delos hosts unreachable -- local token is good but Delos's is stale." >> "$LOG"
    curl -s -X POST "https://slack.com/api/chat.postMessage" \
        -H "Authorization: Bearer $(python3 -c "
import csv
with open('$REPO_DIR/data/user_credentials.csv') as f: print(next(csv.DictReader(f))['slack_token'])")" \
        -H "Content-Type: application/json" \
        -d '{"channel": "#error-alerts", "text": ":rotating_light: *Fyers auto-token* -- token generated locally but the Delos push failed (both hosts unreachable). Delos keeps yesterday/stale token; re-push with the fyers-token skill or re-run the wrapper."}' > /dev/null
    exit 2
fi

# 4 -- remote verify (exists, mode 600, mtime+issued_at today IST, one live
# History call from Delos's own network)
ssh -o ConnectTimeout=10 -o BatchMode=yes "$LAST_HOST" \
    'cd ~/scripts/algo-trading-lab && /home/parijnan/anaconda3/bin/python data_pipeline/fyers_token_refresh.py verify' >> "$LOG" 2>&1
if [ $? -eq 0 ]; then
    echo "$(date '+%F %T') remote verify: token is fresh and works. === run complete ===" >> "$LOG"
    exit 0
else
    echo "$(date '+%F %T') remote verify FAILED -- Delos's copy needs a re-push. === run complete ===" >> "$LOG"
    curl -s -X POST "https://slack.com/api/chat.postMessage" \
        -H "Authorization: Bearer $(python3 -c "
import csv
with open('$REPO_DIR/data/user_credentials.csv') as f: print(next(csv.DictReader(f))['slack_token'])")" \
        -H "Content-Type: application/json" \
        -d '{"channel": "#error-alerts", "text": ":rotating_light: *Fyers auto-token* -- token generated and pushed, but Delos remote verify failed. Re-run the wrapper or check Delos (data_pipeline/fyers_auto_token.log for the verify output)."}' > /dev/null
    exit 3
fi
