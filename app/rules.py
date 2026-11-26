"""The fixed rules of BuyerLenz. Every part of the server reads them from here."""

MIN_SHOW = 10  # fewer buyers than this: hidden, and the exact count is never revealed
MIN_SOLID = 30  # fewer than this: amber "use with caution", no market comparison
FLAT = 3  # differences of this many points or less are shown in grey (within noise)

SURVEY_START = (2025, 1)  # (year, month): purchases before January 2025 are left out
EXPECTED_START = (2025, 7)  # nothing should be earlier than July 2025: kept, but flagged in the upload report
ROLLBACK_HOURS = 48  # a published upload can be undone for this long
MAX_UPLOAD_MB = 400  # a 49,000-buyer survey export is about 220 MB

MAX_CUT_QUESTIONS = 5  # a data cut holds at most this many questions...
MAX_CUT_COLUMNS = 25  # ...and this many columns (brands, models), so no single export rebuilds the survey
MAX_TREND_LINES = 8
EXPORT_LIMIT = 20  # Copy to Excel per user per calendar month (India time), resetting on the 1st

# "Worth a look" on the admin activity log (looking back over the last WATCH_DAYS days)
WATCH_DAYS = 7
WATCH_EXPORTS_MONTH = 15  # copies this month: close to the allowance
WATCH_EXPORTS_DAY = 8  # copies in a single day
WATCH_REPLACED = 3  # sign-ins that pushed another device off: a login may be shared
WATCH_PLACES_DAY = 3  # different internet addresses in one day
WATCH_FAILED = 5  # wrong passwords for one login
WATCH_FAILED_ADDRESS = 10  # failed sign-ins from one internet address: someone may be guessing


def sample_status(n: int) -> str:
    return "hidden" if n < MIN_SHOW else "caution" if n < MIN_SOLID else "ok"
