"""Tests run against a separate database (buyerlenz_test) that is wiped before every test."""
import os

os.environ["DATABASE_URL"] = "postgresql+psycopg://localhost:5432/buyerlenz_test"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.engine import _cubes  # noqa: E402
from app.guards import ACTIVITY_LOG_APPEND_ONLY  # noqa: E402
from app.main import app  # noqa: E402
from app.ingest.store import sync_reference  # noqa: E402
from app.models import Base, Brand, Company, User  # noqa: E402
from app.security import hash_password  # noqa: E402

PASSWORD = "correct-horse-battery"
CHROME_MAC = {"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/141.0 Safari/537.36"}
CHROME_WIN = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141.0 Safari/537.36"}


@pytest.fixture(autouse=True)
def fresh_database():
    _cubes.clear()  # summaries from the previous test's data
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(ACTIVITY_LOG_APPEND_ONLY)
    db = SessionLocal()
    sync_reference(db)
    hyundai = db.query(Brand).filter_by(name="Hyundai").one()
    novus = Company(name="Novus Insights", is_novus=True)
    client_co = Company(name="Hyundai Motor India", brand_id=hyundai.id, seat_limit=3)
    db.add_all([novus, client_co])
    db.flush()
    db.add_all([
        User(email="admin@novus.example", name="Asha Admin", password_hash=hash_password(PASSWORD), role="admin", company_id=novus.id),
        User(email="arjun@hyundai.example", name="Arjun Mehta", password_hash=hash_password(PASSWORD), role="client", company_id=client_co.id),
    ])
    db.commit()
    db.close()
    yield


def new_browser(headers=CHROME_MAC) -> TestClient:
    """A separate browser with its own cookies, sending the header the BuyerLenz pages send."""
    return TestClient(app, headers={**headers, "x-buyerlenz": "1"})
