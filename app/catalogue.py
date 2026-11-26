"""The questions BuyerLenz imports, how each is read from the file, and what clients see it called.

Only columns that belong to a question listed here are imported. Everything else in a file (free-text
answers, "Please specify" boxes, interviewer details, anything personal or unknown) is dropped and listed
in the upload report. That is deliberate: a new or unexpected column never reaches clients by accident.

Kinds:
  single    one column, the answer written as words
  multi     one column per answer option, holding the option or "Not selected"
  slots     a few columns that each hold one answer (e.g. first and second reason)
  banded    an exact number grouped into bands (age, income)
  number    an exact number kept as it is (price paid, EMI...)
  consider  the models-considered grid (E5)
  reject    reasons for rejecting each considered model (E7)
"""
import re
from dataclasses import dataclass
from typing import Optional

SATISFACTION = ("Extremely Dissatisfied", "Dissatisfied", "Average", "Satisfied", "Extremely Satisfied")
SATISFIED_LABEL = "Satisfied (4–5)"  # headline answer for satisfaction questions: Satisfied + Extremely Satisfied
PRODUCT_QUESTIONS = {"E9", "E10", "E11", "E12", "E13", "E14", "E21"}

AGE_BANDS = (  # (label, lowest, highest) in completed years
    ("18–25", 18, 25), ("26–35", 26, 35), ("36–45", 36, 45), ("46–55", 46, 55), ("56 and above", 56, 120),
)
INCOME_BANDS = (  # monthly household income in rupees, as in questionnaire B6
    ("₹30,001–40,000", 30_001, 40_000), ("₹40,001–50,000", 40_001, 50_000), ("₹50,001–60,000", 50_001, 60_000),
    ("₹60,001–80,000", 60_001, 80_000), ("₹80,001–1,00,000", 80_001, 100_000),
    ("₹1,00,001–1,50,000", 100_001, 150_000), ("₹1,50,001–2,00,000", 150_001, 200_000),
    ("₹2,00,001–2,50,000", 200_001, 250_000), ("More than ₹2,50,000", 250_001, 10**12),
)
# The range question (B6_1) for people who would not give an exact figure uses the questionnaire's wording.
INCOME_RANGE_LABELS = {
    "INR 30,001 to 40,000": "₹30,001–40,000", "INR 40,001 to 50,000": "₹40,001–50,000",
    "INR 50,001 to 60,000": "₹50,001–60,000", "INR 60,001 to 80,000": "₹60,001–80,000",
    "INR 80,001 to 1,00,000": "₹80,001–1,00,000", "INR 1,00,001 to 1,50,000": "₹1,00,001–1,50,000",
    "INR 1,50,001 to 2,00,000": "₹1,50,001–2,00,000", "INR 2,00,001 to 2,50,000": "₹2,00,001–2,50,000",
    "More than INR 2,50,000": "More than ₹2,50,000",
}


@dataclass(frozen=True)
class Q:
    code: str
    title: str
    theme: str  # who, think, bought, feel
    kind: str
    columns: Optional[str] = None  # pattern for the file's column codes; defaults by kind
    order: tuple = ()  # fixed answer order for scales; otherwise answers are shown largest first
    bands: tuple = ()
    wording: str = ""

    @property
    def product(self) -> bool:
        """Answers depend on the model (fuel, variant, features): options a model doesn't offer are left out,
        never shown as 0%."""
        return self.code in PRODUCT_QUESTIONS

    @property
    def rating(self) -> bool:
        return self.order == SATISFACTION

    @property
    def natural_order(self) -> bool:
        """Scales and bands keep their own order; everything else is shown largest first."""
        return bool(self.order or self.bands)

    def column_pattern(self) -> re.Pattern:
        if self.columns:
            return re.compile(self.columns)
        if self.kind == "multi":
            return re.compile(rf"^{self.code}r\d+(?:_\d+)?$")
        return re.compile(rf"^{re.escape(self.code)}$")


