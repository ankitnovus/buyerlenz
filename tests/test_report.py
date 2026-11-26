"""The Summary report: up to 5 models side by side, master-file order, every rule per column; download = one export."""
import io

import openpyxl

from tests.test_engine import buyers, client, publish
from tests.test_profile import entity_key


def report(c, keys, status=200):
    r = c.post("/api/client/report", json={"entities": keys})
    assert r.status_code == status, r.text
    return r.json()


def section(data, title):
    return next(s for s in data["sections"] if s["title"] == title)


def test_report_columns_follow_the_master_order_and_rules():
    publish(buyers(40, buyer_type="First Time Buyer"), buyers(5, model="Seltos"))
    c = client()
    creta, seltos = entity_key(c, "Creta"), entity_key(c, "Seltos")
    data = report(c, [creta, seltos])
    assert [col["label"] for col in data["columns"]] == ["Creta", "Seltos"]
    assert data["columns"][0]["n"] == 40 and "n" not in data["columns"][1]  # under 10: no count sent
    d2 = section(data, "Type of Car Buyer")
    assert [r["label"] for r in d2["rows"]] == ["First Time Buyer", "Additional Car Buyer", "Replacement Car Buyer"]
    assert d2["rows"][0]["cells"][creta] == {"pct": 100.0}
    assert d2["rows"][1]["cells"][creta] == {"pct": 0.0}  # nobody chose it: a true zero, shown as "-"
    assert d2["rows"][0]["cells"][seltos] == {"status": "hidden"}  # nothing at all for a hidden column


def test_top_five_lists_name_the_answers_and_leave_out_others():
    publish(buyers(40, safety=True, looks=True))
    c = client()
    creta = entity_key(c, "Creta")
    top = section(report(c, [creta]), "Top 5 Reasons to Buy This Car")
    assert top["rows"][0]["label"] == "Reason 1 - %"
    assert {top["rows"][0]["cells"][creta]["text"], top["rows"][1]["cells"][creta]["text"]} == {"Safety", "Looks"}


def test_no_more_than_five_models():
    publish(buyers(40))
    c = client()
    report(c, [entity_key(c, "Creta")] * 6 + ["m:1"], status=422)


def test_download_is_a_formatted_workbook_and_counts_as_one_export():
    publish(buyers(40))
    c = client()
    creta = entity_key(c, "Creta")
    r = c.post("/api/client/report/download", json={"entities": [creta]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    ws = openpyxl.load_workbook(io.BytesIO(r.content)).active
    assert ws["A1"].value.startswith("BuyerLenz summary report")
    assert "Arjun Mehta" in ws["A2"].value  # stamped with who downloaded it
    assert ws["B5"].value == "Creta" and ws["B6"].value == 40
    assert c.get("/api/client/allowance").json()["used"] == 1
