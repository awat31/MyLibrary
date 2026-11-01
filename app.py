"""
MyLibrary — a bookshelf-style personal library web app.

Run with:  ./run.sh   (or)   venv/bin/python app.py
"""
import os
import json
import hashlib
import re
import secrets
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime

from flask import (
    Flask, render_template, redirect, url_for, request, flash, abort,
    send_from_directory, send_file, current_app, session, get_flashed_messages,
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user,
)
from sqlalchemy import or_, and_, func, cast, String, desc, asc
from sqlalchemy.orm import joinedload, selectinload
from werkzeug.utils import secure_filename

from config import Config
from models import db, ensure_schemas, User, Book, Genre, LibraryEntry, book_genres

MIN_PW = Config.MIN_PASSWORD_LENGTH

# Open Library (Internet Archive) — free ISBN catalogue; queried live whenever a
# user looks a book up, never bulk-imported.
OPENLIBRARY_BASE = "https://openlibrary.org"
COVERS_BASE = "https://covers.openlibrary.org"
OPENLIBRARY_TIMEOUT = 8  # seconds
ISBN_PATTERN = re.compile(r"^\d{9}[\dxX]$|^\d{13}$")
OL_USER_AGENT = "MyLibrary/1.0 (personal library app; live ISBN lookups)"
PAGE_SIZE = 24
ADMIN_PAGE_SIZE = 25
READER_PAGE_SIZE = 30
DB_INIT_LOCK_KEY = 1296849234
ADMIN_STATS_CACHE_SECONDS = 30


# --------------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------------
app = Flask(__name__)
app.config.from_object(Config)
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "error"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def pagination_url(page_parameter: str, page_number: int) -> str:
    values = dict(request.view_args or {})
    values.update(request.args.to_dict())
    values[page_parameter] = page_number
    return url_for(request.endpoint, **values)


@app.context_processor
def inject_globals():
    return {
        "current_year": datetime.now().year,
        "app_name": "MyLibrary",
        "min_pw_length": app.config["MIN_PASSWORD_LENGTH"],
        "pagination_url": pagination_url,
    }


# Display cap for usernames in tightly-sized UI (readers-page cards).
# Real usernames can be longer; we just don't let them blow up the layout.
DISPLAY_NAME_LIMIT = 25


@app.template_filter("shortname")
def shortname(username: str, limit: int = DISPLAY_NAME_LIMIT) -> str:
    """Render a username inside a fixed-size card: beyond `limit` chars it
    is cut and suffixed with '...' (the full name stays in the hover title)."""
    name = username or ""
    return name if len(name) <= limit else name[:limit] + "..."


# --------------------------------------------------------------------------
# Security pipeline: CSRF, login throttling, security headers
# --------------------------------------------------------------------------
def _csrf_token() -> str:
    """Per-session CSRF token; created on demand via the layout <meta> tag
    and injected into every <form> by /static/js/csrf.js before submission."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(16)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = _csrf_token


@app.before_request
def csrf_protect():
    """Every state-changing request must present the per-session token
    (form field `csrf_token` or header `X-CSRFToken`)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
        if not sent or not secrets.compare_digest(
                sent, session.get("_csrf_token", "")):
            abort(400, "CSRF token missing or invalid.")


_login_attempts: dict[str, list[float]] = {}


def _login_is_throttled(key: str) -> bool:
    """Sliding-window throttle on failed sign-ins (per IP + username)."""
    now = time.time()
    window = app.config["LOGIN_WINDOW_SECONDS"]
    hits = [t for t in _login_attempts.get(key, []) if now - t < window]
    _login_attempts[key] = hits
    return len(hits) >= app.config["MAX_LOGIN_ATTEMPTS"]


def _record_failed_login(key: str):
    now = time.time()
    window = app.config["LOGIN_WINDOW_SECONDS"]
    hits = [t for t in _login_attempts.get(key, []) if now - t < window]
    hits.append(now)
    _login_attempts[key] = hits


def _clear_failed_logins(key: str):
    _login_attempts.pop(key, None)


@app.after_request
def apply_security_headers(response):
    for name, value in app.config["SECURE_HEADERS"].items():
        response.headers[name] = value
    return response


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def normalize_isbn(raw: str) -> str:
    """Strip dashes/spaces; uppercase X (ISBN-10 check digit)."""
    return "".join(c for c in (raw or "") if c.isalnum()).upper()


def slug_for_spine(text: str) -> int:
    """Deterministic hash for colouring cover-less book spines."""
    h = 0
    for ch in (text or "x"):
        h = (h * 31 + ord(ch)) & 0xFFFF
    return h % 6


app.jinja_env.globals["spine_variant"] = slug_for_spine


def like_escape(raw: str) -> str:
    """Escape LIKE wildcards so user input matches literally, not as patterns."""
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_genres(raw: str):
    """Turn 'Fantasy, history, epic' into Genre objects, creating new ones."""
    names, out = set(), []
    for part in (raw or "").split(","):
        name = unicodedata.normalize("NFKC", part).strip().title()
        if not name or len(name) > 64 or name.lower() in names:
            continue
        names.add(name.lower())
        genre = Genre.query.filter(func.lower(Genre.name) == name.lower()).first()
        if not genre:
            genre = Genre(name=name)
            db.session.add(genre)
        out.append(genre)
    return out


