import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from app.db import SessionLocal
from app.ingest.reader import read_file
from app.models import ActivityLog, Answer, Asked, Consideration, Rejection, Respondent, Upload
from tests.conftest import CHROME_WIN, PASSWORD, new_browser
from tests.sample_file import buyer, make_file

ADMIN = {"email": "admin@novus.example", "password": PASSWORD}
CLIENT = {"email": "arjun@hyundai.example", "password": PASSWORD}
CRETA_FILE = Path(__file__).resolve().parents[2] / "test-kit" / "BuyerLenz_Creta_RAWDATA_2026_10_01_C1.xlsx"


def admin():
    b = new_browser()
    b.post("/api/auth/login", json=ADMIN)
    return b


def upload(browser, data: bytes, name="sample.xlsx"):
    """Upload, then fetch the finished result (the test browser waits for the background reading)."""
    r = browser.post("/api/admin/uploads", files={"file": (name, data)})
    if r.status_code != 200:
        return r
    assert r.json()["status"] == "processing"
    return browser.get(f"/api/admin/uploads/{r.json()['id']}")


def count(model, *where):
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


# ---------- reading a file ----------

def test_reader_keeps_only_catalogue_questions_and_drops_personal_and_free_text():
    p = read_file(io.BytesIO(make_file([buyer("1")])))
    dropped = dict(p.dropped)
    assert dropped["X1"] == "possibly personal"
    assert dropped["E15r1oe"] == "free text"
    assert "X1" not in str(p.options) and "free words" not in str(p.options)


def test_reader_blank_means_not_asked():
    p = read_file(io.BytesIO(make_file([buyer("1", f3="Low Fuel Economy"), buyer("2", f3=None)])))
    asked_f3 = [b.record for b in p.buyers if "F3" in b.asked]
    assert asked_f3 == ["1"]


def test_reader_bands_age_and_income_and_sets_quarter():
    p = read_file(io.BytesIO(make_file([buyer("1", age="25", income="100000", month="March 2026")])))
    b = p.buyers[0]
    assert b.answers["B1"] == {"18–25"}
    assert b.answers["B6"] == {"₹80,001–1,00,000"}
    assert b.numbers["B1"] == 25
    assert b.quarter == "2026Q1"


def test_reader_ranks_considered_models_by_code_and_links_rejections():
    p = read_file(io.BytesIO(make_file([buyer("1")])))
    b = p.buyers[0]
    assert b.considered == [(1, "9", True, False), (2, "55", False, True)]
    assert sorted(b.rejections) == [(1, "E7r78"), (2, "E7r79")]


def test_reader_skips_buyers_without_a_purchase_month():
    p = read_file(io.BytesIO(make_file([buyer("1"), buyer("2", month="sometime")])))
    assert [b.record for b in p.buyers] == ["1"]
    assert sum(p.warnings.values()) == 1


# ---------- uploading, publishing, undoing ----------

