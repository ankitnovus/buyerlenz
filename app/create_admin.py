"""Create a Novus admin account from the Terminal. The password is typed privately and never shown.

Run from the backend folder:  .venv/bin/python -m app.create_admin
"""
from getpass import getpass

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ActivityLog, Company, User
from app.security import MIN_PASSWORD_LENGTH, hash_password

NOVUS = "Novus Insights"


def main() -> None:
    db = SessionLocal()
    email = input("Admin email: ").strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        print("An account with that email already exists. Nothing changed.")
        return
    name = input("Full name (shown on stamps and the activity log): ").strip()
    while True:
        password = getpass(f"Password (at least {MIN_PASSWORD_LENGTH} characters, hidden as you type): ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"Too short. Use at least {MIN_PASSWORD_LENGTH} characters.")
        elif password != getpass("Type the password again: "):
            print("The two passwords don't match. Try again.")
        else:
            break
    novus = db.scalar(select(Company).where(Company.name == NOVUS))
    if novus is None:
        novus = Company(name=NOVUS, is_novus=True)
        db.add(novus)
        db.flush()
    user = User(email=email, name=name, password_hash=hash_password(password), role="admin", company_id=novus.id)
    db.add(user)
    db.flush()
    db.add(ActivityLog(user_id=user.id, company_id=novus.id, action="admin_created", detail={"via": "terminal"}))
    db.commit()
    print(f"Done. {name} can now sign in as an admin with {email}.")


if __name__ == "__main__":
    main()
