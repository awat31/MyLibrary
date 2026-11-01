"""Database models.

MyLibrary keeps two logical databases inside one PostgreSQL instance:

    Schema "books"  -> books, genres, genre links (the global catalogue)
    Schema "users"  -> users, library links  (who owns what, and when)

A user can link an unlimited number of books into their library, but each
book record exists exactly once: uniqueness is enforced on ISBN, falling
back to (title, author) for books submitted without an ISBN.
"""
from datetime import datetime
import hashlib

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from sqlalchemy import UniqueConstraint, func

db = SQLAlchemy()


def ensure_schemas(engine, book_schema: str, user_schema: str):
    """Create the two schemas if they don't exist yet (before create_all)."""
    with engine.connect() as conn:
        for schema in (book_schema, user_schema):
            if not engine.dialect.has_schema(conn, schema):
                conn.execute(db.text(f'CREATE SCHEMA "{schema}"'))
                conn.commit()


class User(UserMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = {"schema": "users"}

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    # Case-insensitive lookup helper, kept lowercase.
    username_lower = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    # Opt-out of appearing in the /readers list (shelf stays reachable by link).
    is_searchable = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    library = db.relationship(
        "LibraryEntry",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def set_password(self, raw: str):
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, raw)

    def __repr__(self):
        return f"<User {self.username}>"


class Book(db.Model):
    __tablename__ = "books"
    __table_args__ = (
        UniqueConstraint("isbn", name="uq_books_isbn"),
        db.Index("ix_books_title_author", "title", "author"),
        {"schema": "books"},
    )

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False, index=True)
    author = db.Column(db.String(255), nullable=False, index=True)
    publisher = db.Column(db.String(255))
    published_year = db.Column(db.Integer, index=True)
    isbn = db.Column(db.String(32), unique=True, index=True)  # nullable when unknown
    description = db.Column(db.Text)
    # Cover: either an uploaded file (stored in static/uploads) or a URL.
    cover_filename = db.Column(db.String(255))
    cover_url = db.Column(db.String(512))
    # Approval workflow: pending books live only in their submitter's library.
    approved = db.Column(db.Boolean, default=False, nullable=False, index=True)
    submitted_by_id = db.Column(
        db.Integer, db.ForeignKey("users.users.id", ondelete="SET NULL")
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    genres = db.relationship(
        "Genre", secondary="books.book_genres", back_populates="books", lazy="select"
    )
    owners = db.relationship(
        "LibraryEntry", back_populates="book", cascade="all, delete-orphan", lazy="dynamic"
    )
    submitted_by = db.relationship("User", foreign_keys=[submitted_by_id])

    @property
    def cover_src(self) -> str:
        """Local serving URL for the cover, or '' when no cover exists."""
        if self.cover_url:
            version = hashlib.sha256(self.cover_url.encode("utf-8")).hexdigest()[:16]
            return f"/cover/{self.id}?v={version}"
        if self.cover_filename:
            return f"/uploads/{self.cover_filename}"
        return ""

    @property
    def genre_names(self):
        return sorted(g.name for g in self.genres)

    def __repr__(self):
        return f"<Book {self.title} ({self.author})>"


class Genre(db.Model):
    __tablename__ = "genres"
    __table_args__ = {"schema": "books"}

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False, index=True)

    books = db.relationship(
        "Book", secondary="books.book_genres", back_populates="genres", lazy="select"
    )


book_genres = db.Table(
    "book_genres",
    db.Column(
        "book_id", db.Integer,
        db.ForeignKey("books.books.id", ondelete="CASCADE"), primary_key=True,
    ),
    db.Column(
        "genre_id", db.Integer,
        db.ForeignKey("books.genres.id", ondelete="CASCADE"), primary_key=True,
    ),
    schema="books",
)


class LibraryEntry(db.Model):
    """A copy of a book sitting on some user's shelf.

    One row per (user, book): a user can technically own several copies of the
    same title in real life, but the catalogue record is reused and the count
    of owned copies is tracked separately.
    """
    __tablename__ = "user_books"
    __table_args__ = (
        UniqueConstraint("user_id", "book_id", name="uq_user_books_user_book"),
        {"schema": "users"},
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    book_id = db.Column(
        db.Integer, db.ForeignKey("books.books.id", ondelete="CASCADE"), nullable=False, index=True
    )
    copies = db.Column(db.Integer, default=1, nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    user = db.relationship("User", back_populates="library")
    book = db.relationship("Book", back_populates="owners")

    def __repr__(self):
        return f"<LibraryEntry user={self.user_id} book={self.book_id}>"
