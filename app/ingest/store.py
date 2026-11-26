"""Save a file into the database, and publish, undo or discard it.

Files are read in the background, batch by batch ('processing'), then become 'checked' (or 'failed').
A checked upload's buyers stay invisible to clients until it is published.
Publishing is blocked while any check fails (unknown model, buyers already uploaded).
"""
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.catalogue import QUESTIONS
from app.ingest.reader import FileProblem, FileReader
from app.models import AnswerOption, Brand, CarModel, ModelName, Question, Respondent, Upload, User
from app.rules import ROLLBACK_HOURS, sample_status

MODELS_FILE = Path(__file__).resolve().parent.parent / "reference" / "models.json"


class UploadRefused(Exception):
    """The upload or action can't go ahead; the message says why in plain words."""


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- reference lists ----------

def sync_reference(db: Session) -> None:
    """Make sure every catalogue question, brand and model exists. Staff edits to models are kept."""
    existing = {q.code: q for q in db.scalars(select(Question))}
    for sort, q in enumerate(QUESTIONS):
        row = existing.get(q.code) or Question(code=q.code, wording="")
        row.title, row.theme, row.kind, row.sort = q.title, q.theme, q.kind, sort
        if q.wording:  # otherwise the wording comes from the survey file (see save_wordings)
            row.wording = q.wording
        db.add(row)

    if db.scalar(select(func.count()).select_from(Brand)) == 0:
        ref = json.loads(MODELS_FILE.read_text())
        brands = {name: Brand(name=name) for name in ref["brands"]}
        db.add_all(brands.values())
        db.flush()
        for m in ref["models"]:
            model = CarModel(name=m["name"], brand_id=brands[m["brand"]].id, e5_codes=m["e5_codes"])
            db.add(model)
            db.flush()
            db.add_all(ModelName(name=n, model_id=model.id) for n in m["names"])
    db.flush()


def save_wordings(db: Session, wordings: dict) -> None:
    """Keep the survey's exact question wording (from row 1) for questions that don't have one yet."""
    for q in db.scalars(select(Question).where(Question.code.in_(wordings.keys()))):
        if not q.wording:
            q.wording = wordings[q.code][:1000]


class Options:
    """Answer options in the database, created the first time a file uses them."""

    def __init__(self, db: Session):
        self.db = db
        self.questions = {q.code: q for q in db.scalars(select(Question))}
        self.order = {q.code: q.order for q in QUESTIONS}
        self.rows: dict = defaultdict(dict)  # question code -> {key: AnswerOption}
        code_of = {q.id: code for code, q in self.questions.items()}
        for o in db.scalars(select(AnswerOption)):
            self.rows[code_of[o.question_id]][o.key] = o
        self.created: dict = defaultdict(list)  # question code -> options this upload added
        self.ids: dict = defaultdict(dict)  # question code -> {key: option id}
        for code, opts in self.rows.items():
            self.ids[code] = {k: o.id for k, o in opts.items()}

    def add(self, options: dict) -> None:
        """Create any option not seen before (options: {question code: {key: label}}, in file order)."""
        added = False
        for code, opts in options.items():
            have = self.rows[code]
            next_sort = max((o.sort for o in have.values()), default=-1) + 1
            for key, label in opts.items():
                if key not in have:
                    sort = self.order[code].index(key) if key in self.order[code] else 1000 + next_sort
                    next_sort += 1
                    have[key] = AnswerOption(question_id=self.questions[code].id, key=key, label=label, sort=sort)
                    self.db.add(have[key])
                    self.created[code].append(have[key])
                    added = True
        if added:
            self.db.flush()
            for code, opts in self.rows.items():
                self.ids[code] = {k: o.id for k, o in opts.items()}

    def settle_labels(self, options: dict) -> dict:
        """Final labels for the options this upload created (several-answer labels are only known at the end).
        Returns {question code: [labels added]} for the report."""
        for code, created in self.created.items():
            for o in created:
                o.label = options.get(code, {}).get(o.key, o.label)
        return {code: [o.label for o in created] for code, created in self.created.items()}


# ---------- reading a file into the database ----------

BATCH = 2000  # buyers saved at a time: memory stays small whatever the file size
RUNNING: set = set()  # uploads this server is reading right now


def start_upload(db: Session, user: User, filename: str, path: str, digest: str) -> tuple:
    """Checks that can be answered at once (duplicate file, header rows), then records the upload as
    'processing'. Returns (upload, reader); read_upload() does the rest in the background."""
    same = db.scalar(select(Upload).where(Upload.sha256 == digest,
                                          Upload.status.in_(("processing", "checked", "published"))))
    if same:
        raise UploadRefused(f"This exact file was already uploaded as “{same.filename}”.")
    reader = FileReader(path)
    upload = Upload(filename=filename, sha256=digest, status="processing", uploaded_by=user.id,
                    report={"read": 0})
    db.add(upload)
    db.flush()
    RUNNING.add(upload.id)
    return upload, reader


