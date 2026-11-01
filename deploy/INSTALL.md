# Deploying MyLibrary to another node

A pull-it-together checklist for a fresh Ubuntu (22.04+/24.04/26.04) LXC, VM or
bare-metal host. Two supported shapes:

- **Option A — direct (simplest):** gunicorn binds `0.0.0.0:443` with the
  self-signed cert. Perfect for a LAN/home lab.
- **Option B — reverse proxy (public):** nginx on 80/443 with a Let's Encrypt
  cert; gunicorn binds `127.0.0.1:8000`. Requires a DNS name pointed at the node.

---

## 1. Get the code into the target node

`venv/`, `certs/`, `instance_secret_key`, and `static/uploads/` are node-local
and should NOT travel with the code (they're git-ignored).

```bash
# From your current node: transfer via git or rsync
rsync -av --exclude venv --exclude certs --exclude instance_secret_key \
      --exclude static/uploads --exclude '__pycache__' \
      /root/MyLibrary/ user@newnode:/srv/mylibrary/
```

(If you use git: push the repo, clone it on the target — see `.gitignore`.)

## 2. Target-node prerequisites

```bash
sudo apt-get update
sudo apt-get install -y postgresql postgresql-contrib python3-venv python3-pip libpq-dev
```

If you plan to serve directly on port 443 (option A) and do not run the app as
root, give gunicorn the capability to bind low ports:

```bash
sudo setcap 'cap_net_bind_service=+ep' /srv/mylibrary/venv/bin/gunicorn
```

## 3. Database on the target

```bash
sudo service postgresql start
sudo -u postgres psql -c "CREATE USER mylibrary WITH PASSWORD 'STRONG_NEW_PW';"
sudo -u postgres psql -c "CREATE DATABASE mylibrary OWNER mylibrary;"
sudo -u postgres psql -d mylibrary \
  -c 'CREATE SCHEMA IF NOT EXISTS books;'
sudo -u postgres psql -d mylibrary \
  -c 'CREATE SCHEMA IF NOT EXISTS users;'
sudo -u postgres psql -d mylibrary \
  -c 'GRANT USAGE, CREATE ON SCHEMA books, users TO mylibrary;'
```

Point the app at it via an environment variable (see step 5):

```
DATABASE_URL=postgresql+psycopg2://mylibrary:STRONG_NEW_PW@localhost:5432/mylibrary
```

## 4. First app run

```bash
cd /srv/mylibrary
sudo install -d -o mylibrary -g mylibrary \
  /srv/mylibrary/static/uploads /srv/mylibrary/var/cover_cache
sudo -u mylibrary ./run.sh          # creates venv, schemas, tables, Admin user
# test with curl -k https://localhost, then Ctrl+C
```

`run.sh` auto-generates a self-signed cert (`certs/`) and a random
`instance_secret_key` (chmod 600) on first run. In production, prefer
`SECRET_KEY` from the environment (step 5) — it survives file-level accidents.

## 5. Environment configuration

Two supported places:
- A systemd `Environment=` line (see deploy/mylibrary.service), or
- `/srv/mylibrary/.env` sourced by your unit/wrapper:

```
SECRET_KEY=<openssl rand -hex 32>
DATABASE_URL=postgresql+psycopg2://mylibrary:STRONG_NEW_PW@localhost:5432/mylibrary
```

- `FLASK_DEBUG` — leave unset in production (debug stays off).
- `COVER_CACHE_MAX_MB` — on-demand cover-cache limit in MB (default `256`; `0` disables it).
- Do NOT set `DEV_HTTP`; the app is HTTPS-only by design.

## 6. Process supervision (systemd)

```bash
sudo cp /srv/mylibrary/deploy/mylibrary.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mylibrary
sudo systemctl status mylibrary
```

Logs: `journalctl -u mylibrary -f`.

## 7A. Option A — direct HTTPS (no proxy) with systemd

Set in the unit: `Environment=BIND=0.0.0.0:443` (the default).
You're done: `https://<node-ip>/` with the self-signed warning, or import
`certs/cert.pem` as a trusted root on clients.

## 7B. Option B — nginx + Let's Encrypt (public host)

Bring a DNS A/AAAA record to point at the node first, then:

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx
sudo cp /srv/mylibrary/deploy/nginx.conf /etc/nginx/sites-available/mylibrary
sudo ln -sf /etc/nginx/sites-available/mylibrary /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d library.example.com
```

Then point gunicorn at the loopback behind nginx by setting in the unit:

```
Environment=BIND=127.0.0.1:8000
Environment=CERTFILE=/etc/letsencrypt/live/library.example.com/fullchain.pem
Environment=KEYFILE=/etc/letsencrypt/live/library.example.com/privkey.pem
```

(`run.sh` honours `BIND`, `WORKERS`, `THREADS`, `CERTFILE`, `KEYFILE` overrides.)

## 8. Backups (cron)

```bash
# /etc/cron.daily/mylibrary-backup
#!/bin/sh
pg_dump -U mylibrary mylibrary | gzip > /backups/mylibrary-$(date +%F).sql.gz
rsync -a /srv/mylibrary/static/uploads /backups/uploads-current/
find /backups -name 'mylibrary-*.sql.gz' -mtime +30 -delete
```

Restore = drop/create DB + schemas, `psql -f dump.sql`, re-run the app
(`init_db()` reapplies idempotent migrations). The cover cache under
`var/cover_cache/` is deliberately not backed up; it is fetched again as covers
are viewed.

## 9. Upgrades

```bash
cd /srv/mylibrary
git pull                 # or rsync the new tree again
./run.sh                 # schema migrations (e.g. new columns) apply on boot
sudo systemctl restart mylibrary
```

Idempotent migrations (`_migrate()` in `app.py`) add missing columns on start.

## 10. Production sanity checklist

- [ ] `journalctl -u mylibrary` shows gunicorn booting without debug banners
- [ ] `curl -k https://localhost/` → 200 (or valid cert behind proxy)
- [ ] Set-Cookie header shows `Secure; HttpOnly; SameSite=Lax`
- [ ] HSTS + CSP present (`curl -skI`)
- [ ] Admin password changed immediately (Profile → Password)
- [ ] Signup works, submission lands as ⧗ pending, Admin approves
- [ ] Backups cron runs manually once: `sudo /etc/cron.daily/mylibrary-backup`
