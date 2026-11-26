"""Settings read from backend/.env, so nothing secret or machine-specific lives in the code."""
import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
# Hosts write the address as postgresql://…; the app talks to PostgreSQL through psycopg (version 3)
for prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(prefix):
        DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len(prefix):]

# Session cookie. COOKIE_SECURE must be "true" once the site runs on https (phase 7).
SESSION_COOKIE = "bl_session"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

# A session ends after this much inactivity, and in any case after the absolute limit.
SESSION_IDLE_HOURS = 8
SESSION_MAX_HOURS = 24

# Repeated wrong passwords lock the login for a while.
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
# Failed sign-ins from one internet address (any emails) before that address is paused for LOCKOUT_MINUTES.
MAX_FAILED_PER_ADDRESS = 20

# The automatic API pages (/docs) describe every endpoint; only switch them on for local development.
SHOW_API_DOCS = os.environ.get("SHOW_API_DOCS", "false").lower() == "true"

# Extra web addresses allowed to make changes, comma-separated (e.g. "https://buyerlenz.novusinsights.com").
# The app's own address is always allowed; this is for hosting setups that rewrite it on the way in.
ALLOWED_ORIGINS = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}

# Production mode: the back end also serves the built screens (frontend/dist), so the whole app is one address.
FRONTEND_DIST = os.environ.get("FRONTEND_DIST", "")

# Largest request body accepted, apart from file uploads (which have their own limit).
MAX_REQUEST_KB = 256
