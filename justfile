run:
    docker compose up -d --build

dev:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'kill 0' EXIT
    (cd backend && .venv/bin/uvicorn main:app --reload --port 8000) &
    (cd frontend && npm run dev) &
    wait
