#!/usr/bin/env bash
# MyLibrary launcher: ensures PostgreSQL is up, applies schema, then runs Flask.
set -e
cd "$(dirname "$0")"

# Start PostgreSQL if it isn't running
if ! pg_isready -q 2>/dev/null; then
  echo "Starting PostgreSQL…"
  service postgresql start >/dev/null 2>&1 || sudo service postgresql start
  until pg_isready -q 2>/dev/null; do sleep 0.5; done
fi

# First run: install deps, create tables, seed the Admin user
if [ ! -d venv ]; then
  echo "First run detected — setting up…"
  python3 -m venv venv
  venv/bin/pip install --quiet -r requirements.txt
  venv/bin/python seed.py
fi

exec venv/bin/python -m flask --app app run --host 0.0.0.0 --port 5000
