"""Protections applied to every request: only BuyerLenz's own pages may make changes,
oversized requests are refused, and every response carries the browser's safety settings."""
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse

from app import config
from app.rules import MAX_UPLOAD_MB

# The front end sends this header on every change. Another website can't add it without the server's
# permission (which is never given), so a hidden form or script elsewhere can't act as a signed-in user.
APP_HEADER = "x-buyerlenz"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-store",  # survey numbers and account details are never kept in caches
}

# The screens (served in production mode) may load only their own code, the Google fonts,
# and the watermark image that is drawn in the page. Nothing else runs.
PAGE_POLICY = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
               "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; "
               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

SWAGGER_POLICY = ("default-src 'none'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                  "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                  "font-src 'self' data: https://cdn.jsdelivr.net; "
                  "img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; object-src 'none'; "
                  "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def refuse(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status, headers=HEADERS)


async def protect(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and request.method not in SAFE_METHODS:
        if request.headers.get(APP_HEADER) != "1":
            return refuse(403, "not_from_app", "This request didn’t come from the BuyerLenz app.")
        origin = request.headers.get("origin")
        if origin and origin not in config.ALLOWED_ORIGINS and urlparse(origin).netloc != request.headers.get("host"):
            return refuse(403, "not_from_app", "This request came from another website.")
        limit = (MAX_UPLOAD_MB * 1024 + 64) * 1024 if path == "/api/admin/uploads" else config.MAX_REQUEST_KB * 1024
        try:
            size = int(request.headers.get("content-length") or 0)
        except ValueError:
            return refuse(400, "bad_request", "The request couldn’t be read.")
        if size > limit:
            return refuse(413, "too_large", "That request is too large.")
    response = await call_next(request)
    if path == "/api/docs":
        response.headers["Content-Security-Policy"] = SWAGGER_POLICY
    elif not path.startswith("/api/"):
        response.headers["Content-Security-Policy"] = PAGE_POLICY
        if path.startswith("/assets/"):  # file names change with every build, so these can be kept
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    for name, value in HEADERS.items():
        response.headers.setdefault(name, value)
    if config.COOKIE_SECURE:  # on https: tell browsers never to use plain http for this site
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response
