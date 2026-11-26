"""Admin: the activity log (who signed in, viewed, built and copied what) and the "worth a look" flags.
The log itself is only ever read here; entries are written by app.activity.log."""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.catalogue import BY_CODE
from app.db import get_db
from app.models import ActivityLog, Brand, CarModel, Company, User
from app.rules import (EXPORT_LIMIT, WATCH_DAYS, WATCH_EXPORTS_DAY, WATCH_EXPORTS_MONTH, WATCH_FAILED,
                       WATCH_FAILED_ADDRESS, WATCH_PLACES_DAY, WATCH_REPLACED)

router = APIRouter(prefix="/api/admin/activity", tags=["activity"], dependencies=[Depends(require_admin)])

INDIA = timezone(timedelta(hours=5, minutes=30))
PAGE = 100

KINDS = {
    "sign_ins": ("Sign-ins", {"sign_in", "sign_out", "sign_in_failed", "sign_in_locked", "sign_in_throttled",
                              "password_changed", "password_change_failed", "seat_request"}),
    "views": ("Views", {"view_home", "view_profile", "view_cut", "view_cut_trend", "view_tile_preview", "view_report"}),
    "exports": ("Copies to Excel", {"export"}),
    "saved": ("Saved cuts and layouts", {"save_cut", "delete_cut", "layout_changed", "layout_reset"}),
    "admin": ("Admin changes", {"client_created", "client_updated", "user_created", "password_reset", "user_deactivated",
                                "user_reactivated", "user_signed_out_by_admin", "seat_request_handled", "view_as_client",
                                "view_as_ended", "upload_started", "upload_checked", "upload_refused", "upload_published",
                                "upload_rolled_back", "upload_discarded", "model_name_matched"}),
}


def india_day(at: datetime) -> date:
    return at.astimezone(INDIA).date()


def quarter(q: Optional[str]) -> str:
    """Stored as "2025Q4" (older entries) or already as a label ("Q3 2025 – Q2 2026")."""
    if q and len(q) == 6 and q[4] == "Q":
        return f"Q{q[5]} {q[:4]}"
    return q or ""


class Names:
    """Turns stored keys ("m:12", "b:3", question codes) into the names people know."""

    def __init__(self, db: Session):
        self.models = dict(db.execute(select(CarModel.id, CarModel.name)).all())
        self.brands = dict(db.execute(select(Brand.id, Brand.name)).all())

    def entity(self, key: str) -> str:
        kind, _, ident = str(key).partition(":")
        if kind == "m" and ident.isdigit():
            return self.models.get(int(ident), "a model")
        if kind == "b" and ident.isdigit():
            return self.brands.get(int(ident), "a brand")
        return "All buyers" if key == "all" else str(key)

    def entities(self, keys) -> str:
        names = [self.entity(k) for k in keys or []]
        return ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")

    @staticmethod
    def questions(codes) -> str:
        return "; ".join(BY_CODE[c].title if c in BY_CODE else str(c) for c in codes or [])


