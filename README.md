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

The launcher detects first run: it installs a Python venv, creates both schemas,
seeds the **Admin** user and a small starter catalogue, then serves the site at
**http://localhost:5000** (bound on 0.0.0.0, so reachable from your LAN).

Manual equivalent:

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python seed.py          # Admin + sample books
venv/bin/python app.py           # dev server (debug)
# or: venv/bin/python -m flask --app app run --host 0.0.0.0 --port 5000
```

### Admin account

| Username | Password |
|---|---|
| `Admin` | `Admin123!` |

> ⚠️ Change this after first login for anything beyond local use.
> The Admin is fully privileged: approve/reject submissions, edit/delete any book,
> delete users, promote/demote other admins, and bypass approval when adding books.

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
