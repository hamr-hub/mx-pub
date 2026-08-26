#!/bin/bash
# Entrypoint: launches Chrome headless in background, runs the publish loop.
set -e

# Generate publish queue if missing
if [ ! -f publish_queue.json ]; then
    echo "[entrypoint] generating publish queue..."
    python3 build_publish_queue.py
fi

# Launch headless Chrome with remote debugging
echo "[entrypoint] launching headless Chrome..."
google-chrome \
    --headless=new \
    --no-sandbox \
    --remote-debugging-port=9222 \
    --user-data-dir=/tmp/chrome-profile \
    --no-first-run \
    --no-default-browser-check \
    --disable-gpu \
    --disable-dev-shm-usage \
    > /tmp/chrome.log 2>&1 &

CHROME_PID=$!
echo "[entrypoint] Chrome PID=$CHROME_PID, waiting for CDP..."
for i in {1..30}; do
    if curl -sf http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
        echo "[entrypoint] Chrome CDP ready"
        break
    fi
    sleep 1
done

# Run the loop (continuous)
echo "[entrypoint] starting publish loop..."
while true; do
    python3 loop_publish_v2.py 2>&1
    echo "[entrypoint] wave done, sleeping 10min..."
    sleep 600
done