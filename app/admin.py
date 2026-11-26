"""Novus admin area. Every endpoint here needs an admin login."""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.activity_api import watch
from app.auth import require_admin
from app.db import get_db
from app.models import Company, SeatRequest, User

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    """Headline counts for the admin home page."""
    return {
        "client_companies": db.scalar(select(func.count()).select_from(Company).where(Company.is_novus.is_(False))),
        "users": db.scalar(select(func.count()).select_from(User)),
        "open_seat_requests": db.scalar(select(func.count()).select_from(SeatRequest).where(SeatRequest.handled.is_(False))),
        "worth_a_look": len(watch(db)),
    }
