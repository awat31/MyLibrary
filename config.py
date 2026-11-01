"""Configuration for MyLibrary."""
import os
import secrets

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _load_secret_key() -> str:
    """Session-signing key: env var wins; otherwise a per-install random key
    stored with 0600 perms (never a hardcoded fallback — a known secret key
    would let attackers forge admin sessions)."""
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    keyfile = os.path.join(BASE_DIR, "instance_secret_key")
    try:
        with open(keyfile, "r") as fh:
            key = fh.read().strip()
            if len(key) >= 32:
                return key
            raise FileNotFoundError
    except FileNotFoundError:
        key = secrets.token_hex(32)
        with open(keyfile, "w") as fh:
            fh.write(key)
        os.chmod(keyfile, 0o600)
        return key


class Config:
    # RANDOMLY GENERATED per install (instance_secret_key); override with env var.
    SECRET_KEY = _load_secret_key()
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

    # --- Local cover cache ---------------------------------------------
    # Remote covers (Open Library) are fetched on first display, cached on
    # disk under var/cover_cache and served at LAN speed afterwards. The cache
    # is LRU-trimmed: never-used-up-again covers age out, so the folder holds
    # only what readers actually look at. Set COVER_CACHE_MAX_MB=0 to disable.
    COVER_CACHE_DIR = os.path.join(BASE_DIR, "var", "cover_cache")
    COVER_CACHE_MAX_MB = int(os.environ.get("COVER_CACHE_MAX_MB", "256"))
    COVER_FETCH_TIMEOUT = 8
    COVER_FETCH_MAX_BYTES = 8 * 1024 * 1024  # refuse oversized cover responses.
    MIN_PASSWORD_LENGTH = 12

    # --- Session/cookie hardening -------------------------------------
    # Served over HTTPS; mark the session cookie Secure + SameSite.
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # --- Login brute-force throttle -----------------------------------
    MAX_LOGIN_ATTEMPTS = 8
    LOGIN_WINDOW_SECONDS = 900  # 15 minutes

    # --- Security headers ---------------------------------------------
    SECURE_HEADERS = {
        "Content-Security-Policy": (
            "default-src 'self'; "
            "img-src 'self' data: https:; "
            "style-src 'self'; "
            "script-src 'self'; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        ),
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    }
