#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
unset PYTHONPATH PYTHONHOME
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
if [ ! -x .venv-linux/bin/python ]; then python3 -m venv .venv-linux; fi
.venv-linux/bin/python -m pip install -r requirements.txt
exec .venv-linux/bin/python -m uvicorn rapid.app:app --host 127.0.0.1 --port 8000