def describe(action: str, d: dict, names: Names, known: bool = True) -> str:
    """One plain-English line for an activity-log entry."""
    q = quarter(d.get("quarter"))
    shape = f"{len(d.get('questions') or [])} question(s) × {len(d.get('columns') or [])} column(s)"
    staff = f" (viewing as {d['view_as']})" if d.get("view_as") else ""
    lines = {
        "sign_in": f"Signed in on {d.get('device', 'a device')}"
                   + (", signing out another device" if d.get("replaced_other_device") else ""),
        "sign_out": "Signed out",
        "sign_in_failed": "Wrong password" if known else "Failed sign-in: no account has this email",
        "sign_in_locked": "Tried to sign in while locked out",
        "sign_in_throttled": f"Sign-in refused: too many failures from this internet address ({d.get('email', '')})",
        "password_change_failed": "Wrong current password when changing password",
        "password_changed": "Changed their password",
        "seat_request": "Asked for another seat",
        "view_home": f"Viewed Home · {q}{staff}",
        "view_profile": f"Viewed Buyer profile: {names.entities(d.get('entities'))} · {q}{staff}",
        "view_cut": f"Built a data cut, {shape} · {q}{staff}: {names.questions(d.get('questions'))}",
        "view_cut_trend": f"Viewed a trend: {names.questions([d.get('question')])}{staff}",
        "export": (f"Downloaded the summary report: {names.entities(d.get('columns'))} · {q}{staff}"
                   if d.get("kind") == "summary_report"
                   else f"Copied to Excel, {shape} · {q}{staff}: {names.questions(d.get('questions'))}"),
        "view_report": f"Viewed the summary report: {names.entities(d.get('entities'))} · {q}{staff}",
        "view_tile_preview": f"Previewed a Home tile: {BY_CODE[d['tile']['question']].title if d.get('tile', {}).get('question') in BY_CODE else ''}{staff}",
        "layout_changed": f"Customised their {'Home' if d.get('page') == 'home' else 'Buyer Profile'}{staff}",
        "layout_reset": f"Reset their {'Home' if d.get('page') == 'home' else 'Buyer Profile'} to the standard layout{staff}",
        "save_cut": f"Saved the cut “{d.get('name', '')}”",
        "delete_cut": f"Deleted the saved cut “{d.get('name', '')}”",
        "client_created": f"Added the client {d.get('client', '')}",
        "client_updated": f"Changed the client {d.get('client', '')}: {', '.join(sorted(d.get('changes') or {})) or 'no fields'}",
        "user_created": f"Added the user {d.get('account', '')} at {d.get('client', '')}",
        "password_reset": f"Reset the password for {d.get('account', '')}",
        "user_deactivated": f"Deactivated {d.get('account', '')}",
        "user_reactivated": f"Reactivated {d.get('account', '')}",
        "user_signed_out_by_admin": f"Signed out {d.get('account', '')}",
        "seat_request_handled": "Marked a seat request as handled",
        "view_as_client": f"Opened the client view as {d.get('brand', '')}",
        "view_as_ended": "Went back to the admin view",
        "upload_started": f"Started uploading {d.get('filename', 'a file')}",
        "upload_checked": f"Read {d.get('filename', 'a file')} ({d.get('buyers', 0)} buyers) for checking",
        "upload_refused": f"Upload of {d.get('filename', 'a file')} refused: {d.get('reason', '')}",
        "upload_published": f"Published {d.get('filename', 'an upload')}",
        "upload_rolled_back": f"Rolled back {d.get('filename', 'an upload')}",
        "upload_discarded": f"Discarded {d.get('filename', 'an upload')}",
        "model_name_matched": f"Matched the model name “{d.get('name', '')}” to {d.get('model', '')}",
    }
    return lines.get(action, action.replace("_", " ").capitalize())


# ---------- the log ----------

@router.get("")
def activity(
    client: str = "",  # "" everyone, "novus" Novus staff, or a client company id
    user: Optional[int] = None,
    kind: str = "",
    day_from: Optional[date] = None,
    day_to: Optional[date] = None,
    before: Optional[int] = None,  # show entries older than this one ("Show older")
    db: Session = Depends(get_db),
):
    stmt = (select(ActivityLog, User, Company).outerjoin(User, ActivityLog.user_id == User.id)
            .outerjoin(Company, ActivityLog.company_id == Company.id).order_by(ActivityLog.id.desc()).limit(PAGE + 1))
    if client == "novus":
        stmt = stmt.where(Company.is_novus.is_(True))
    elif client.isdigit():
        stmt = stmt.where(ActivityLog.company_id == int(client))
    if user is not None:
        stmt = stmt.where(ActivityLog.user_id == user)
    if kind in KINDS:
        stmt = stmt.where(ActivityLog.action.in_(KINDS[kind][1]))
    if day_from:
        stmt = stmt.where(ActivityLog.at >= datetime.combine(day_from, datetime.min.time(), INDIA))
    if day_to:
        stmt = stmt.where(ActivityLog.at < datetime.combine(day_to + timedelta(days=1), datetime.min.time(), INDIA))
    if before:
        stmt = stmt.where(ActivityLog.id < before)
    rows = db.execute(stmt).all()
    names = Names(db)
    out = [{
        "id": a.id, "at": a.at.isoformat(), "action": a.action,
        "kind": next((k for k, (_, acts) in KINDS.items() if a.action in acts), "other"),
        "user_id": u.id if u else None,
        "user": u.name if u else None,
        "email": u.email if u else a.detail.get("email"),  # failed sign-ins for an unknown email
        "company": c.name if c else None,
        "staff": bool(c and c.is_novus),
        "summary": describe(a.action, a.detail or {}, names, known=u is not None),
        "ip": a.ip,
    } for a, u, c in rows[:PAGE]]
    return {"rows": out, "more": len(rows) > PAGE}


@router.get("/filters")
def filters(db: Session = Depends(get_db)):
    """What the filter menus offer: every company, every person, every kind of activity."""
    companies = db.scalars(select(Company).order_by(Company.is_novus.desc(), Company.name)).all()
    users = db.scalars(select(User).order_by(User.name)).all()
    return {
        "companies": [{"id": "novus" if c.is_novus else str(c.id), "name": "Novus staff" if c.is_novus else c.name}
                      for c in companies],
        "users": [{"id": u.id, "name": u.name, "email": u.email,
                   "company": "novus" if u.company.is_novus else str(u.company_id)} for u in users],
        "kinds": [{"id": k, "label": label} for k, (label, _) in KINDS.items()],
    }


