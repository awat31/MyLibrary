"""
MyLibrary — a bookshelf-style personal library web app.

Run with:  ./run.sh   (or)   venv/bin/python app.py
"""
import os
import unicodedata
import uuid
from datetime import datetime

from flask import (
    Flask, render_template, redirect, url_for, request, flash, abort,
    send_from_directory, current_app,
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user,
)
from sqlalchemy import or_, and_, func, cast, String, desc, asc
from werkzeug.utils import secure_filename

from config import Config
from models import db, ensure_schemas, User, Book, Genre, LibraryEntry, book_genres


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


@app.context_processor
def inject_globals():
    return {
        "current_year": datetime.now().year,
        "app_name": "MyLibrary",
    }


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
    query = Book.query
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
        like = f"%{q}%"
        genre_alias = db.aliased(Genre)
        query = (
            query.join(
                book_genres, book_genres.c.book_id == Book.id, isouter=True
            )
            .join(genre_alias, genre_alias.id == book_genres.c.genre_id, isouter=True)  # noqa: E501
            .filter(
                or_(
                    Book.title.ilike(like),
                    Book.author.ilike(like),
                    func.coalesce(Book.publisher, "").ilike(like),
                    Book.description.ilike(like),
                    Book.isbn.ilike(f"%{q}%"),
                    cast(Book.published_year, String).ilike(f"%{q}%"),
                    func.coalesce(genre_alias.name, "").ilike(like),
                )
            )
            .distinct()
        )
    return query.order_by(asc(Book.title), asc(Book.author)).all()


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


def form_to_book_data(form):
    return {
        "title": clean_search(form.get("title")),
        "author": clean_search(form.get("author")),
        "publisher": clean_search(form.get("publisher")) or None,
        "published_year": int(form["published_year"]) if form.get("published_year", "").isdigit() else None,
        "isbn": normalize_isbn(form.get("isbn")) or None,
        "description": clean_search(form.get("description")) or None,
        "genres": parse_genres(form.get("genres")),
        "cover_url": clean_search(form.get("cover_url")) or None,
    }


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


def owned_book_ids(user: User) -> set[int]:
    if not user or not user.is_authenticated:
        return set()
    return {e.book_id for e in user.library.all()}


def entry_for(user: User, book_id: int) -> LibraryEntry | None:
    return LibraryEntry.query.filter_by(user_id=user.id, book_id=book_id).first()


def require_admin():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)


def admin_stats():
    pending = Book.query.filter_by(approved=False).count()
    return {
        "books": Book.query.filter_by(approved=True).count(),
        "pending": pending,
        "users": User.query.count(),
        "shelved": LibraryEntry.query.count(),
    }


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
    owned = owned_book_ids(current_user) if current_user.is_authenticated else set()
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
        elif not password or len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
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
        user = User.query.filter_by(username_lower=username.lower()).first()
        if user and user.check_password(request.form.get("password", "")):
            login_user(user, remember=bool(request.form.get("remember")))
            flash(f"Welcome back, {user.username}!", "success")
            nxt = request.args.get("next")
            return redirect(nxt if nxt and nxt.startswith("/") else url_for("my_library"))
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
    books = search_books(q=q, genre=genre, author=author,
                         year_min=year_min, year_max=year_max)
    all_genres = Genre.query.order_by(Genre.name).all()
    authors = [
        a[0] for a in db.session.query(Book.author).distinct().order_by(Book.author)
    ]
    return render_template(
        "search.html", books=books, genres=all_genres, authors=authors,
        owned_ids=owned_book_ids(current_user),
        filters={"q": q, "genre": genre, "author": author,
                 "year_min": year_min, "year_max": year_max},
    )


# --------------------------------------------------------------------------
# Profile & privacy
# --------------------------------------------------------------------------
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "visibility":
            current_user.is_searchable = bool(request.form.get("is_searchable"))
            db.session.commit()
            if current_user.is_searchable:
                flash("You are now visible in the Readers list.", "success")
            else:
                flash("You are now hidden from the Readers list — "
                      "readers can still find you by direct link.", "success")
        return redirect(url_for("profile"))
    shelf_count = current_user.library.count()
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
    books = search_books(
        q=q, genre=genre, author=author, year_min=year_min, year_max=year_max,
        approved_only=False if owner.id == current_user.id else True,
        library_of=owner,
    )
    shelf_books = [
        (entry, book) for book in books
        for entry in [LibraryEntry.query.filter_by(
            user_id=owner.id, book_id=book.id).first()]
        if entry
    ]
    shelf_books.sort(key=lambda pair: pair[1].title.lower())
    all_genres = Genre.query.order_by(Genre.name).all()
    authors = sorted(
        {b.author for b in books},
        key=str.casefold,
    )
    return render_template(
        "library.html",
        owner=owner, entries=shelf_books, genres=all_genres, authors=authors,
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
    query = User.query.filter_by(is_searchable=True)
    if q:
        query = query.filter(User.username_lower.ilike(f"%{q.lower()}%"))
    users = query.order_by(User.username_lower).all()
    if current_user.is_admin:
        # Admins also see who is hidden from the list.
        hidden = User.query.filter_by(is_searchable=False)
        if q:
            hidden = hidden.filter(User.username_lower.ilike(f"%{q.lower()}%"))
        users = sorted(
            list(users) + list(hidden.all()),
            key=lambda u: u.username_lower,
        )
    counts = dict(
        db.session.query(LibraryEntry.user_id, func.count())
        .group_by(LibraryEntry.user_id).all()
    )
    return render_template("readers.html", users=users, counts=counts, q=q)


@app.route("/readers/<int:user_id>")
@login_required
def reader_library(user_id: int):
    owner = db.session.get(User, user_id)
    if not owner:
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
        owned_ids=owned_book_ids(current_user),
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
    pending = Book.query.filter_by(approved=False).order_by(asc(Book.created_at)).all()
    all_books = Book.query.order_by(desc(Book.created_at)).all()
    users = User.query.order_by(asc(User.username_lower)).all()
    counts = dict(
        db.session.query(LibraryEntry.user_id, func.count())
        .group_by(LibraryEntry.user_id).all()
    )
    return render_template(
        "admin.html", pending=pending, all_books=all_books, users=users,
        counts=counts, stats=admin_stats(),
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


# --------------------------------------------------------------------------
# Init
# --------------------------------------------------------------------------
def init_db():
    with app.app_context():
        engine = db.engine
        ensure_schemas(engine, app.config["BOOK_SCHEMA"], app.config["USER_SCHEMA"])
        _migrate(engine)
        db.create_all()


def _migrate(engine):
    """Lightweight, idempotent migrations for columns added post-launch."""
    stmts = [
        # Hidden-from-Readers preference (added in v1.1)
        (
            "users", "users", "is_searchable",
            "ALTER TABLE users.users ADD COLUMN is_searchable BOOLEAN NOT NULL DEFAULT TRUE",
        ),
    ]
    with engine.connect() as conn:
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
                conn.commit()


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
