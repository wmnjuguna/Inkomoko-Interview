#!/bin/sh
# Bring the schema up to date, then hand PID 1 over to gunicorn.
# With arguments (for example the setup service's "python -m integration_service.setup"),
# run those instead of gunicorn, after the migration.
set -eu

alembic upgrade head

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# --timeout must stay above the Fineract call timeout (30 s by default) plus some work.
# No gunicorn access log: the app already writes one JSON line per request.
# No control socket: nothing uses it, and the integration user has no home to put it in.
exec gunicorn \
    --workers 2 \
    --threads 4 \
    --bind 0.0.0.0:8091 \
    --timeout 120 \
    --graceful-timeout 30 \
    --no-control-socket \
    'integration_service.app:create_app()'
