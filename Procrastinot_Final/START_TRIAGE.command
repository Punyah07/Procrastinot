#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  bash setup_mac.sh || { read -p "Setup failed. Press Enter to close."; exit 1; }
fi
bash run_mac.sh &
server_pid=$!
sleep 3
open 'http://127.0.0.1:8501'
wait "$server_pid"
