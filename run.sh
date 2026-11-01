#!/usr/bin/env bash
# MyLibrary launcher: ensures PostgreSQL is up, applies schema migration,
# generates self-signed certs on first run, then serves over HTTPS.
set -e
cd "$(dirname "$0")"

# Start PostgreSQL if it isn't running
if ! pg_isready -q 2>/dev/null; then
  echo "Starting PostgreSQL…"
  service postgresql start >/dev/null 2>&1 || sudo service postgresql start
  until pg_isready -q 2>/dev/null; do sleep 0.5; done
fi

# Self-signed TLS certificate (covers localhost + 127.0.0.1)
if [ ! -f certs/cert.pem ] || [ ! -f certs/key.pem ]; then
  echo "Generating self-signed TLS certificate…"
  mkdir -p certs
  openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 825 \
    -keyout certs/key.pem -out certs/cert.pem \
    -subj "/CN=MyLibrary" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
fi

# First run: install deps, create tables, seed the Admin user
if [ ! -d venv ]; then
  echo "First run detected — setting up…"
  python3 -m venv venv
  venv/bin/pip install --quiet -r requirements.txt
  venv/bin/python seed.py       # creates the Admin user (skip if restoring a backup)
fi

# Bind target: direct HTTPS on :443 by default; nginx deployments use 127.0.0.1:8000
BIND="${BIND:-0.0.0.0:443}"
WORKERS="${WORKERS:-1}"
THREADS="${THREADS:-4}"
CERTFILE="${CERTFILE:-certs/cert.pem}"
KEYFILE="${KEYFILE:-certs/key.pem}"

exec venv/bin/gunicorn \
  --workers "$WORKERS" --threads "$THREADS" --timeout 120 \
  --bind "$BIND" \
  --certfile "$CERTFILE" --keyfile "$KEYFILE" \
  wsgi:app