def read_upload(upload_id: int, reader: FileReader, ip: str = None) -> None:
    """Read every buyer into the database in batches, then mark the upload 'checked' (or 'failed').
    Runs after the web request has answered, so the admin sees progress instead of a frozen page."""
    from app.activity import record
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        sync_reference(db)
        options = Options(db)
        refs = references(db)
        total = 0
        batch = []
        for b in reader.buyers():
            batch.append(b)
            if len(batch) >= BATCH:
                total += save_batch(db, upload_id, batch, reader, options, refs)
                batch = []
                progress(upload_id, total)
        total += save_batch(db, upload_id, batch, reader, options, refs)
        rest = reader.finish()
        if total == 0:
            raise UploadRefused("No buyers could be read from this file.")
        save_wordings(db, rest["wordings"])
        new_options = options.settle_labels(rest["options"])
        upload = db.get(Upload, upload_id)
        dropped = Counter(reason for _, reason in reader.dropped)
        upload.status = "checked"
        upload.report = {
            "buyers": total,
            "columns_imported": reader.imported_columns,
            "columns_dropped": dict(dropped),
            "dropped_personal": [c for c, r in reader.dropped if r == "possibly personal"],
            "warnings": [{"what": w, "question": q, "buyers": n} for (w, q), n in reader.warnings.items()],
            "new_options": new_options,
        }
        user = db.get(User, upload.uploaded_by)
        record(db, "upload_checked", user, ip, upload_id=upload_id, filename=upload.filename, buyers=total)
        db.commit()
    except Exception as e:
        db.rollback()
        message = str(e) if isinstance(e, (UploadRefused, FileProblem)) else \
            f"The file couldn’t be read to the end ({type(e).__name__}). Check it opens in Excel, then upload it again."
        fail_upload(upload_id, message, ip)
    finally:
        db.close()
        RUNNING.discard(upload_id)


def progress(upload_id: int, read: int) -> None:
    """Show how far the reading has got (written separately, so the admin sees it while the batch work goes on)."""
    from app.db import SessionLocal

    with SessionLocal() as db:
        u = db.get(Upload, upload_id)
        u.report = {"read": read}
        db.commit()


def fail_upload(upload_id: int, message: str, ip: str = None) -> None:
    from app.activity import record
    from app.db import SessionLocal

    with SessionLocal() as db:
        u = db.get(Upload, upload_id)
        db.execute(delete(Respondent).where(Respondent.upload_id == upload_id))  # nothing half-read is kept
        u.status, u.report = "failed", {"error": message}
        record(db, "upload_refused", db.get(User, u.uploaded_by), ip, filename=u.filename, reason=message)
        db.commit()


def recover_interrupted(db: Session) -> None:
    """An upload left 'processing' that this server isn't reading was interrupted (e.g. a restart)."""
    for u in db.scalars(select(Upload).where(Upload.status == "processing")):
        if u.id not in RUNNING:
            db.execute(delete(Respondent).where(Respondent.upload_id == u.id))
            u.status, u.report = "failed", {"error": "The server restarted while reading this file. Upload it again."}
    db.commit()


def references(db: Session) -> dict:
    qid = dict(db.execute(select(Question.code, Question.id)).all())
    e5_model = {}
    for model_id, codes in db.execute(select(CarModel.id, CarModel.e5_codes)).all():
        for c in codes:
            e5_model[c] = model_id
    return {"qid": qid, "e5_model": e5_model}


def save_batch(db: Session, upload_id: int, buyers: list, reader: FileReader, options: Options, refs: dict) -> int:
    """Bulk-write one batch of buyers and their answers with PostgreSQL COPY. Returns how many were saved."""
    if not buyers:
        return 0
    options.add(reader.options)  # answers first seen in this batch
    option_ids, qid, e5_model = options.ids, refs["qid"], refs["e5_model"]
    cur = db.connection().connection.driver_connection.cursor()
    with cur.copy("COPY respondents (upload_id, record, model_name, purchase_month, quarter) FROM STDIN") as cp:
        for b in buyers:
            cp.write_row((upload_id, b.record, b.model_name, b.purchase_month, b.quarter))
    rid = dict(db.execute(select(Respondent.record, Respondent.id).where(
        Respondent.upload_id == upload_id, Respondent.record.in_([b.record for b in buyers]))).all())

    with cur.copy("COPY asked (respondent_id, question_id) FROM STDIN") as cp:
        for b in buyers:
            for code in b.asked:
                cp.write_row((rid[b.record], qid[code]))
    with cur.copy("COPY answers (respondent_id, option_id) FROM STDIN") as cp:
        for b in buyers:
            for code, keys in b.answers.items():
                for key in keys:
                    cp.write_row((rid[b.record], option_ids[code][key]))
    with cur.copy("COPY number_answers (respondent_id, question_id, value) FROM STDIN") as cp:
        for b in buyers:
            for code, value in b.numbers.items():
                cp.write_row((rid[b.record], qid[code], value))
    with cur.copy("COPY considerations (respondent_id, rank, e5_code, model_id, test_drive, dealer_visit) FROM STDIN") as cp:
        for b in buyers:
            for rank, code, drive, visit in b.considered:
                cp.write_row((rid[b.record], rank, code, e5_model.get(code), drive, visit))
    with cur.copy("COPY rejections (respondent_id, rank, option_id) FROM STDIN") as cp:
        for b in buyers:
            for rank, key in set(b.rejections):
                cp.write_row((rid[b.record], rank, option_ids["E7"][key]))
    return len(buyers)


