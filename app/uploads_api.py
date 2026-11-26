"""Admin: upload survey files, review the checks, publish, undo, and answer model-list questions."""
import hashlib
import os
import tempfile
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.activity import log
from app.auth import require_admin
from app.db import get_db
from app.ingest.reader import FileProblem, check_archive
from app.ingest.store import (UploadRefused, checks, discard, publish, read_upload, recover_interrupted,
                              resolve_model_name, roll_back, start_upload, sync_reference)
from app.models import CarModel, Upload, User
from app.rules import MAX_UPLOAD_MB

router = APIRouter(prefix="/api/admin", tags=["uploads"])


def refuse(message: str, status: int = 400):
    raise HTTPException(status, detail={"code": "refused", "message": message})


def describe(db: Session, u: Upload, full: bool = False) -> dict:
    uploader = db.get(User, u.uploaded_by)
    out = {"id": u.id, "filename": u.filename, "status": u.status, "uploaded_at": u.uploaded_at.isoformat(),
           "uploaded_by": uploader.name if uploader else None, "buyers": u.report.get("buyers", 0),
           "published_at": u.published_at.isoformat() if u.published_at else None,
           "rolled_back_at": u.rolled_back_at.isoformat() if u.rolled_back_at else None}
    out["read"] = u.report.get("read", 0) if u.status == "processing" else None
    out["error"] = u.report.get("error") if u.status == "failed" else None
    if u.status in ("processing", "failed"):
        out.update(models=[], quarters=[], blocking=0, can_publish=False, can_roll_back=False, rollback_until=None)
        if full:
            out.update(report=u.report, checks=None)
        return out
    c = checks(db, u)
    out.update(models=[m["model"] or m["name_in_file"] for m in c["models"]], quarters=c["quarters"],
               blocking=len(c["blocking"]), can_publish=c["can_publish"], can_roll_back=c["can_roll_back"],
               rollback_until=c["rollback_until"])
    if full:
        out.update(report=u.report, checks=c)
    return out


def get_upload(db: Session, upload_id: int) -> Upload:
    u = db.get(Upload, upload_id)
    if u is None:
        refuse("That upload doesn’t exist.", 404)
    return u


@router.get("/uploads")
def list_uploads(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    recover_interrupted(db)
    return [describe(db, u) for u in db.scalars(select(Upload).order_by(Upload.id.desc()))]


def save_to_disk(file: UploadFile) -> tuple:
    """Copy the upload to a private temporary file in pieces (never the whole file in memory).
    Returns (path, sha256). Refuses files over the size limit or that aren't Excel."""
    limit = MAX_UPLOAD_MB * 1024 * 1024
    h, size = hashlib.sha256(), 0
    fd, path = tempfile.mkstemp(prefix="buyerlenz-", suffix=".xlsx")
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := file.file.read(1024 * 1024):
                if size == 0 and not chunk.startswith(b"PK"):
                    refuse("This doesn’t look like an Excel (.xlsx) file.")
                size += len(chunk)
                if size > limit:
                    refuse(f"The file is larger than {MAX_UPLOAD_MB} MB.")
                h.update(chunk)
                out.write(chunk)
        if size == 0:
            refuse("The file is empty.")
        return path, h.hexdigest()
    except BaseException:
        os.remove(path)
        raise


def read_then_tidy(upload_id: int, reader, path: str, ip: Optional[str]) -> None:
    try:
        read_upload(upload_id, reader, ip)
    finally:
        os.remove(path)


@router.post("/uploads")
def upload_file(file: UploadFile, request: Request, background: BackgroundTasks, db: Session = Depends(get_db),
                user: User = Depends(require_admin)):
    """Quick checks now; the buyers are read in the background and the upload page shows progress."""
    name = file.filename or "file.xlsx"
    if not name.lower().endswith(".xlsx"):
        refuse("Only Excel files (.xlsx) can be uploaded.")
    path, digest = save_to_disk(file)
    try:
        check_archive(path)
        u, reader = start_upload(db, user, name, path, digest)
    except (FileProblem, UploadRefused) as e:
        os.remove(path)
        db.rollback()
        log(db, request, "upload_refused", user, filename=name, reason=str(e))
        db.commit()
        refuse(str(e))
    log(db, request, "upload_started", user, upload_id=u.id, filename=name)
    db.commit()
    background.add_task(read_then_tidy, u.id, reader, path, request.client.host if request.client else None)
    return describe(db, u, full=True)


@router.get("/uploads/{upload_id}")
def upload_detail(upload_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return describe(db, get_upload(db, upload_id), full=True)


def act(upload_id: int, request: Request, db: Session, user: User, action, logged: str) -> dict:
    u = get_upload(db, upload_id)
    try:
        action(db, u)
    except UploadRefused as e:
        refuse(str(e), 409)
    log(db, request, logged, user, upload_id=upload_id, filename=u.filename)
    db.commit()
    return describe(db, u, full=True) if logged != "upload_discarded" else {"ok": True}


@router.post("/uploads/{upload_id}/publish")
def publish_upload(upload_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return act(upload_id, request, db, user, publish, "upload_published")


@router.post("/uploads/{upload_id}/roll-back")
def roll_back_upload(upload_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return act(upload_id, request, db, user, roll_back, "upload_rolled_back")


@router.delete("/uploads/{upload_id}")
def discard_upload(upload_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return act(upload_id, request, db, user, discard, "upload_discarded")


# ---------- model list ----------

@router.get("/models")
def list_models(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    sync_reference(db)
    db.commit()
    models = db.scalars(select(CarModel)).all()
    return sorted(({"id": m.id, "name": m.name, "brand": m.brand.name} for m in models),
                  key=lambda m: (m["brand"], m["name"]))


class ModelNameAnswer(BaseModel):
    name: str  # the name as written in the file
    model_id: Optional[int] = None  # match to this existing model...
    new_model: Optional[str] = None  # ...or create a new model with this name
    brand: Optional[str] = None


@router.post("/model-names")
def answer_model_name(body: ModelNameAnswer, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_admin)):
    try:
        model = resolve_model_name(db, body.name, body.model_id, body.new_model, body.brand)
    except UploadRefused as e:
        refuse(str(e), 409)
    log(db, request, "model_name_matched", user, name=body.name, model=model.name, new=body.model_id is None)
    db.commit()
    return {"name": body.name, "model": model.name}