def find_duplicate_book(isbn: str, title: str, author: str, exclude_id=None):
    """One record per book: match on ISBN first, then title+author."""
    query = Book.query
    if exclude_id:
        query = query.filter(Book.id != exclude_id)
    if isbn:
        hit = query.filter(Book.isbn == isbn).first()
        if hit:
            return hit
    return query.filter(
        func.lower(Book.title) == title.strip().lower(),
        func.lower(Book.author) == author.strip().lower(),
    ).first()


def clean_search(value: str):
    return (value or "").strip()


def paginate_query(query, page_parameter: str = "page", per_page: int = PAGE_SIZE):
    page = max(1, request.args.get(page_parameter, 1, type=int) or 1)
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    if pagination.pages and page > pagination.pages:
        pagination = query.paginate(
            page=pagination.pages, per_page=per_page, error_out=False,
        )
    return pagination


def search_books(
    q: str = None, genre: str = None, author: str = None,
    year_min=None, year_max=None, approved_only: bool = True,
    library_of: User = None,
):
    """Flexible book query.

    `q` is a general keyword search across every field in the books database:
    title, author, publisher, ISBN, year, description and genre names.
    `library_of` restricts results to one user's shelf (joined with LibraryEntry
    so pending books submitted by that user are included).
    """
    query = Book.query.options(selectinload(Book.genres))
    if library_of is not None:
        query = (
            query.join(LibraryEntry, LibraryEntry.book_id == Book.id)
            .filter(LibraryEntry.user_id == library_of.id)
        )
        visible = or_(
            Book.approved.is_(True),
            and_(Book.approved.is_(False), Book.submitted_by_id == library_of.id),
        )
        query = query.filter(visible)
    elif approved_only:
        query = query.filter(Book.approved.is_(True))

    if genre:
        query = (
            query.join(book_genres, book_genres.c.book_id == Book.id)
            .join(Genre, Genre.id == book_genres.c.genre_id)
            .filter(func.lower(Genre.name) == genre.lower())
        )
    if author:
        query = query.filter(func.lower(Book.author) == author.lower())
    if year_min:
        try:
            query = query.filter(Book.published_year >= int(year_min))
        except (TypeError, ValueError):
            pass
    if year_max:
        try:
            query = query.filter(Book.published_year <= int(year_max))
        except (TypeError, ValueError):
            pass
    if q:
        like = f"%{like_escape(q)}%"
        escape_kw = "\\"
        genre_match = (
            db.session.query(book_genres.c.book_id)
            .join(Genre, Genre.id == book_genres.c.genre_id)
            .filter(
                book_genres.c.book_id == Book.id,
                Genre.name.ilike(like, escape=escape_kw),
            )
            .exists()
        )
        query = query.filter(
            or_(
                Book.title.ilike(like, escape=escape_kw),
                Book.author.ilike(like, escape=escape_kw),
                Book.publisher.ilike(like, escape=escape_kw),
                Book.description.ilike(like, escape=escape_kw),
                Book.isbn.ilike(like, escape=escape_kw),
                cast(Book.published_year, String).ilike(
                    like, escape=escape_kw),
                genre_match,
            )
        )
    return query.order_by(asc(Book.title), asc(Book.author))


