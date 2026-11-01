"""Configuration for MyLibrary."""
import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://mylibrary:mylibrary_pw@localhost:5432/mylibrary",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Two logical databases (schemas) living in one PostgreSQL instance:
    #   "books" -> books, genres, book_genres
    #   "users" -> users, user_books
    BOOK_SCHEMA = "books"
    USER_SCHEMA = "users"
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 MB upload cap
    ALLOWED_COVER_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