def check_file(db: Session, user: User, filename: str, path: str) -> Upload:
    """Read a file completely, straight away (for tests and scripts; the upload page reads in the background)."""
    digest = sha256_of(path)
    upload, reader = start_upload(db, user, filename, path, digest)
    db.commit()
    read_upload(upload.id, reader)
    db.expire_all()
    return db.get(Upload, upload.id)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- live checks (re-run every time, so fixes made elsewhere count straight away) ----------

def checks(db: Session, upload: Upload) -> dict:
    by_name = db.execute(
        select(Respondent.model_name, Respondent.quarter, func.count())
        .where(Respondent.upload_id == upload.id).group_by(Respondent.model_name, Respondent.quarter)
    ).all()
    known = {n.name: n.model_id for n in db.scalars(select(ModelName))}
    models = {m.id: m for m in db.scalars(select(CarModel))}

    names: dict = defaultdict(lambda: {"buyers": 0, "quarters": {}})
    for name, quarter, n in by_name:
        names[name]["buyers"] += n
        names[name]["quarters"][quarter] = {"buyers": n, "status": sample_status(n)}
    model_rows = []
    for name, info in sorted(names.items()):
        m = models.get(known.get(name))
        model_rows.append({"name_in_file": name, "model": m.name if m else None,
                           "brand": m.brand.name if m else None, **info})

    other = (select(Respondent.record).join(Upload)
             .where(Upload.id != upload.id, Upload.status.in_(("checked", "published"))))
    duplicates = db.scalar(select(func.count()).select_from(Respondent)
                           .where(Respondent.upload_id == upload.id, Respondent.record.in_(other)))

    blocking = []
    unknown = [r["name_in_file"] for r in model_rows if r["model"] is None]
    if unknown:
        blocking.append({"code": "unknown_model", "names": unknown,
                         "message": "Model not in the model list. Match it to an existing model or add it as new."})
    if duplicates:
        blocking.append({"code": "duplicates", "buyers": duplicates,
                         "message": f"{duplicates} buyers in this file are already in another upload."})

    quarters = sorted({q for _, q, _ in by_name if q})
    rollback_until = (upload.published_at + timedelta(hours=ROLLBACK_HOURS)) if upload.published_at else None
    return {"models": model_rows, "quarters": quarters, "blocking": blocking,
            "can_publish": upload.status == "checked" and not blocking,
            "rollback_until": rollback_until.isoformat() if rollback_until else None,
            "can_roll_back": bool(upload.status == "published" and rollback_until and rollback_until > now())}


# ---------- publish, undo, discard ----------

def publish(db: Session, upload: Upload) -> None:
    if upload.status != "checked":
        raise UploadRefused("Only a checked upload can be published.")
    c = checks(db, upload)
    if c["blocking"]:
        raise UploadRefused("Fix the problems listed in the checks before publishing.")
    upload.status, upload.published_at = "published", now()


def roll_back(db: Session, upload: Upload) -> None:
    if upload.status != "published":
        raise UploadRefused("Only a published upload can be undone.")
    if upload.published_at + timedelta(hours=ROLLBACK_HOURS) < now():
        raise UploadRefused(f"The {ROLLBACK_HOURS}-hour window to undo this upload has passed.")
    upload.status, upload.rolled_back_at = "rolled_back", now()


def discard(db: Session, upload: Upload) -> None:
    """Delete an upload that was never published, with all its buyers."""
    if upload.status == "published":
        raise UploadRefused("A published upload can’t be deleted. Undo it first (within 48 hours).")
    if upload.status == "processing":
        raise UploadRefused("This file is still being read. Wait until it finishes, then delete it.")
    db.execute(delete(Upload).where(Upload.id == upload.id))


def resolve_model_name(db: Session, name: str, model_id: int = None, new_model: str = None, brand: str = None) -> CarModel:
    """Answer an 'unknown model' check: point the name at an existing model, or create a new one."""
    if db.get(ModelName, name):
        raise UploadRefused(f"“{name}” is already matched to a model.")
    if model_id is not None:
        model = db.get(CarModel, model_id)
        if model is None:
            raise UploadRefused("That model doesn’t exist.")
    else:
        if not new_model or not brand:
            raise UploadRefused("Give the new model’s name and brand.")
        b = db.scalar(select(Brand).where(Brand.name == brand)) or Brand(name=brand)
        db.add(b)
        db.flush()
        model = CarModel(name=new_model.strip(), brand_id=b.id, e5_codes=[])
        db.add(model)
        db.flush()
    db.add(ModelName(name=name, model_id=model.id))
    return model