def save_cover_file(file_storage) -> str | None:
    """Validated cover upload; returns stored filename or None."""
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(file_storage.filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in app.config["ALLOWED_COVER_EXTENSIONS"]:
        flash("Cover must be an image (png, jpg, jpeg, webp, gif).", "error")
        return None
    try:
        from PIL import Image
        Image.open(file_storage.stream).verify()
        file_storage.stream.seek(0)
    except Exception:
        flash("That cover file doesn't look like a valid image.", "error")
        return None
    stored = f"{uuid.uuid4().hex[:12]}_{filename}"
    file_storage.save(os.path.join(app.config["UPLOAD_FOLDER"], stored))
    return stored


def delete_cover_file(filename: str):
    if filename:
        path = os.path.join(app.config["UPLOAD_FOLDER"], secure_filename(filename))
        if os.path.exists(path):
            os.remove(path)


def safe_cover_url(raw: str) -> str | None:
    """External covers must be http(s) URLs (blocks javascript:, data:, file:)."""
    cleaned = clean_search(raw)
    if cleaned and cleaned.lower().startswith(("http://", "https://")):
        return cleaned
    if cleaned:
        flash("Cover URL ignored — only http:// and https:// links are allowed.",
              "error")
    return None


def form_to_book_data(form):
    return {
        "title": clean_search(form.get("title")),
        "author": clean_search(form.get("author")),
        "publisher": clean_search(form.get("publisher")) or None,
        "published_year": int(form["published_year"]) if form.get("published_year", "").isdigit() else None,
        "isbn": normalize_isbn(form.get("isbn")) or None,
        "description": clean_search(form.get("description")) or None,
        "genres": parse_genres(form.get("genres")),
        "cover_url": safe_cover_url(form.get("cover_url")),
    }


# --------------------------------------------------------------------------
# Open Library ISBN lookup (live, on-demand — no bulk import)
# --------------------------------------------------------------------------
def _ol_json(path: str) -> dict:
    request = urllib.request.Request(
        OPENLIBRARY_BASE + path,
        headers={"User-Agent": OL_USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=OPENLIBRARY_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _first(value):
    if isinstance(value, list) and value:
        return value[0]
    return value if value else None


def _authors_from(doc) -> str | None:
    names = doc.get("author_name") or []
    if not names:
        return None
    return ", ".join(names[:2]) + (" et al." if len(names) > 2 else "")


def _normalize_ol_description(raw) -> str | None:
    if isinstance(raw, dict):
        raw = raw.get("value")
    if not isinstance(raw, str):
        return None
    # Strip residual wiki markup; keep it plain prose.
    cleaned = re.sub(r"\{\{[^}]*\}\}", "", raw)
    cleaned = re.sub(r"'''?", "", cleaned)
    return cleaned.strip()[:4000] or None


def _isbns_from(doc, wanted: str) -> str | None:
    """Prefer the queried ISBN form so uniqueness checks match what was typed."""
    for source in (doc.get("isbn13"), doc.get("isbn")):
        for candidate in (source if isinstance(source, list) else [source]):
            if isinstance(candidate, str) and normalize_isbn(candidate) == wanted:
                return wanted
    return _first(doc.get("isbn13")) or _first(doc.get("isbn"))


def _ol_fields_for_doc(doc: dict, wanted_isbn: str = "") -> dict:
    fields = {
        "isbn": _isbns_from(doc, wanted_isbn) or None,
        "title": doc.get("title"),
        "author": _authors_from(doc),
        "publisher": _first(doc.get("publisher")),
        "published_year": doc.get("first_publish_year"),
        "cover_url": (f"{COVERS_BASE}/b/id/{doc['cover_i']}-L.jpg"
                      if doc.get("cover_i") else None),
        "description": None,
        "genres": [],
    }
    work_key = doc.get("key")
    if work_key:
        try:
            work = _ol_json(f"{work_key}.json")
            fields["description"] = _normalize_ol_description(work.get("description"))
            subjects = work.get("subjects") or []
            picked = []
            for subject in subjects:
                name = unicodedata.normalize("NFKC", str(subject)).strip().title()[:64]
                if name and len(picked) < 3 and name not in picked:
                    picked.append(name)
            fields["genres"] = picked
        except Exception:
            pass  # work detail is best-effort metadata enrichment
    return fields


def _ol_lookup_isbn(isbn: str) -> dict | None:
    query = urllib.parse.urlencode({
        "isbn": isbn,
        "fields": "key,title,author_name,publisher,first_publish_year,cover_i,isbn,isbn13",
        "limit": 1,
    })
    data = _ol_json(f"/search.json?{query}")
    docs = data.get("docs") or []
    if not docs:
        return None
    return _ol_fields_for_doc(docs[0], wanted_isbn=isbn)


def _ol_keyword_search(query_text: str, limit: int = 5) -> list[dict]:
    query = urllib.parse.urlencode({
        "q": query_text,
        "fields": "key,title,author_name,first_publish_year,cover_i,isbn,isbn13",
        "limit": limit,
    })
    data = _ol_json(f"/search.json?{query}")
    docs = data.get("docs") or []
    results = []
    for doc in docs:
        results.append(_ol_fields_for_doc(doc))
    return results


@app.route("/api/books/lookup", methods=["GET"])
@login_required
def api_ol_lookup():
    """ISBN -> Open Library fields (+ whether the book already exists locally)."""
    isbn = normalize_isbn(request.args.get("isbn", ""))
    if not ISBN_PATTERN.fullmatch(isbn):
        return {"error": "invalid_isbn"}, 400
    try:
        fields = _ol_lookup_isbn(isbn)
    except Exception:
        return {"error": "openlibrary_unreachable"}, 502
    if not fields:
        return {"error": "not_found"}, 404
    duplicate = find_duplicate_book(fields.get("isbn") or "", fields["title"] or "",
                                    fields["author"] or "")
    fields["duplicate"] = (
        {"id": duplicate.id, "title": duplicate.title, "author": duplicate.author}
        if duplicate else None
    )
    return fields


@app.route("/api/books/search", methods=["GET"])
@login_required
def api_ol_search():
    """Keyword search against Open Library (title/author discovery)."""
    query_text = clean_search(request.args.get("q"))
    if not query_text:
        return {"error": "empty_query"}, 400
    try:
        return {"results": _ol_keyword_search(query_text)}
    except Exception:
        return {"error": "openlibrary_unreachable"}, 502


# --------------------------------------------------------------------------
# Local cover cache — covers fetched by/when a reader asks, never pre-seeded.
# Hit rate-heavy pages (home/readers/search) serve covers at LAN speed after
# the first view. The LRU trim means infrequently viewed books stop accruing
# storage once the budget is met.
# --------------------------------------------------------------------------
COVER_CACHE_HOST = "covers.openlibrary.org"
COVER_CACHE_TYPES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif",
}
_cover_locks_guard = threading.Lock()
_cover_locks: dict[str, tuple[threading.Lock, int]] = {}


@contextmanager
def _cover_fetch_lock(url: str):
    """Share one in-flight fetch per URL among threads in this worker."""
    with _cover_locks_guard:
        existing = _cover_locks.get(url)
        if existing:
            lock, references = existing
        else:
            lock, references = threading.Lock(), 0
        _cover_locks[url] = (lock, references + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _cover_locks_guard:
            current_lock, references = _cover_locks[url]
            if references == 1:
                del _cover_locks[url]
            else:
                _cover_locks[url] = (current_lock, references - 1)


def _cover_cache_hit(url: str) -> str | None:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_dir = current_app.config["COVER_CACHE_DIR"]
    for extension in COVER_CACHE_TYPES.values():
        path = os.path.join(cache_dir, digest + extension)
        if os.path.isfile(path):
            return path
    return None


def _is_cacheable_cover_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        return (
            parts.scheme.lower() == "https"
            and parts.hostname == COVER_CACHE_HOST
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
        )
    except ValueError:
        return False


def _is_trusted_cover_redirect_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        hostname = parts.hostname or ""
        return (
            parts.scheme.lower() == "https"
            and (hostname == "archive.org" or hostname.endswith(".archive.org"))
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
        )
    except ValueError:
        return False


class _CoverRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not (_is_cacheable_cover_url(newurl)
                or _is_trusted_cover_redirect_url(newurl)):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _cover_cache_store(url: str) -> str | None:
    """Fetch an Open Library cover into the cache using an atomic replacement."""
    if not _is_cacheable_cover_url(url):
        return None
    request = urllib.request.Request(
        url, headers={"User-Agent": OL_USER_AGENT},
    )
    opener = urllib.request.build_opener(_CoverRedirectHandler())
    with opener.open(
            request, timeout=current_app.config["COVER_FETCH_TIMEOUT"]) as response:
        if not (_is_cacheable_cover_url(response.geturl())
                or _is_trusted_cover_redirect_url(response.geturl())):
            return None
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        extension = COVER_CACHE_TYPES.get(content_type)
        if not extension:
            return None
        data = response.read(current_app.config["COVER_FETCH_MAX_BYTES"] + 1)
    if not data or len(data) > current_app.config["COVER_FETCH_MAX_BYTES"]:
        return None
    budget = current_app.config["COVER_CACHE_MAX_MB"] * 1024 * 1024
    if budget <= 0 or len(data) > budget:
        return None
    cache_dir = current_app.config["COVER_CACHE_DIR"]
    os.makedirs(cache_dir, exist_ok=True)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    target = os.path.join(cache_dir, digest + extension)
    fd, tmp = tempfile.mkstemp(prefix=f".{digest}.", suffix=".part", dir=cache_dir)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return target


def _cover_cache_bump(path: str) -> None:
    """Mark a cached cover as recently used for LRU eviction."""
    try:
        os.utime(path, None)
    except OSError:
        current_app.logger.warning("Could not update cover-cache access time: %s", path)


def _cover_cache_trim(protected_path: str | None = None) -> None:
    """Keep the cache within its byte budget. mtime is bumped on every hit, so
    eviction is least-recently-viewed-first: frequently seen covers stay warm,
    ones no one looks at free their space."""
    cache_dir = current_app.config["COVER_CACHE_DIR"]
    try:
        entries = []
        total = 0
        for name in os.listdir(cache_dir):
            if name.startswith(".") or not any(
                    name.endswith(extension) for extension in COVER_CACHE_TYPES.values()):
                continue
            path = os.path.join(cache_dir, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            entries.append((stat.st_size, stat.st_mtime, path))
            total += stat.st_size
        budget = current_app.config["COVER_CACHE_MAX_MB"] * 1024 * 1024
        if total <= budget:
            return
        for size, _mtime, path in sorted(entries, key=lambda e: e[1]):
            if total <= budget:
                break
            if path == protected_path:
                continue
            try:
                os.remove(path)
                total -= size
            except OSError:
                current_app.logger.warning("Could not evict cover-cache file: %s", path)
    except OSError:
        current_app.logger.warning("Could not inspect cover cache at %s", cache_dir,
                                   exc_info=True)


def _cover_response(path: str):
    """Serve a cached cover with a long-lived browser cache header — the ?v=
    fingerprint in /cover/<id> URLs changes whenever a cover's source changes,
    so stale images can never outlive their URL."""
    response = send_file(path)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def _cover_visible(book: Book) -> bool:
    """Same visibility rule as /book/<id>: approved covers are public; a
    pending book's cover only appears on the submitter's shelf (or Admin's)."""
    if book.approved:
        return True
    return (getattr(current_user, "is_authenticated", False)
            and (current_user.is_admin or current_user.id == book.submitted_by_id))


def _serve_remote_cover(url: str):
    if current_app.config["COVER_CACHE_MAX_MB"] > 0:
        try:
            cached = _cover_cache_hit(url)
            if cached:
                _cover_cache_bump(cached)
                return _cover_response(cached)
            with _cover_fetch_lock(url):
                cached = _cover_cache_hit(url)
                if cached:
                    _cover_cache_bump(cached)
                    return _cover_response(cached)
                path = _cover_cache_store(url)
                if path:
                    _cover_cache_trim(protected_path=path)
                    return _cover_response(path)
        except (OSError, urllib.error.URLError, ValueError):
            current_app.logger.warning(
                "Could not cache cover; using remote URL",
                exc_info=True,
            )
    return redirect(url)


@app.route("/cover-preview")
@login_required
def cover_preview():
    """Cache Open Library covers shown in the temporary lookup-result cards."""
    url = request.args.get("url", "")
    if not _is_cacheable_cover_url(url):
        abort(404)
    return _serve_remote_cover(url)


@app.route("/cover/<int:book_id>")
def cover_proxy(book_id: int):
    """Cover images, served through the local cache. Local uploads stream
    straight from static/uploads; remote covers come from the LRU disk cache
    (fetched from Open Library on this first request). Anything uncachable
    falls back to a direct redirect to the hotlink."""
    book = db.session.get(Book, book_id)
    if not book or not _cover_visible(book):
        abort(404)
    if not book.cover_url:
        if book.cover_filename:
            return send_from_directory(app.config["UPLOAD_FOLDER"],
                                       secure_filename(book.cover_filename))
        abort(404)
    return _serve_remote_cover(book.cover_url)


def apply_book_fields(book: Book, data: dict):
    book.title = data["title"]
    book.author = data["author"]
    book.publisher = data["publisher"]
    book.published_year = data["published_year"]
    if data["isbn"]:
        book.isbn = data["isbn"]
    book.description = data["description"]
    book.cover_url = data["cover_url"]
    book.genres = data["genres"]


def owned_book_ids(user: User, book_ids: list[int] | None = None) -> set[int]:
    if not user or not user.is_authenticated:
        return set()
    query = db.session.query(LibraryEntry.book_id).filter(
        LibraryEntry.user_id == user.id
    )
    if book_ids is not None:
        if not book_ids:
            return set()
        query = query.filter(LibraryEntry.book_id.in_(book_ids))
    return {book_id for (book_id,) in query.all()}


def entry_for(user: User, book_id: int) -> LibraryEntry | None:
    return LibraryEntry.query.filter_by(user_id=user.id, book_id=book_id).first()


def require_admin():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)


_admin_stats_lock = threading.Lock()
_admin_stats_expires = 0.0
_admin_stats_value: dict[str, int] | None = None


def admin_stats():
    global _admin_stats_expires, _admin_stats_value
    with _admin_stats_lock:
        now = time.monotonic()
        if _admin_stats_value is not None and now < _admin_stats_expires:
            return dict(_admin_stats_value)
        stats = db.session.query(
            db.select(func.count(Book.id))
            .where(Book.approved.is_(True))
            .scalar_subquery().label("books"),
            db.select(func.count(Book.id))
            .where(Book.approved.is_(False))
            .scalar_subquery().label("pending"),
            db.select(func.count(User.id)).scalar_subquery().label("users"),
            db.select(func.count(LibraryEntry.id)).scalar_subquery().label("shelved"),
        ).one()
        _admin_stats_value = {
            "books": stats.books,
            "pending": stats.pending,
            "users": stats.users,
            "shelved": stats.shelved,
        }
        _admin_stats_expires = now + ADMIN_STATS_CACHE_SECONDS
        return dict(_admin_stats_value)


# --------------------------------------------------------------------------
# Home & auth
# --------------------------------------------------------------------------
@app.route("/")
def home():
    recent = (
        Book.query.filter_by(approved=True)
        .order_by(desc(Book.created_at))
        .limit(8)
        .all()
    )
    stats = admin_stats()
    owned = (
        owned_book_ids(current_user, [book.id for book in recent])
        if current_user.is_authenticated else set()
    )
    return render_template(
        "home.html", recent_books=recent, stats=stats, owned_ids=owned
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    if request.method == "POST":
        username = clean_search(request.form.get("username"))
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not username or len(username) > 64:
            flash("Please choose a username (max 64 characters).", "error")
        elif not password or len(password) < MIN_PW:
            flash(f"Password must be at least {MIN_PW} characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif User.query.filter_by(username_lower=username.lower()).first():
            flash("That username is already taken.", "error")
        else:
            user = User(username=username, username_lower=username.lower())
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash("Welcome to MyLibrary! Your shelf awaits.", "success")
            return redirect(url_for("my_library"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    if request.method == "POST":
        username = clean_search(request.form.get("username"))
        # Throttle: too many recent failures from this IP+username -> refuse.
        throttle_key = f"{request.remote_addr}:{username.lower()}"
        if _login_is_throttled(throttle_key):
            flash("Too many failed sign-in attempts — please wait 15 minutes.",
                  "error")
            return render_template("login.html"), 429
        user = User.query.filter_by(username_lower=username.lower()).first()
        if user and user.check_password(request.form.get("password", "")):
            _clear_failed_logins(throttle_key)
            login_user(user, remember=bool(request.form.get("remember")))
            flash(f"Welcome back, {user.username}!", "success")
            nxt = request.args.get("next")
            # Open-redirect guard: only real site-relative paths allowed.
            if nxt and (not nxt.startswith("/") or nxt.startswith("//")
                        or "\\" in nxt):
                nxt = None
            return redirect(nxt or url_for("my_library"))
        _record_failed_login(throttle_key)
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "success")
    return redirect(url_for("home"))


# --------------------------------------------------------------------------
# Book search (global catalogue)
# --------------------------------------------------------------------------
@app.route("/search")
@login_required
def search():
    q = clean_search(request.args.get("q"))
    genre = clean_search(request.args.get("genre"))
    author = clean_search(request.args.get("author"))
    year_min = clean_search(request.args.get("year_min"))
    year_max = clean_search(request.args.get("year_max"))
    pagination = paginate_query(search_books(
        q=q, genre=genre, author=author, year_min=year_min, year_max=year_max,
    ))
    books = pagination.items
    all_genres = Genre.query.order_by(Genre.name).all()
    authors = sorted({book.author for book in books}, key=str.casefold)
    return render_template(
        "search.html", books=books, pagination=pagination,
        genres=all_genres, authors=authors,
        owned_ids=owned_book_ids(current_user, [book.id for book in books]),
        filters={"q": q, "genre": genre, "author": author,
                 "year_min": year_min, "year_max": year_max},
    )


# --------------------------------------------------------------------------
# Profile & privacy
# --------------------------------------------------------------------------
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    shelf_count = current_user.library.count()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "visibility":
            current_user.is_searchable = bool(request.form.get("is_searchable"))
            db.session.commit()
            if current_user.is_searchable:
                flash("You are now visible in the Readers list.", "success")
            else:
                flash("You are now hidden from the Readers list — "
                      "your shelf is viewable only by you and the Admin.", "success")
        elif action == "password":
            current_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            confirm = request.form.get("confirm", "")
            if not current_user.check_password(current_pw):
                flash("Your current password is incorrect.", "error")
            elif len(new_pw) < MIN_PW:
                flash(f"New password must be at least {MIN_PW} characters.", "error")
            elif new_pw != confirm:
                flash("New passwords do not match.", "error")
            else:
                current_user.set_password(new_pw)
                db.session.commit()
                flash("Password updated.", "success")
        return redirect(url_for("profile"))
    return render_template("profile.html", shelf_count=shelf_count)


# --------------------------------------------------------------------------
# Library (own shelf + other readers' shelves)
# --------------------------------------------------------------------------
def _library_view(owner: User):
    q = clean_search(request.args.get("q"))
    genre = clean_search(request.args.get("genre"))
    author = clean_search(request.args.get("author"))
    year_min = clean_search(request.args.get("year_min"))
    year_max = clean_search(request.args.get("year_max"))
    pagination = paginate_query(search_books(
        q=q, genre=genre, author=author, year_min=year_min, year_max=year_max,
        approved_only=False if owner.id == current_user.id else True,
        library_of=owner,
    ))
    books = pagination.items
    entries = LibraryEntry.query.filter(
        LibraryEntry.user_id == owner.id,
        LibraryEntry.book_id.in_([book.id for book in books]),
    ).all() if books else []
    entries_by_book = {entry.book_id: entry for entry in entries}
    shelf_books = [
        (entries_by_book[book.id], book)
        for book in books if book.id in entries_by_book
    ]
    all_genres = Genre.query.order_by(Genre.name).all()
    authors = sorted({book.author for book in books}, key=str.casefold)
    return render_template(
        "library.html",
        owner=owner, entries=shelf_books, pagination=pagination,
        genres=all_genres, authors=authors,
        is_own_shelf=owner.id == current_user.id,
        filters={"q": q, "genre": genre, "author": author,
                 "year_min": year_min, "year_max": year_max},
    )


@app.route("/my-library")
@login_required
def my_library():
    return _library_view(current_user)


@app.route("/readers")
@login_required
def readers():
    q = clean_search(request.args.get("q"))
    if not current_user.is_admin:
        query = User.query.filter(
            User.is_searchable.is_(True),
            User.is_admin.is_(False),
        )
    else:
        query = User.query
    if q:
        query = query.filter(User.username_lower.ilike(f"%{like_escape(q.lower())}%", escape="\\"))
    pagination = paginate_query(
        query.order_by(User.username_lower), "readers_page", READER_PAGE_SIZE,
    )
    users = pagination.items
    counts = {}
    if users:
        counts = dict(
            db.session.query(LibraryEntry.user_id, func.count())
            .filter(LibraryEntry.user_id.in_([user.id for user in users]))
            .group_by(LibraryEntry.user_id).all()
        )
    return render_template(
        "readers.html", users=users, counts=counts, q=q,
        pagination=pagination,
    )


@app.route("/readers/<int:user_id>")
@login_required
def reader_library(user_id: int):
    owner = db.session.get(User, user_id)
    if not owner:
        abort(404)
    # Hidden users' and admin accounts' shelves are only open to the owner
    # and to Admins — everyone else gets a plain 404, so URL guessing
    # reveals nothing.
    if (
        (not owner.is_searchable or owner.is_admin)
        and owner.id != current_user.id
        and not current_user.is_admin
    ):
        abort(404)
    return _library_view(owner)


# --------------------------------------------------------------------------
# Adding/removing books from your shelf
# --------------------------------------------------------------------------
@app.route("/library/add/<int:book_id>", methods=["POST"])
@login_required
def library_add(book_id: int):
    book = db.session.get(Book, book_id)
    if not book or (not book.approved and book.submitted_by_id != current_user.id):
        abort(404)
    if not entry_for(current_user, book.id):
        db.session.add(LibraryEntry(user_id=current_user.id, book_id=book.id))
        db.session.commit()
        flash(f"Added “{book.title}” to your shelf.", "success")
    else:
        flash("That book is already on your shelf.", "error")
    return redirect(request.referrer or url_for("my_library"))


@app.route("/library/remove/<int:book_id>", methods=["POST"])
@login_required
def library_remove(book_id: int):
    entry = entry_for(current_user, book_id)
    if entry:
        book = entry.book
        db.session.delete(entry)
        # Pending books only live via their submitter's shelf entry.
        if book and not book.approved and book.submitted_by_id == current_user.id:
            db.session.delete(book)
        db.session.commit()
        flash(f"Removed “{book.title if book else 'book'}” from your shelf.", "success")
    return redirect(request.referrer or url_for("my_library"))


# --------------------------------------------------------------------------
# Themed errors
# --------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(e):
    is_reader_url = request.path.startswith("/readers")
    if is_reader_url:
        title, message, trail = (
            "Reader not found",
            "No reader lives at that corner of the library. The shelf number "
            "may be wrong, their profile may be hidden, or they were never here at all.",
            "Try browsing the Readers list instead.",
        )
    else:
        title, message, trail = (
            "This page has been loaned out",
            "We looked in every aisle, but whatever you were after isn't on the "
            "shelves. The link may be old, mistyped, or quietly retired.",
            "Head back to your shelf and try again from there.",
        )
    return render_template("error_404.html", err_title=title, message=message, trail=trail), 404


# --------------------------------------------------------------------------
# Book details
# --------------------------------------------------------------------------
@app.route("/book/<int:book_id>")
@login_required
def book_detail(book_id: int):
    book = db.session.get(Book, book_id)
    if not book or (not book.approved and book.submitted_by_id != current_user.id
                    and not current_user.is_admin):
        abort(404)
    holder_count = book.owners.count() if book.approved else None
    return render_template(
        "book_detail.html", book=book,
        owned_ids=owned_book_ids(current_user, [book.id]),
        holder_count=holder_count,
    )


@app.route("/book/<int:book_id>/edit", methods=["GET", "POST"])
@login_required
def book_edit(book_id: int):
    require_admin()
    book = db.session.get(Book, book_id)
    if not book:
        abort(404)
    if request.method == "POST":
        data = form_to_book_data(request.form)
        if not data["title"] or not data["author"]:
            flash("Title and author are required.", "error")
            return render_template("submit.html", book=book, editing=True,
                                   genres=Genre.query.order_by(Genre.name).all())
        duplicate = find_duplicate_book(data["isbn"], data["title"], data["author"],
                                        exclude_id=book.id)
        if duplicate:
            flash(f"A record for this book already exists (#{duplicate.id}).", "error")
            return redirect(url_for("book_edit", book_id=book.id))
        new_cover = save_cover_file(request.files.get("cover"))
        if new_cover:
            delete_cover_file(book.cover_filename)
            book.cover_filename = new_cover
            book.cover_url = None
        apply_book_fields(book, data)
        db.session.commit()
        flash("Book updated.", "success")
        return redirect(url_for("book_detail", book_id=book.id))
    return render_template("submit.html", book=book, editing=True,
                           genres=Genre.query.order_by(Genre.name).all())


# --------------------------------------------------------------------------
# Submitting new books (goes to Admin for approval)
# --------------------------------------------------------------------------
@app.route("/submit", methods=["GET", "POST"])
@login_required
def submit():
    if request.method == "POST":
        data = form_to_book_data(request.form)
        if not data["title"] or not data["author"]:
            flash("Title and author are required.", "error")
            return render_template("submit.html", genres=Genre.query.order_by(Genre.name).all())
        duplicate = find_duplicate_book(data["isbn"], data["title"], data["author"])
        if duplicate:
            already = bool(entry_for(current_user, duplicate.id))
            flash(
                "This book already exists in the catalogue — see it here: "
                f"{duplicate.title} ({duplicate.author})."
                + ("" if already else " You can add it to your shelf from its page."),
                "error",
            )
            return render_template(
                "submit.html", duplicate=duplicate, genres=Genre.query.order_by(Genre.name).all(),
                form_data=request.form,
            )
        cover_file = save_cover_file(request.files.get("cover"))
        book = Book(
            title=data["title"], author=data["author"],
            publisher=data["publisher"], published_year=data["published_year"],
            isbn=data["isbn"], description=data["description"],
            cover_filename=cover_file, cover_url=None if cover_file else data["cover_url"],
            approved=current_user.is_admin,  # admins add straight to the catalogue
            submitted_by_id=current_user.id,
        )
        book.genres = data["genres"]
        db.session.add(book)
        db.session.flush()
        db.session.add(LibraryEntry(user_id=current_user.id, book_id=book.id))
        db.session.commit()
        if book.approved:
            flash("Book added to the catalogue.", "success")
        else:
            flash("Thank you! Your submission was sent to the Admin for approval. "
                  "It is already visible on your shelf as pending.", "success")
        return redirect(url_for("book_detail", book_id=book.id))
    return render_template("submit.html", genres=Genre.query.order_by(Genre.name).all())


# --------------------------------------------------------------------------
# Admin dashboard
# --------------------------------------------------------------------------
@app.route("/admin")
@login_required
def admin():
    require_admin()
    pending_page = paginate_query(
        Book.query.filter_by(approved=False)
        .options(joinedload(Book.submitted_by))
        .order_by(asc(Book.created_at)),
        "pending_page", ADMIN_PAGE_SIZE,
    )
    books_page = paginate_query(
        Book.query.order_by(desc(Book.created_at)),
        "books_page", ADMIN_PAGE_SIZE,
    )
    users_page = paginate_query(
        User.query.order_by(asc(User.username_lower)),
        "users_page", ADMIN_PAGE_SIZE,
    )
    users = users_page.items
    counts = {}
    if users:
        counts = dict(
            db.session.query(LibraryEntry.user_id, func.count())
            .filter(LibraryEntry.user_id.in_([user.id for user in users]))
            .group_by(LibraryEntry.user_id).all()
        )
    book_counts = {}
    if books_page.items:
        book_counts = dict(
            db.session.query(LibraryEntry.book_id, func.count())
            .filter(LibraryEntry.book_id.in_([book.id for book in books_page.items]))
            .group_by(LibraryEntry.book_id).all()
        )
    return render_template(
        "admin.html", pending=pending_page.items, pending_page=pending_page,
        all_books=books_page.items, books_page=books_page,
        users=users, users_page=users_page, counts=counts,
        book_counts=book_counts, stats=admin_stats(),
    )


@app.route("/admin/approve/<int:book_id>", methods=["POST"])
@login_required
def admin_approve(book_id: int):
    require_admin()
    book = db.session.get(Book, book_id)
    if not book:
        abort(404)
    book.approved = True
    db.session.commit()
    flash(f"Approved “{book.title}” — it is now in the catalogue.", "success")
    return redirect(request.referrer or url_for("admin"))


@app.route("/admin/reject/<int:book_id>", methods=["POST"])
@login_required
def admin_reject(book_id: int):
    require_admin()
    book = db.session.get(Book, book_id)
    if not book:
        abort(404)
    delete_cover_file(book.cover_filename)
    db.session.delete(book)  # cascades to its library entries
    db.session.commit()
    flash(f"Rejected “{book.title}” — submission removed.", "success")
    return redirect(request.referrer or url_for("admin"))


@app.route("/admin/delete-book/<int:book_id>", methods=["POST"])
@login_required
def admin_delete_book(book_id: int):
    require_admin()
    book = db.session.get(Book, book_id)
    if not book:
        abort(404)
    delete_cover_file(book.cover_filename)
    db.session.delete(book)
    db.session.commit()
    flash(f"Deleted “{book.title}” from the catalogue.", "success")
    return redirect(request.referrer or url_for("admin"))


@app.route("/admin/delete-user/<int:user_id>", methods=["POST"])
@login_required
def admin_delete_user(user_id: int):
    require_admin()
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "error")
    elif user.is_admin:
        flash("Admin accounts cannot be deleted from the dashboard.", "error")
    else:
        # Entries with pending books submitted by this user: drop those books too.
        for entry in user.library.all():
            if not entry.book.approved and entry.book.submitted_by_id == user.id:
                delete_cover_file(entry.book.cover_filename)
                db.session.delete(entry.book)
        name = user.username
        db.session.delete(user)
        db.session.commit()
        flash(f"Deleted user “{name}” and their shelf links.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/set-password", methods=["POST"])
@login_required
def admin_set_password():
    require_admin()
    user_id = request.form.get("user_id", "")
    user = db.session.get(User, int(user_id)) if user_id.isdigit() else None
    if not user:
        flash("Pick a user first.", "error")
        return redirect(url_for("admin"))
    new_pw = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    if len(new_pw) < MIN_PW:
        flash(f"Password must be at least {MIN_PW} characters.", "error")
    elif new_pw != confirm:
        flash("Passwords do not match.", "error")
    else:
        user.set_password(new_pw)
        db.session.commit()
        flash(f"Password reset for “{user.username}”. "
              "Share the new password with them securely.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/toggle-admin/<int:user_id>", methods=["POST"])
@login_required
def admin_toggle_admin(user_id: int):
    require_admin()
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.id == current_user.id:
        flash("You cannot revoke your own admin status.", "error")
    else:
        user.is_admin = not user.is_admin
        db.session.commit()
        flash(f"{'Granted' if user.is_admin else 'Revoked'} admin for “{user.username}”.", "success")
    return redirect(url_for("admin"))


# --------------------------------------------------------------------------
# Uploaded covers
# --------------------------------------------------------------------------
@app.route("/uploads/<path:filename>")
@login_required
def uploads(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/static/uploads/<path:filename>")
@login_required
def protected_static_upload(filename):
    """Keep legacy uploaded-cover URLs behind the same login requirement."""
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# --------------------------------------------------------------------------
# Init
# --------------------------------------------------------------------------
def init_db():
    with app.app_context():
        engine = db.engine
        with engine.connect() as lock_connection:
            lock_connection.execute(
                db.text("SELECT pg_advisory_lock(:lock_key)"),
                {"lock_key": DB_INIT_LOCK_KEY},
            )
            lock_connection.commit()
            try:
                ensure_schemas(
                    engine, app.config["BOOK_SCHEMA"], app.config["USER_SCHEMA"],
                )
                db.create_all()
                _migrate(engine)
            finally:
                lock_connection.execute(
                    db.text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": DB_INIT_LOCK_KEY},
                )
                lock_connection.commit()


def _migrate(engine):
    """Lightweight, idempotent migrations for columns added post-launch."""
    stmts = [
        # Hidden-from-Readers preference (added in v1.1)
        (
            "users", "users", "is_searchable",
            "ALTER TABLE users.users ADD COLUMN is_searchable BOOLEAN NOT NULL DEFAULT TRUE",
        ),
    ]
    search_indexes = (
        "CREATE INDEX IF NOT EXISTS ix_books_title_trgm "
        "ON books.books USING gin (title gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS ix_books_author_trgm "
        "ON books.books USING gin (author gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS ix_books_publisher_trgm "
        "ON books.books USING gin (publisher gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS ix_books_description_trgm "
        "ON books.books USING gin (description gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS ix_books_isbn_trgm "
        "ON books.books USING gin (isbn gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS ix_genres_name_trgm "
        "ON books.genres USING gin (name gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS ix_users_username_lower_trgm "
        "ON users.users USING gin (username_lower gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS ix_books_author_lower "
        "ON books.books (lower(author))",
        "CREATE INDEX IF NOT EXISTS ix_books_title_author_lower "
        "ON books.books (lower(title), lower(author))",
        "CREATE INDEX IF NOT EXISTS ix_genres_name_lower "
        "ON books.genres (lower(name))",
    )
    with engine.begin() as conn:
        conn.execute(db.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        for ddl in search_indexes:
            conn.execute(db.text(ddl))
        for schema, table, column, ddl in stmts:
            exists = conn.execute(
                db.text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = :table AND column_name = :column"
                ),
                {"schema": schema, "table": table, "column": column},
            ).first()
            if not exists:
                conn.execute(db.text(ddl))


if __name__ == "__main__":
    # Dev-only fallback (gunicorn via run.sh is the production entry point).
    # The app is HTTPS-only in production; there is no plain-HTTP mode.
    if os.environ.get("DEV_HTTP") == "1":
        raise SystemExit("DEV_HTTP is disabled: MyLibrary runs HTTPS-only.")
    init_db()
    app.run(host="0.0.0.0", port=5000,
            debug=bool(os.environ.get("FLASK_DEBUG")))