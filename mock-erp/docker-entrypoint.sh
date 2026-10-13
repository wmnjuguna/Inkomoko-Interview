#!/bin/sh
# Bring the schema up to date, then hand PID 1 over to gunicorn.
set -eu

alembic upgrade head

# --timeout must stay above the longest fault delay (120 s).
# No gunicorn access log: the app already writes one JSON line per request.
# No control socket: nothing uses it, and the erp user has no home to put it in.
exec gunicorn \
    --workers 2 \
    --threads 4 \
    --bind 0.0.0.0:8090 \
    --timeout 180 \
    --graceful-timeout 30 \
    --no-control-socket \
    'mock_erp.app:create_app()'
