"""Seed the databases: creates the Admin user and a small starter catalogue.

Run:  venv/bin/python seed.py
Idempotent — skips anything that already exists.

Admin credentials (created if no 'Admin' user exists):
    username: Admin
    password: Admin123!
Change the password after first login for anything beyond local use.
"""
import os

import app as flask_app_module  # noqa: F401  (registers routes before init_db)
from app import app, init_db, parse_genres
from models import db, User, Book, LibraryEntry, Genre  # noqa

ADMIN_USERNAME = "Admin"
ADMIN_PASSWORD = "Admin123!"

SAMPLE_BOOKS = [
    {
        "title": "The Hobbit", "author": "J.R.R. Tolkien",
        "publisher": "Allen & Unwin", "published_year": 1937,
        "isbn": "9780547928227",
        "description": ("Bilbo Baggins, a comfort-loving hobbit, is whisked away on an "
                        "unexpected adventure of dwarves, dragons and stolen treasure."),
        "genres": "Fantasy, Adventure",
    },
    {
        "title": "Pride and Prejudice", "author": "Jane Austen",
        "publisher": "T. Egerton", "published_year": 1813,
        "isbn": "9780141439518",
        "description": ("Elizabeth Bennet navigates manners, upbringing, morality and "
                        "marriage in the society ofRegency England, all while proud "
                        "Mr Darcy waits to be understood."),
        "genres": "Classics, Romance",
    },
    {
        "title": "The Name of the Wind", "author": "Patrick Rothfuss",
        "publisher": "DAW Books", "published_year": 2007,
        "isbn": "9780756404741",
        "description": ("The tale of Kvothe, an infamous musician, magician and assassin, "
                        "recounted over three days in an inn at the edge of the world."),
        "genres": "Fantasy",
    },
    {
        "title": "Dune", "author": "Frank Herbert",
        "publisher": "Chilton Books", "published_year": 1965,
        "isbn": "9780441013598",
        "description": ("Paul Atreides travels to Arrakis, the most dangerous planet in "
                        "the universe, where spice, sandworms and destiny await."),
        "genres": "Science Fiction, Adventure",
    },
    {
        "title": "A Short History of Nearly Everything", "author": "Bill Bryson",
        "publisher": "Broadway Books", "published_year": 2003,
        "isbn": "9780767908184",
        "description": ("Bryson explores science from the Big Bang to civilisation with "
                        "wit, wonder and an admirable refusal to be intimidated."),
        "genres": "Science, Non-fiction",
    },
    {
        "title": "The Count of Monte Cristo", "author": "Alexandre Dumas",
        "publisher": "Paris", "published_year": 1844,
        "isbn": "9780140449266",
        "description": ("Wrongly imprisoned Edmond Dantès escapes and devotes his fortune "
                        "to a meticulous, magnificent revenge."),
        "genres": "Classics, Adventure",
    },
    {
        "title": "1984", "author": "George Orwell",
        "publisher": "Secker & Warburg", "published_year": 1949,
        "isbn": "9780451524935",
        "description": ("Winston Smith rebels against the Party and Big Brother in a "
                        "totalitarian state where history is rewritten nightly."),
        "genres": "Classics, Dystopian",
    },
    {
        "title": "The Midnight Library", "author": "Matt Haig",
        "publisher": "Canongate", "published_year": 2020,
        "isbn": "9780525559474",
        "description": ("Between life and death sits a library where every book is a "
                        "door to another life you could have lived."),
        "genres": "Fantasy, Fiction",
    },
]


def main():
    init_db()
    with app.app_context():
        # Admin user
        admin = User.query.filter_by(username_lower=ADMIN_USERNAME.lower()).first()
        if not admin:
            admin = User(username=ADMIN_USERNAME, username_lower=ADMIN_USERNAME.lower(),
                         is_admin=True)
            admin.set_password(ADMIN_PASSWORD)
            db.session.add(admin)
            db.session.commit()
            print(f"Created admin user '{ADMIN_USERNAME}' (password: {ADMIN_PASSWORD})")
        else:
            admin.is_admin = True
            db.session.commit()
            print(f"Admin '{ADMIN_USERNAME}' already exists — privileges confirmed")

        # Sample catalogue (approved)
        for spec in SAMPLE_BOOKS:
            isbn = spec.get("isbn")
            book = None
            if isbn:
                book = Book.query.filter_by(isbn=isbn).first()
            if not book:
                book = Book.query.filter(
                    db.func.lower(Book.title) == spec["title"].lower(),
                    db.func.lower(Book.author) == spec["author"].lower(),
                ).first()
            if not book:
                book = Book(
                    title=spec["title"], author=spec["author"],
                    publisher=spec["publisher"], published_year=spec["published_year"],
                    isbn=isbn, description=spec["description"], approved=True,
                    submitted_by_id=admin.id,
                )
                book.genres = parse_genres(spec["genres"])
                # Put a couple of books on the admin's shelf to start.
                db.session.add(book)
                db.session.flush()
                db.session.add(LibraryEntry(user_id=admin.id, book_id=book.id))
                print(f"Added book: {spec['title']}")
            # else: skip

        # Some extra genres so filters have options
        for g in ("Mystery", "Poetry", "Biography", "History", "Horror"):
            if not Genre.query.filter_by(name=g).first():
                db.session.add(Genre(name=g))

        db.session.commit()
        print("Seed complete.")


if __name__ == "__main__":
    main()
