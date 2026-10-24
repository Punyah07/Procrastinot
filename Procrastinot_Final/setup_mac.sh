#!/bin/bash
set -e
cd "$(dirname "$0")"
echo '✳ Setting up TRIAGE on your Mac…'
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3 is required. Install it from https://www.python.org/downloads/'
  exit 1
fi
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt -r tests/requirements-test.txt
./.venv/bin/python -m playwright install chromium
echo ''
echo '✓ TRIAGE installed. Run: bash run_mac.sh'
