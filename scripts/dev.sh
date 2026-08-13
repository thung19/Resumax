#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

case "${1:-}" in
  backend)
    cd "$ROOT_DIR"
    if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
      python3 -m venv "$ROOT_DIR/.venv"
      "$ROOT_DIR/.venv/bin/python" -m pip install -r backend/requirements.txt
    fi
    exec "$ROOT_DIR/.venv/bin/python" -m uvicorn backend.main:app --reload --port 8000
    ;;
  frontend)
    cd "$ROOT_DIR/frontend"
    if [[ ! -d node_modules ]]; then
      npm install
    fi
    exec npm run dev
    ;;
  *)
    echo "Usage: $0 backend|frontend" >&2
    exit 1
    ;;
esac
