"""Admin: client companies (brand, seats, removed quarters, renewal), their users, seat requests,
and "View as client" for Novus staff. Every change is written to the activity log."""
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.activity import log
from app.auth import current_session, require_admin
from app.db import get_db
from app.engine import Engine
from app.models import Brand, Company, LoginSession, SeatRequest, User
from app.security import hash_password, temporary_password

router = APIRouter(prefix="/api/admin", tags=["accounts"])


def fail(status: int, message: str):
    raise HTTPException(status, detail={"code": "refused", "message": message})


def now() -> datetime:
    return datetime.now(timezone.utc)


def quarter_label(q: str) -> str:
    return f"Q{q[5]} {q[:4]}"


def seats_used(db: Session, company_id: int) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.company_id == company_id, User.active.is_(True)))


def end_sessions(db: Session, user_id: int, reason: str) -> None:
    for s in db.scalars(select(LoginSession).where(LoginSession.user_id == user_id, LoginSession.ended_at.is_(None))):
        s.ended_at, s.end_reason = now(), reason


def client_company(db: Session, company_id: int) -> Company:
    c = db.get(Company, company_id)
    if c is None or c.is_novus:
        fail(404, "That client doesn’t exist.")
    return c


def describe_company(db: Session, c: Company) -> dict:
    brand = db.get(Brand, c.brand_id) if c.brand_id else None
    return {
        "id": c.id, "name": c.name, "brand_id": c.brand_id, "brand": brand.name if brand else None,
        "seat_limit": c.seat_limit, "seats_used": seats_used(db, c.id), "removed_quarters": sorted(c.removed_quarters or []),
        "renewal_date": c.renewal_date.date().isoformat() if c.renewal_date else None, "active": c.active,
        "open_seat_requests": db.scalar(select(func.count()).select_from(SeatRequest).where(
            SeatRequest.company_id == c.id, SeatRequest.handled.is_(False))),
    }


# ---------- reference lists for the forms ----------

