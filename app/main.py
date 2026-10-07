from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.auth import LoginRequired
from app.routes import auth, clients, dashboard, documents, portal, workflow
from app.web import BASE, NoOrg

app = FastAPI(title="teklifkit", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Çerez tabanlı oturumda CSRF savunması: tarayıcı cross-site yazma isteği ise reddet."""
    if request.method in UNSAFE and request.headers.get("sec-fetch-site") == "cross-site":
        return Response("Cross-site istek reddedildi", status_code=403)
    response = await call_next(request)
    if request.url.path.startswith(
        "/p/"
    ):  # token URL'leri: önbelleğe, referrer'a, indekse sızmasın
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _redirect(request: Request, to: str) -> Response:
    if request.headers.get("hx-request"):  # HTMX isteğinde tam sayfa yönlendirmesi
        return Response(status_code=401, headers={"HX-Redirect": to})
    return RedirectResponse(to, status_code=303)


@app.exception_handler(LoginRequired)
async def login_required_handler(request: Request, _: LoginRequired):
    return _redirect(request, "/login")


@app.exception_handler(NoOrg)
async def no_org_handler(request: Request, _: NoOrg):
    return _redirect(request, "/onboarding")


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/dashboard", status_code=303)


app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(clients.router)
app.include_router(documents.router)  # /documents/new, /row: workflow'daki /{doc_id}'den önce
app.include_router(workflow.router)
app.include_router(portal.router)
