#!/usr/bin/env bash
set -e

# Wait for Postgres if configured.
if [ -n "$POSTGRES_HOST" ]; then
  echo "Waiting for Postgres at $POSTGRES_HOST:${POSTGRES_PORT:-5432}..."
  until python -c "import socket,os,sys; s=socket.socket(); s.settimeout(2); s.connect((os.environ['POSTGRES_HOST'], int(os.environ.get('POSTGRES_PORT','5432')))); s.close()" 2>/dev/null; do
    sleep 1
  done
  echo "Postgres is up."
fi

# Only the web container should run migrations/collectstatic (RUN_INIT=1).
if [ "${RUN_INIT:-0}" = "1" ]; then
  python manage.py migrate --noinput
  python manage.py collectstatic --noinput
fi

exec "$@"