QUESTIONS: tuple = (
    # Who they are
    Q("B1", "Age", "who", "banded", bands=AGE_BANDS, wording="Please tell us your age in completed years."),
    Q("B2", "Gender", "who", "single"),
    Q("B6", "Monthly household income", "who", "banded", columns=r"^(B6|B6_1|noanswerB6_n1)$", bands=INCOME_BANDS,
      wording="Please let us know your monthly household income."),
    Q("B3", "Education", "who", "single"),
    Q("B4", "Marital status", "who", "single"),
    Q("B7", "Occupation", "who", "single"),
    Q("B8", "Home owned or rented", "who", "single"),
    Q("B9", "Type of home", "who", "single"),
    Q("B10", "Parking at home", "who", "single"),
    Q("D2", "Buyer type", "who", "single"),
    Q("A7", "Ownership and decision role", "who", "single"),
    Q("C1", "Also owns a two-wheeler", "who", "single"),
    # How they think
    Q("D1", "Why they needed a new car", "think", "slots", columns=r"^D1_c\d+$"),
    Q("E1", "When they started looking", "think", "single", order=(
        "Since last month from the date of purchase", "Since last 3 months from the date of purchase",
        "Since last 6 months from the date of purchase", "Since last 9 months from the date of purchase",
        "Since last 12 months (1 year) from the date of purchase",
        "Since last 15 months (1 year 3 months) from the date of purchase",
        "Since last 24 months (2 years) from the date of purchase",
        "Since last 36 months (3 years) from the date of purchase", "Others")),
    Q("E1_1", "What mattered most in a new car", "think", "multi"),
    Q("G1", "Smartphone brand", "think", "multi", columns=r"^G1r\d+c1$"),
    Q("G2", "Smart devices at home", "think", "multi"),
    # G3: the cells read "None" (never abroad); an earlier reader mistook that word for an empty cell.
    Q("G3", "Holidays abroad in the last two years", "think", "single", order=("None", "1", "2 to 4", "More than 4")),
    Q("G4", "How long they plan to keep the car", "think", "single", order=(
        "In 1 year", "In 2 years", "In 3 years", "After 4 to 5 years", "After 6 to 7 years", "After 8 Years",
        "After 9 Years", "After 10 Years", "I will keep the car till its service life", "I did not decide yet")),
    Q("G5", "Fuel for their next car", "think", "single"),
    # What they bought
    Q("E9", "Variant", "bought", "single"),
    Q("E10", "Fuel type", "bought", "single"),
    Q("E11", "Engine capacity", "bought", "single"),
    Q("E12", "Turbo or non-turbo", "bought", "single"),
    Q("E13", "Gearbox", "bought", "single"),
    Q("E14", "Seats", "bought", "single"),
    Q("E3", "Bought with a loan", "bought", "single"),
    Q("E22", "Accessories fitted", "bought", "single"),
    Q("D4", "Previous car new or used", "bought", "single"),
    Q("E15", "Why they chose it", "bought", "multi"),
    Q("E5", "Models also considered", "bought", "consider", columns=r"^(E5r\d+(?:_\d+)?c\d|E5_1r1)$"),
    Q("E7", "Why they rejected the models they considered", "bought", "reject", columns=r"^E7r\d+(?:_\d+)?c\d$"),
    Q("E2r1c1", "Budget they had in mind", "bought", "number"),
    Q("E2r2c1", "On-road price paid", "bought", "number"),
    Q("E4c1", "Down payment", "bought", "number"),
    Q("E4c2", "Monthly EMI", "bought", "number"),
    # How they feel
    Q("F1", "Satisfaction with the car", "feel", "single", order=SATISFACTION),
    Q("F2", "Main reason for top satisfaction with the car", "feel", "single"),
    Q("F3", "Main reason the car didn't get 5 out of 5", "feel", "single"),
    Q("F4", "Satisfaction with the dealer", "feel", "single", order=SATISFACTION),
    Q("F5", "Main reason for top satisfaction with the dealer", "feel", "single"),
    Q("F6", "Main reason the dealer didn't get 5 out of 5", "feel", "single"),
    Q("E18", "What satisfies them about the car", "feel", "multi"),
    Q("E19", "What dissatisfies them about the car", "feel", "multi"),
    Q("E20", "Words that describe the car", "feel", "multi"),
    Q("E21", "Features they like most", "feel", "multi"),
)

# Columns read for their own purpose rather than as a question
E5_NONE_COLUMN = "E5_1r1"  # "I did not consider any other model"
E5_NONE_KEY, E5_NONE_LABEL = "none", "Didn’t consider any other model"
RECORD_COLUMNS = ("record", "respid")  # the survey tool's interview number (exports name it either way)
MODEL_COLUMN = "E8"  # model purchased
MONTH_COLUMN = "E9_1"  # month of purchase, e.g. "November 2025"

NOT_SELECTED = {"not selected"}
BY_CODE = {q.code: q for q in QUESTIONS}