@router.get("/brands")
def brands(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return [{"id": b.id, "name": b.name} for b in db.scalars(select(Brand).order_by(Brand.name))]


@router.get("/quarters")
def quarters(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Published quarters, the ones an admin can take away from a client."""
    return [{"id": r[0], "label": quarter_label(r[0])} for r in Engine(db).published_quarters()]


# ---------- client companies ----------

class CompanyBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    brand_id: Optional[int] = None
    seat_limit: Optional[int] = Field(default=None, ge=1, le=500)
    removed_quarters: Optional[list[str]] = None
    renewal_date: Optional[date] = None
    active: Optional[bool] = None


def apply(db: Session, c: Company, body: CompanyBody) -> None:
    if body.name is not None:
        clash = db.scalar(select(Company).where(Company.name == body.name.strip(), Company.id != c.id))
        if clash:
            fail(409, "Another client already has that name.")
        c.name = body.name.strip()
    if body.brand_id is not None:
        if db.get(Brand, body.brand_id) is None:
            fail(400, "Unknown brand.")
        c.brand_id = body.brand_id
    if body.seat_limit is not None:
        if body.seat_limit < seats_used(db, c.id):
            fail(409, "That’s fewer seats than active users. Deactivate a user first.")
        c.seat_limit = body.seat_limit
    if body.removed_quarters is not None:
        if any(len(q) != 6 or q[4] != "Q" or q[5] not in "1234" or not q[:4].isdigit() for q in body.removed_quarters):
            fail(400, "Unknown quarter.")
        c.removed_quarters = sorted(set(body.removed_quarters))
    if body.renewal_date is not None:
        c.renewal_date = datetime.combine(body.renewal_date, datetime.min.time())
    if body.active is not None:
        c.active = body.active


@router.get("/clients")
def list_clients(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return [describe_company(db, c) for c in db.scalars(select(Company).where(Company.is_novus.is_(False)).order_by(Company.name))]


@router.post("/clients")
def create_client(body: CompanyBody, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if not body.name or body.brand_id is None:
        fail(400, "Give the client’s name and brand.")
    if db.scalar(select(Company).where(Company.name == body.name.strip())):
        fail(409, "A client with that name already exists.")
    c = Company(name=body.name.strip(), seat_limit=1, removed_quarters=[], active=True)
    db.add(c)
    db.flush()
    apply(db, c, body)
    log(db, request, "client_created", admin, client=c.name)
    db.commit()
    return describe_company(db, c)


@router.get("/clients/{company_id}")
def client_detail(company_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    c = client_company(db, company_id)
    users = db.scalars(select(User).where(User.company_id == c.id).order_by(User.name)).all()
    last_in = dict(db.execute(select(LoginSession.user_id, func.max(LoginSession.created_at))
                              .where(LoginSession.user_id.in_([u.id for u in users])).group_by(LoginSession.user_id)).all())
    live = {s.user_id: s for s in db.scalars(select(LoginSession).where(
        LoginSession.user_id.in_([u.id for u in users]), LoginSession.ended_at.is_(None)))}
    requests = db.scalars(select(SeatRequest).where(SeatRequest.company_id == c.id).order_by(SeatRequest.id.desc())).all()
    names = {u.id: u.name for u in users}
    return {
        **describe_company(db, c),
        "users": [{"id": u.id, "name": u.name, "email": u.email, "active": u.active,
                   "must_change_password": u.must_change_password,
                   "last_sign_in": last_in[u.id].isoformat() if u.id in last_in else None,
                   "signed_in_on": live[u.id].device if u.id in live else None} for u in users],
        "seat_requests": [{"id": r.id, "user": names.get(r.user_id), "created_at": r.created_at.isoformat(),
                           "handled": r.handled} for r in requests],
    }


@router.patch("/clients/{company_id}")
def update_client(company_id: int, body: CompanyBody, request: Request, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    c = client_company(db, company_id)
    apply(db, c, body)
    if body.active is False:  # switching a client off signs all its users out
        for u in db.scalars(select(User).where(User.company_id == c.id)):
            end_sessions(db, u.id, "account_closed")
    log(db, request, "client_updated", admin, client=c.name, changes=body.model_dump(exclude_none=True, mode="json"))
    db.commit()
    return client_detail(company_id, db)


# ---------- users ----------

class NewUser(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    email: str = Field(min_length=5, max_length=320)


@router.post("/clients/{company_id}/users")
def add_user(company_id: int, body: NewUser, request: Request, db: Session = Depends(get_db),
             admin: User = Depends(require_admin)):
    c = client_company(db, company_id)
    email = body.email.strip().lower()
    if "@" not in email:
        fail(400, "That doesn’t look like an email address.")
    if db.scalar(select(User).where(User.email == email)):
        fail(409, "An account with that email already exists.")
    if seats_used(db, c.id) >= c.seat_limit:
        fail(409, f"All {c.seat_limit} seats are in use. Raise the seat limit or deactivate a user first.")
    password = temporary_password()
    u = User(email=email, name=body.name.strip(), password_hash=hash_password(password), role="client",
             company_id=c.id, must_change_password=True)
    db.add(u)
    db.flush()
    log(db, request, "user_created", admin, client=c.name, account=email)
    db.commit()
    return {"id": u.id, "email": email, "temporary_password": password}


def client_user(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None or u.role != "client":
        fail(404, "That user doesn’t exist.")
    return u


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    u = client_user(db, user_id)
    password = temporary_password()
    u.password_hash, u.must_change_password, u.failed_logins, u.locked_until = hash_password(password), True, 0, None
    end_sessions(db, u.id, "password_reset")
    log(db, request, "password_reset", admin, account=u.email)
    db.commit()
    return {"email": u.email, "temporary_password": password}


@router.post("/users/{user_id}/deactivate")
def deactivate(user_id: int, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    u = client_user(db, user_id)
    u.active = False
    end_sessions(db, u.id, "deactivated")
    log(db, request, "user_deactivated", admin, account=u.email)
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/reactivate")
def reactivate(user_id: int, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    u = client_user(db, user_id)
    if not u.active and seats_used(db, u.company_id) >= u.company.seat_limit:
        fail(409, f"All {u.company.seat_limit} seats are in use. Raise the seat limit first.")
    u.active = True
    log(db, request, "user_reactivated", admin, account=u.email)
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/sign-out")
def sign_out_everywhere(user_id: int, request: Request, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    u = client_user(db, user_id)
    end_sessions(db, u.id, "signed_out_by_admin")
    log(db, request, "user_signed_out_by_admin", admin, account=u.email)
    db.commit()
    return {"ok": True}


@router.post("/seat-requests/{request_id}/handled")
def seat_request_handled(request_id: int, request: Request, db: Session = Depends(get_db),
                         admin: User = Depends(require_admin)):
    r = db.get(SeatRequest, request_id)
    if r is None:
        fail(404, "That request doesn’t exist.")
    r.handled = True
    log(db, request, "seat_request_handled", admin, request_id=request_id)
    db.commit()
    return {"ok": True}


# ---------- View as client ----------

class ViewAs(BaseModel):
    brand_id: Optional[int] = None  # None: back to the admin view


@router.post("/view-as")
def view_as(body: ViewAs, request: Request, s: LoginSession = Depends(current_session), db: Session = Depends(get_db),
            admin: User = Depends(require_admin)):
    if body.brand_id is not None and db.get(Brand, body.brand_id) is None:
        fail(400, "Unknown brand.")
    s.view_as_brand_id = body.brand_id
    log(db, request, "view_as_client" if body.brand_id else "view_as_ended", admin,
        brand=db.get(Brand, body.brand_id).name if body.brand_id else None)
    db.commit()
    return {"view_as": db.get(Brand, body.brand_id).name if body.brand_id else None}