def test_upload_check_publish_and_undo():
    a = admin()
    r = upload(a, make_file([buyer(str(i)) for i in range(12)]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "checked" and body["buyers"] == 12
    assert body["checks"]["blocking"] == [] and body["can_publish"]
    assert body["checks"]["models"][0]["model"] == "Creta"
    assert body["checks"]["models"][0]["quarters"]["2025Q4"] == {"buyers": 12, "status": "caution"}
    assert count(Respondent) == 12 and count(Consideration) == 24 and count(Rejection) == 24
    assert count(Asked) > 0 and count(Answer) > 0

    uid = body["id"]
    assert a.post(f"/api/admin/uploads/{uid}/publish").json()["status"] == "published"
    undone = a.post(f"/api/admin/uploads/{uid}/roll-back").json()
    assert undone["status"] == "rolled_back"
    with SessionLocal() as db:
        actions = [x.action for x in db.scalars(select(ActivityLog))]
    assert {"upload_checked", "upload_published", "upload_rolled_back"} <= set(actions)


def test_undo_is_refused_after_48_hours():
    a = admin()
    uid = upload(a, make_file([buyer("1")])).json()["id"]
    a.post(f"/api/admin/uploads/{uid}/publish")
    with SessionLocal() as db:
        db.execute(update(Upload).values(published_at=datetime.now(timezone.utc) - timedelta(hours=49)))
        db.commit()
    r = a.post(f"/api/admin/uploads/{uid}/roll-back")
    assert r.status_code == 409 and "48-hour" in r.json()["detail"]["message"]


def test_discard_removes_an_unpublished_upload_and_its_buyers():
    a = admin()
    uid = upload(a, make_file([buyer("1"), buyer("2")])).json()["id"]
    assert a.delete(f"/api/admin/uploads/{uid}").status_code == 200
    assert count(Upload) == 0 and count(Respondent) == 0 and count(Answer) == 0


def test_same_file_twice_is_refused():
    a = admin()
    data = make_file([buyer("1")])
    upload(a, data)
    r = upload(a, data)
    assert r.status_code == 400 and "already uploaded" in r.json()["detail"]["message"]


def test_buyers_already_uploaded_block_publishing():
    a = admin()
    upload(a, make_file([buyer("1"), buyer("2")]))
    second = upload(a, make_file([buyer("2"), buyer("3", age="40")])).json()
    assert [b["code"] for b in second["checks"]["blocking"]] == ["duplicates"]
    assert a.post(f"/api/admin/uploads/{second['id']}/publish").status_code == 409


def test_unknown_model_blocks_until_matched():
    a = admin()
    r = upload(a, make_file([buyer("1", model="Creta N Line")])).json()
    assert r["checks"]["blocking"][0]["code"] == "unknown_model"
    assert a.post(f"/api/admin/uploads/{r['id']}/publish").status_code == 409

    creta = next(m for m in a.get("/api/admin/models").json() if m["name"] == "Creta")
    assert a.post("/api/admin/model-names", json={"name": "Creta N Line", "model_id": creta["id"]}).status_code == 200
    assert a.post(f"/api/admin/uploads/{r['id']}/publish").json()["status"] == "published"


def test_unknown_model_can_be_added_as_new():
    a = admin()
    r = upload(a, make_file([buyer("1", model="Brand New SUV")])).json()
    a.post("/api/admin/model-names", json={"name": "Brand New SUV", "new_model": "New SUV", "brand": "Hyundai"})
    detail = a.get(f"/api/admin/uploads/{r['id']}").json()
    assert detail["checks"]["blocking"] == []
    assert detail["checks"]["models"][0]["model"] == "New SUV"


def test_only_excel_files_and_only_admins():
    a = admin()
    assert upload(a, b"name,phone\n", name="data.csv").status_code == 400
    assert upload(a, b"not really excel", name="fake.xlsx").status_code == 400
    client = new_browser(CHROME_WIN)
    client.post("/api/auth/login", json=CLIENT)
    assert upload(client, make_file([buyer("1")])).status_code == 403


@pytest.mark.skipif(not CRETA_FILE.exists(), reason="Creta demo file not present (it is never stored in git)")
def test_real_creta_demo_file():
    a = admin()
    r = upload(a, CRETA_FILE.read_bytes(), name=CRETA_FILE.name)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["buyers"] == 700
    assert body["checks"]["blocking"] == []
    assert body["checks"]["quarters"] == ["2025Q3", "2025Q4", "2026Q1", "2026Q2"]
    assert body["report"]["columns_dropped"]["free text"] > 500


# ---------- large files: read in batches, in the background ----------

def test_files_larger_than_one_batch_are_saved_completely(monkeypatch):
    from app.ingest import store
    monkeypatch.setattr(store, "BATCH", 5)  # 12 buyers = three batches
    a = admin()
    r = upload(a, make_file([buyer(str(i), fuel="CNG" if i == 11 else "Petrol") for i in range(12)])).json()
    assert r["status"] == "checked" and r["buyers"] == 12
    assert count(Respondent) == 12
    assert "CNG" in r["report"]["new_options"]["E10"]  # an answer first seen in the last batch


def test_an_interrupted_upload_is_marked_failed_and_keeps_nothing():
    a = admin()
    uid = upload(a, make_file([buyer("1"), buyer("2")])).json()["id"]
    with SessionLocal() as db:  # as if the server restarted half way through reading
        db.execute(update(Upload).where(Upload.id == uid).values(status="processing"))
        db.commit()
    listed = next(u for u in a.get("/api/admin/uploads").json() if u["id"] == uid)
    assert listed["status"] == "failed" and "restarted" in listed["error"]
    assert count(Respondent, Respondent.upload_id == uid) == 0
    assert a.delete(f"/api/admin/uploads/{uid}").status_code == 200  # a failed upload can be cleared away


def test_a_file_still_being_read_cannot_be_deleted():
    a = admin()
    uid = upload(a, make_file([buyer("1")])).json()["id"]
    with SessionLocal() as db:
        db.execute(update(Upload).where(Upload.id == uid).values(status="processing"))
        db.commit()
    assert a.delete(f"/api/admin/uploads/{uid}").status_code == 409


def test_the_word_none_is_an_answer_not_an_empty_cell():
    p = read_file(io.BytesIO(make_file([buyer("1", buyer_type="None")])))
    assert p.buyers[0].answers["D2"] == {"None"}


def test_interview_number_may_be_called_respid():
    data = make_file([buyer("1"), buyer("2")])
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data))
    wb.active.cell(row=2, column=1, value="respid")  # how some survey exports name it
    out = io.BytesIO()
    wb.save(out)
    p = read_file(io.BytesIO(out.getvalue()))
    assert [b.record for b in p.buyers] == ["1", "2"]


def test_unchosen_feature_columns_create_no_options():
    p = read_file(io.BytesIO(make_file([buyer("1", safety=True, looks=False)])))
    assert list(p.options["E15"]) == ["E15r1"]  # "Looks" was never chosen, so it isn't an answer option
