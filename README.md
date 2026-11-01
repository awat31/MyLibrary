# 📖 MyLibrary

A whiskybase-style web app — but for **books you own**. A warm, paper-and-mahogany
library where your books sit on dark wooden shelves beside a cosy fire.

Built with **Flask** on **PostgreSQL**, using two linked schemas in one database:

| Schema | Tables | Purpose |
|---|---|---|
| `books` | `books`, `genres`, `book_genres` | The global catalogue — one record per book |
| `users` | `users`, `user_books` | Readers and their shelves (unlimited books per reader) |

**Uniqueness rule:** a book exists exactly once — matched by ISBN first, falling
back to (title + author) when no ISBN is submitted.

## Quick start

```bash
./run.sh
```

The launcher detects first run: it installs a Python venv, generates a
**self-signed TLS certificate** (`certs/cert.pem`, `certs/key.pem`, covering
`localhost` and `127.0.0.1`), creates both schemas, seeds the **Admin** user and
a small starter catalogue, then serves **HTTPS only** with gunicorn at
**https://localhost** (port **443** — the standard HTTPS port; the launcher
requires root or `CAP_NET_BIND_SERVICE` to bind it, which is free in this LXC
container as we run as root).

Your browser will show a warning because the certificate is self-signed — this
is expected. Accept/bypass it, or import `certs/cert.pem` as a trusted root
on client machines.

Manual equivalent:

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python seed.py          # optional sample data
./run.sh                         # HTTPS server (gunicorn)
```

### Configuration overrides

`run.sh` honours environment variables — useful behind systemd or an nginx
proxy: `BIND` (default `0.0.0.0:443`), `WORKERS` (default `1`), `THREADS`
(default `4`), `CERTFILE` / `KEYFILE` (defaults under `certs/`).

### Deploying to another node

A full checklist lives in **`deploy/INSTALL.md`** — target-machine prerequisites,
PostgreSQL schema setup, a ready-made systemd unit (`deploy/mylibrary.service`)
and an optional nginx + Let's Encrypt reverse-proxy config
(`deploy/nginx.conf`). `venv/`, `certs/`, `instance_secret_key` and
`static/uploads/` are node-local (see `.gitignore`) and never travel with
the code.

The app is **HTTPS-only** by design — there is no plain-HTTP mode. For local
development against the Flask dev server use `DEV_HTTP`-free `venv/bin/python app.py`
(debug off unless `FLASK_DEBUG=1`); note its HTTP origin prevents Secure cookies.

### Passwords

- Users change their own password under **Profile → Password** (requires the
  current password, minimum 12 characters).
- The Admin can reset any user's password from the dashboard:
  **Admin → Reset a user's password**.

### Admin account

| Username | Password |
|---|---|
| `Admin` | `Admin123!` |

> ⚠️ Change this after first login (Profile → Password) for anything beyond
> local use. The Admin is fully privileged: approve/reject submissions,
> edit/delete any book, delete users, promote/demote other admins, reset
> passwords, and bypass approval when adding books.

## What users can do

- **Home** — sign in / sign up, live site stats, and the newest catalogue arrivals already sitting on a shelf.
- **Search catalogue** — keyword search across *every* field (title, author, publisher,
  ISBN, year, description, genres) plus author / genre / published-year filters;
  one click shelves any book.
- **Submit a book (Open Library assisted)** — type an ISBN and the form live-fetches
  the details from **Open Library** (title, author, publisher, year, description,
  genres, cover); no ISBN? search by title/author and fill the form from a result
  card. Open Library covers are fetched on demand and cached locally on disk;
  no covers are preloaded at startup, and the disk cache persists across
  restarts. Uploaded image files remain available as a local alternative.
  Nothing is bulk-imported: books enter the catalogue only through
  **Admin approval**.
- **My Shelf** — books rendered sitting on a dark mahogany shelf; hovering lifts a
  book off the shelf. Shelves grow automatically as books are added; scroll down
  for more rows. Shows the date each book entered your library and a
  **⧗ pending** badge for submissions still awaiting approval.
  Filter by published date range, author, genre, or a keyword over every field.
- **Readers** — search users and browse their shelves (read-only, approved books only).
- **Submit a book** — title, author, publisher, year, ISBN, genres (comma separated,
  unlimited), description, and a cover via **file upload or image URL**.
  Submissions land in the Admin's queue; the book appears on the submitter's shelf
  as pending immediately, but stays invisible to everyone else until approved.

## Security hardening (post-pentest)

- Session cookies: `Secure`, `HttpOnly`, `SameSite=Lax`
- CSRF tokens on every state-changing POST (`before_request` check; injected
  client-side from the per-session `<meta>` token by `static/js/csrf.js`)
- Login throttling: >8 failed attempts per IP+username in 15 min → HTTP 429
- Security headers on every response: CSP (`script-src 'self'`), X-Frame-Options
  DENY, nosniff, Referrer-Policy, HSTS
- Secret key: random per-install file (`instance_secret_key`, chmod 600) or the
  `SECRET_KEY` env var — never a hardcoded fallback
- Open-redirect guard on the login `?next=` parameter (site-relative paths only)
- Cover image URLs restricted to `http(s)://`
- Search wildcards (`%`, `_`) are escaped — input always matches literally

## Layout of the code

```
app.py         routes + helpers (auth, search, shelves, submissions, admin)
models.py      SQLAlchemy models across the two schemas
config.py      DB URI, upload rules, schema names
seed.py        idempotent seeding (Admin + starter catalogue)
run.sh         one-command launcher
templates/     Jinja pages (home, auth, search, shelf, submit, admin, book detail)
static/css/    the paper & mahogany theme
static/uploads/ uploaded cover images
```

## Configuration

Environment overrides in `config.py`:

- `DATABASE_URL` — defaults to `postgresql+psycopg2://mylibrary:mylibrary_pw@localhost:5432/mylibrary`
- `SECRET_KEY` — set your own in production
- `COVER_CACHE_MAX_MB` — maximum size of the on-demand cover cache in
  `var/cover_cache/` (default `256`; set to `0` to disable disk caching).
