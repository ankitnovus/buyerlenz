"""Sign in, sign out and 'who am I'. One active session per login, enforced here on the server."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.activity import log
from app.db import get_db
from app.models import ActivityLog, LoginSession, SeatRequest, User
from app.security import MIN_PASSWORD_LENGTH, device_label, hash_password, hash_token, new_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


def now() -> datetime:
    return datetime.now(timezone.utc)


def fail(status: int, code: str, message: str, **extra):
    raise HTTPException(status, detail={"code": code, "message": message, **extra})


def is_live(s: LoginSession) -> bool:
    t = now()
    return (s.ended_at is None
            and s.last_seen > t - timedelta(hours=config.SESSION_IDLE_HOURS)
            and s.created_at > t - timedelta(hours=config.SESSION_MAX_HOURS))


def user_info(user: User, s: Optional[LoginSession] = None) -> dict:
    out = {"name": user.name, "email": user.email, "role": user.role, "company": user.company.name,
           "must_change_password": user.must_change_password}
    if s is not None and s.view_as_brand_id:
        out["view_as"] = s.view_as_brand.name
    return out


# ---------- who is signed in ----------

def current_session(
    request: Request,
    db: Session = Depends(get_db),
    token: Optional[str] = Cookie(default=None, alias=config.SESSION_COOKIE),
) -> LoginSession:
    """The signed-in session for this browser, or a 401 saying why there isn't one."""
    if not token:
        fail(401, "signed_out", "Please sign in.")
    s = db.scalar(select(LoginSession).where(LoginSession.token_hash == hash_token(token)))
    if s is None:
        fail(401, "signed_out", "Please sign in.")
    if s.end_reason == "replaced":
        fail(401, "replaced", "You were signed out because this login was used on another device.")
    if s.ended_at is not None:
        fail(401, "signed_out", "Please sign in.")
    if not is_live(s) or not s.user.active or not s.user.company.active:
        s.ended_at, s.end_reason = now(), "expired"
        db.commit()
        fail(401, "expired", "Your session ended. Please sign in again.")
    if s.last_seen < now() - timedelta(minutes=1):  # avoid a database write on every request
        s.last_seen = now()
        db.commit()
    return s


def require_user(s: LoginSession = Depends(current_session)) -> User:
    """A signed-in user who has chosen their own password (everything except /me, sign out and the
    password change needs this)."""
    if s.user.must_change_password:
        fail(403, "change_password", "Choose your own password before continuing.")
    return s.user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        fail(403, "admins_only", "This area is for Novus staff only.")
    return user


# ---------- checking a password ----------

MAX_PASSWORD_LENGTH = 200  # checking an enormous password on purpose would tie up the server


