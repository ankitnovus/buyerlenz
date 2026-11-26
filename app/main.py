"""BuyerLenz back end: the server that holds the data and enforces every rule."""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.openapi.utils import get_openapi

from app import accounts_api, activity_api, admin, auth, client_api, config, cuts_api, layout_api, report_api, uploads_api
from app.db import database_ok
from app.protect import protect

docs = config.SHOW_API_DOCS
app = FastAPI(title="BuyerLenz API", docs_url="/api/docs" if docs else None, redoc_url=None,
              openapi_url="/api/openapi.json" if docs else None)
app.middleware("http")(protect)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(uploads_api.router)
app.include_router(client_api.router)
app.include_router(cuts_api.router)
app.include_router(accounts_api.router)
app.include_router(activity_api.router)
app.include_router(layout_api.router)
app.include_router(report_api.router)


def openapi_schema():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["BuyerLenzAppHeader"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-BuyerLenz",
        "description": "Enter 1 to enable browser-originated API changes in Swagger.",
    }
    for path in schema["paths"].values():
        for method, operation in path.items():
            if method.lower() not in {"get", "head", "options"}:
                operation["security"] = [{"BuyerLenzAppHeader": []}]

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = openapi_schema


@app.get("/api/health")
def health():
    """A simple check that the server and the database are running."""
    return {"status": "ok", "database": "ok" if database_ok() else "down"}


# ---------- production mode: serve the built screens ----------
if config.FRONTEND_DIST:
    DIST = Path(config.FRONTEND_DIST).resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def screens(path: str):
        """A real file (code, styles, icon) if there is one; otherwise the app page, which shows the right screen."""
        if path.startswith("api/"):
            raise HTTPException(404, detail={"code": "not_found", "message": "Not found."})
        file = (DIST / path).resolve()
        if path and file.is_file() and DIST in file.parents:  # never anything outside the build folder
            return FileResponse(file)
        return FileResponse(DIST / "index.html")
