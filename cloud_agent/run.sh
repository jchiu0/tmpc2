#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi

cd "$SCRIPT_DIR/.."
exec "$SCRIPT_DIR/.venv/bin/python" -m cloud_agent.agent "$@"