# ---------- worth a look ----------

def watch(db: Session) -> list:
    """Patterns worth a human look: heavy copying, a login that may be shared, password guessing.
    These are prompts to check, not accusations; the wording on screen says so."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=WATCH_DAYS)
    month_start = datetime.now(INDIA).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    recent = db.scalars(select(ActivityLog).where(
        ActivityLog.at >= min(since, month_start),
        ActivityLog.action.in_(("export", "sign_in", "sign_in_failed", "sign_in_locked", "sign_in_throttled",
                                "password_change_failed")))).all()
    people = {u.id: u for u in db.scalars(select(User))}
    flags = []

    def flag(key: str, user_id: Optional[int], title: str, detail: str, kind: str, at: datetime):
        u = people.get(user_id)
        flags.append({"key": key, "user_id": user_id, "user": u.name if u else None,
                      "company": u.company.name if u else None, "title": title, "detail": detail,
                      "kind": kind, "latest": at.isoformat()})

    exports = [a for a in recent if a.action == "export"]
    month = defaultdict(list)
    for a in exports:
        if a.at >= month_start:
            month[a.user_id].append(a)
    for uid, items in month.items():
        if len(items) >= WATCH_EXPORTS_MONTH:
            flag(f"month-{uid}", uid, "Close to the monthly copy allowance",
                 f"{len(items)} of {EXPORT_LIMIT} copies to Excel used this month.", "exports", max(a.at for a in items))

    by_day = defaultdict(list)
    for a in exports:
        if a.at >= since:
            by_day[(a.user_id, india_day(a.at))].append(a)
    for (uid, day), items in sorted(by_day.items(), key=lambda kv: kv[0][1], reverse=True):
        if len(items) >= WATCH_EXPORTS_DAY:
            flag(f"day-{uid}-{day}", uid, "Many copies in one day",
                 f"{len(items)} copies to Excel on {day.day} {day.strftime('%b')}.", "exports", max(a.at for a in items))

    sign_ins = [a for a in recent if a.action == "sign_in" and a.at >= since]
    replaced = Counter(a.user_id for a in sign_ins if (a.detail or {}).get("replaced_other_device"))
    for uid, n in replaced.items():
        if n >= WATCH_REPLACED:
            last = max(a.at for a in sign_ins if a.user_id == uid)
            flag(f"shared-{uid}", uid, "This login may be shared",
                 f"Signed in {n} times in {WATCH_DAYS} days while already signed in on another device.", "sign_ins", last)

    places = defaultdict(set)
    for a in sign_ins:
        if a.ip:
            places[(a.user_id, india_day(a.at))].add(a.ip)
    for (uid, day), ips in places.items():
        if len(ips) >= WATCH_PLACES_DAY:
            last = max(a.at for a in sign_ins if a.user_id == uid and india_day(a.at) == day)
            flag(f"places-{uid}-{day}", uid, "Signed in from several places in one day",
                 f"{len(ips)} different internet addresses on {day.day} {day.strftime('%b')}.", "sign_ins", last)

    failed = [a for a in recent if a.action in ("sign_in_failed", "sign_in_locked", "sign_in_throttled",
                                                 "password_change_failed") and a.at >= since]
    per_user = Counter(a.user_id for a in failed if a.user_id)
    for uid, n in per_user.items():
        if n >= WATCH_FAILED:
            last = max(a.at for a in failed if a.user_id == uid)
            flag(f"failed-{uid}", uid, "Repeated wrong passwords",
                 f"{n} failed sign-ins in {WATCH_DAYS} days. The login locks for a while after 5 in a row.", "sign_ins", last)
    per_address = Counter(a.ip for a in failed if a.ip)
    for ip, n in per_address.items():
        if n >= WATCH_FAILED_ADDRESS:
            last = max(a.at for a in failed if a.ip == ip)
            emails = len({a.user_id or (a.detail or {}).get("email") for a in failed if a.ip == ip})
            flag(f"guess-{ip}", None, "Someone may be guessing passwords",
                 f"{n} failed sign-ins from internet address {ip}, across {emails} email address(es), in {WATCH_DAYS} days.",
                 "sign_ins", last)

    return sorted(flags, key=lambda f: f["latest"], reverse=True)


@router.get("/watch")
def worth_a_look(db: Session = Depends(get_db)):
    return watch(db)
