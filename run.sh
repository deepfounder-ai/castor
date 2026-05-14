#!/bin/bash
# Auto-restart wrapper for castor web server
cd "$(dirname "$0")"
source .venv/bin/activate

# Set CASTOR_LLM_URL and CASTOR_EMBED_URL if your LLM server is not on localhost
# export CASTOR_LLM_URL="http://your-ip:1234/v1"
# export CASTOR_EMBED_URL="http://your-ip:1234/v1"

while true; do
    echo "[$(date)] Starting castor --web..."
    python3 -u server.py >> logs/web.log 2>&1
    EXIT_CODE=$?
    echo "[$(date)] castor exited with code $EXIT_CODE, restarting in 3s..."
    sleep 3
done