class Credentials(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class LoginBody(Credentials):
    force: bool = False  # "Sign in here instead": end the session on the other device


def check_credentials(db: Session, request: Request, body: Credentials) -> User:
    """The user if the email and password are right; otherwise counts the failure and stops."""
    email = body.email.strip().lower()
    address = request.client.host if request.client else None
    recent_failures = db.scalar(select(func.count()).select_from(ActivityLog).where(
        ActivityLog.action == "sign_in_failed", ActivityLog.ip == address,
        ActivityLog.at > now() - timedelta(minutes=config.LOCKOUT_MINUTES)))
    if address and recent_failures >= config.MAX_FAILED_PER_ADDRESS:
        # Someone is trying many passwords from one place, perhaps across many emails
        log(db, request, "sign_in_throttled", None, email=email)
        db.commit()
        fail(429, "throttled", f"Too many failed sign-ins from this network. Try again in {config.LOCKOUT_MINUTES} minutes.")
    user = db.scalar(select(User).where(User.email == email))
    if user and user.locked_until and user.locked_until > now():
        log(db, request, "sign_in_locked", user)
        db.commit()
        fail(429, "locked", f"Too many wrong passwords. Try again in {config.LOCKOUT_MINUTES} minutes.")
    if (not verify_password(user.password_hash if user else None, body.password)
            or not (user and user.active and user.company.active)):
        if user:
            user.failed_logins += 1
            if user.failed_logins >= config.MAX_FAILED_LOGINS:
                user.locked_until = now() + timedelta(minutes=config.LOCKOUT_MINUTES)
                user.failed_logins = 0
        log(db, request, "sign_in_failed", user, email=email)
        db.commit()
        # Same message whether the email or the password is wrong, so emails can't be guessed.
        fail(401, "bad_credentials", "That email and password don't match. Check them and try again.")
    user.failed_logins, user.locked_until = 0, None
    return user


# ---------- endpoints ----------

@router.post("/login")
def login(
    body: LoginBody,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    token: Optional[str] = Cookie(default=None, alias=config.SESSION_COOKIE),
):
    user = check_credentials(db, request, body)
    sessions = db.scalars(select(LoginSession).where(
        LoginSession.user_id == user.id, LoginSession.ended_at.is_(None))).all()
    this_browser = hash_token(token) if token else None
    others = []
    for s in sessions:
        if s.token_hash == this_browser:
            s.ended_at, s.end_reason = now(), "renewed"  # same browser signing in again
        elif not is_live(s):
            s.ended_at, s.end_reason = now(), "expired"
        else:
            others.append(s)

    if others and not body.force:
        db.commit()
        latest = max(others, key=lambda s: s.last_seen)
        fail(409, "active_elsewhere", "This login is open on another device.",
             device=latest.device, last_seen=latest.last_seen.isoformat())

    for s in others:
        s.ended_at, s.end_reason = now(), "replaced"
    raw = new_token()
    device = device_label(request.headers.get("user-agent"))
    db.add(LoginSession(token_hash=hash_token(raw), user_id=user.id, device=device))
    log(db, request, "sign_in", user, device=device, replaced_other_device=bool(others))
    db.commit()

    response.set_cookie(
        config.SESSION_COOKIE, raw, httponly=True, secure=config.COOKIE_SECURE, samesite="strict",
        max_age=config.SESSION_MAX_HOURS * 3600, path="/",
    )
    return user_info(user)


@router.post("/logout")
def logout(request: Request, response: Response, s: LoginSession = Depends(current_session),
           db: Session = Depends(get_db)):
    s.ended_at, s.end_reason = now(), "signed_out"
    log(db, request, "sign_out", s.user)
    db.commit()
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(s: LoginSession = Depends(current_session)):
    return user_info(s.user, s)


class NewPassword(BaseModel):
    current_password: str = Field(max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(max_length=MAX_PASSWORD_LENGTH)


@router.post("/change-password")
def change_password(body: NewPassword, request: Request, s: LoginSession = Depends(current_session),
                    db: Session = Depends(get_db)):
    user = s.user
    if not verify_password(user.password_hash, body.current_password):
        # Counts like a wrong sign-in, so an unattended open session can't be used to guess the password
        user.failed_logins += 1
        log(db, request, "password_change_failed", user)
        if user.failed_logins >= config.MAX_FAILED_LOGINS:
            user.failed_logins, user.locked_until = 0, now() + timedelta(minutes=config.LOCKOUT_MINUTES)
            s.ended_at, s.end_reason = now(), "locked"
            db.commit()
            fail(401, "locked", f"Too many wrong passwords. You’ve been signed out; try again in {config.LOCKOUT_MINUTES} minutes.")
        db.commit()
        fail(400, "bad_current", "Your current password isn’t right.")
    if len(body.new_password) < MIN_PASSWORD_LENGTH:
        fail(400, "too_short", f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if body.new_password == body.current_password:
        fail(400, "same", "Choose a password different from the current one.")
    user.password_hash, user.must_change_password, user.failed_logins = hash_password(body.new_password), False, 0
    log(db, request, "password_changed", user)
    db.commit()
    return user_info(user, s)


@router.post("/seat-request")
def seat_request(body: Credentials, request: Request, db: Session = Depends(get_db)):
    """'Ask for another seat' from the signed-in-elsewhere screen. Needs the password, as signing in does."""
    user = check_credentials(db, request, body)
    db.add(SeatRequest(user_id=user.id, company_id=user.company_id))
    log(db, request, "seat_request", user)
    db.commit()
    return {"company": user.company.name}
