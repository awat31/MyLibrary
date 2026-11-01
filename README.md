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
**https://localhost:5000** (bound on 0.0.0.0, so reachable from your LAN).

Your browser will show a warning because the certificate is self-signed — this
is expected. Accept/bypass it, or import `certs/cert.pem` as a trusted root
on client machines.

Manual equivalent:

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python seed.py          # Admin + sample books
./run.sh                         # HTTPS server
# dev alternative over plain HTTP:
venv/bin/python app.py
```

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
