"""Gunicorn entry point: wsgi:app (HTTPS via certs in certs/)."""
from app import app, init_db

init_db()

application = app
