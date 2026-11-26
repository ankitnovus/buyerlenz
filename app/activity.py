"""Write a line to the activity log."""
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from app.models import ActivityLog, User


def log(db: Session, request: Optional[Request], action: str, user: Optional[User] = None, **detail) -> None:
    """Add one entry. The caller commits, so the log line and the change it describes are saved together."""
    record(db, action, user, request.client.host if request and request.client else None, **detail)


def record(db: Session, action: str, user: Optional[User], ip: Optional[str], **detail) -> None:
    """The same, for work done after the web request has finished (e.g. reading a large upload)."""
    db.add(ActivityLog(
        user_id=user.id if user else None,
        company_id=user.company_id if user else None,
        action=action,
        detail=detail,
        ip=ip,
    ))
