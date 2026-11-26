"""Connection to the PostgreSQL database. The address comes from backend/.env, never from the code."""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    """Give each request its own database session, closed when the request ends."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def database_ok() -> bool:
    """True when the database answers a trivial question."""
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return True
    except Exception:
        return False
