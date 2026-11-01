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
  venv/bin/python seed.py
fi

# Gunicorn, one process + threads, HTTPS only on port 5000
exec venv/bin/gunicorn \
  --workers 1 --threads 4 --timeout 120 \
  --bind 0.0.0.0:5000 \
  --certfile certs/cert.pem --keyfile certs/key.pem \
  wsgi:app
