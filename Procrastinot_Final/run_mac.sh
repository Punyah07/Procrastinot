#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo 'First run: bash setup_mac.sh'
  exit 1
fi
printf '\n✳ TRIAGE is starting at http://127.0.0.1:8501\n'
printf 'Control + C stops this local server.\n\n'
.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8501
