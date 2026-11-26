"""Runs each time an online server starts: loads the question, brand and model lists, and creates the
first Novus admin from the host's settings (ADMIN_EMAIL, ADMIN_NAME, ADMIN_PASSWORD) if that account
doesn't exist yet. That admin must choose a new password at first sign-in, so the one typed into the
host's settings stops working straight away. Existing accounts are never changed."""
import os

from sqlalchemy import select

from app.create_admin import NOVUS
from app.db import SessionLocal
from app.ingest.store import sync_reference
from app.models import ActivityLog, Company, User
from app.security import MIN_PASSWORD_LENGTH, hash_password


def main() -> None:
    with SessionLocal() as db:  # always hands the connection back, even after an early return
        setup(db)


def setup(db) -> None:
    sync_reference(db)
    db.commit()
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not email or db.scalar(select(User).where(User.email == email)):
        return
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"ADMIN_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters. No admin created.")
        return
    novus = db.scalar(select(Company).where(Company.is_novus.is_(True)))
    if novus is None:
        novus = Company(name=NOVUS, is_novus=True)
        db.add(novus)
        db.flush()
    user = User(email=email, name=os.environ.get("ADMIN_NAME", "").strip() or email, role="admin",
                password_hash=hash_password(password), company_id=novus.id, must_change_password=True)
    db.add(user)
    db.flush()
    db.add(ActivityLog(user_id=user.id, company_id=novus.id, action="admin_created", detail={"via": "host settings"}))
    db.commit()
    print(f"Created the first admin: {email}")


if __name__ == "__main__":
    main()
