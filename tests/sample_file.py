"""Builds small made-up survey files in the real layout (row 1 wording, row 2 codes). No real data."""
import io

from openpyxl import Workbook

CONSIDERABLE = (("9", "Grand Vitara"), ("47", "Creta"), ("55", "Seltos"))
REASONS = (("78", "Negative Recommendation in the Market"), ("79", "Too expensive"))

COLUMNS = [  # (code, wording)
    ("record", "record"),
    ("X1", "X1. Respondent Name"),
    ("E8", "E8. Model that you Purchased finally."),
    ("E9_1", "E9_1. Please provide the Month of Purchase of your Car"),
    ("B1", "B1. Please tell us your age in completed years."),
    ("B2", "B2. Please let us know your gender."),
    ("B6", "B6. Please let us know your Monthly Household Income?"),
    ("B6_1", "B6_1. Can you please provide a range within which your Monthly Household Income falls?"),
    ("noanswerB6_n1", "B6. Prefer not to say - Please let us know your Monthly Household Income?"),
    ("D2", "D2. What type of car buyer are you?"),
    ("E10", "E10. Which is the fuel type of your car?"),
    ("E3", "E3. Was the vehicle purchase financed through a loan?"),
    ("E4c2", "E4. Monthly EMI - Please tell us about the loan"),
    ("F1", "F1. Please tell us, Overall, how satisfied are you"),
    ("F3", "F3. Can you tell me one single reason why haven't you given 5 rating?"),
    ("F4", "F4. Please tell us, Overall, how satisfied were you with the dealership experience"),
    ("E15r1", "E15. Safety - What were the top five reasons for choosing this car"),
    ("E15r2", "E15. Looks - What were the top five reasons for choosing this car"),
    ("E15r1oe", "E15. Others (Please specify) - What were the top five reasons"),
] + [
    ("E5_1r1", "E5. I did not consider any other model - Which top three models"),
] + [
    (f"E5r{code}c{c}", f"E5. {name} - {part} - Which top three models")
    for code, name in CONSIDERABLE for c, part in ((1, "Consideration"), (2, "Test Drive"), (3, "Dealership Visit"))
] + [
    (f"E7r{reason}c{rank}", f"E7. {label} - What were the top 3 reasons for rejecting")
    for rank in (1, 2, 3) for reason, label in REASONS
]
NS = "Not selected"


def buyer(record, model="Creta", month="November 2025", age="30", income="120000", buyer_type="First Time Buyer",
          fuel="Petrol", loan="Yes", f1="Satisfied", f3="Low Fuel Economy", f4="Extremely Satisfied",
          safety=True, looks=False, emi=None, considered=(("9", True, False), ("55", False, True)), rejected=((1, "78"), (2, "79"))):
    row = {"record": record, "X1": "Some Person", "E8": model, "E9_1": month, "B1": age, "B2": "Male", "B6": income,
           "B6_1": None, "noanswerB6_n1": "Not Selected", "D2": buyer_type, "E10": fuel, "E3": loan, "E4c2": emi,
           "F1": f1, "F3": f3, "F4": f4,
           "E15r1": "Safety" if safety else NS, "E15r2": "Looks" if looks else NS, "E15r1oe": "free words typed in"}
    row["E5_1r1"] = NS if considered else "I did not consider any other model"
    picked = {code: (drive, visit) for code, drive, visit in considered}
    for code, name in CONSIDERABLE:
        on = code in picked
        row[f"E5r{code}c1"] = f"{name} - Consideration" if on else NS
        row[f"E5r{code}c2"] = f"{name} - Test Drive" if on and picked[code][0] else NS
        row[f"E5r{code}c3"] = f"{name} - Dealership Visit" if on and picked[code][1] else NS
    for rank in (1, 2, 3):
        for reason, label in REASONS:
            row[f"E7r{reason}c{rank}"] = (label if (rank, reason) in rejected else NS) if considered else None
    return row


def make_file(buyers) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append([w for _, w in COLUMNS])
    ws.append([c for c, _ in COLUMNS])
    for b in buyers:
        ws.append([b.get(c) for c, _ in COLUMNS])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
