import psycopg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import COOKIE, AuthError, User, current_user, mint_dev_token, password_login
from app.config import get_settings
from app.db import user_db
from app.web import templates

router = APIRouter()


def _login_ctx() -> dict:
    s = get_settings()
    return {
        "dev_login": s.dev_login,
        "demo": bool(s.demo_email and (s.demo_password or s.dev_login)),
    }


def _session_redirect(token: str, to: str = "/dashboard") -> RedirectResponse:
    resp = RedirectResponse(to, status_code=303)
    resp.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
        max_age=3600,
        path="/",
    )
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {**_login_ctx(), "error": None})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        token = password_login(email.strip(), password)
    except AuthError as exc:
        return templates.TemplateResponse(
            request,
            "login.html",
            {**_login_ctx(), "error": str(exc)},
            status_code=401,
        )
    return _session_redirect(token)


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


def _dev_session(email: str) -> RedirectResponse:
    """auth.users'a kullanıcıyı ekleyip (auth taklidi) imzalı oturum açar. Yalnız yerel."""
    with psycopg.connect(get_settings().database_url) as conn:
        row = conn.execute(
            "insert into auth.users (email) values (%s) "
            "on conflict (email) do update set email = excluded.email returning id",
            [email],
        ).fetchone()
    return _session_redirect(mint_dev_token(str(row[0]), email))


@router.get("/dev-login")
def dev_login(email: str = "dev@example.com"):
    """Yerel geliştirme: Supabase olmadan oturum. DEV_LOGIN kapalıyken (üretim) 404."""
    if not get_settings().dev_login:
        raise HTTPException(status_code=404)
    return _dev_session(email)


@router.post("/demo-login")
def demo_login():
    """Salt-okunur demo hesabıyla tek tıkla giriş. Parola sunucuda kalır (DEMO_PASSWORD)."""
    s = get_settings()
    if not s.demo_email:
        raise HTTPException(status_code=404)
    if s.dev_login:
        return _dev_session(s.demo_email)
    if not s.demo_password:
        raise HTTPException(status_code=404)
    try:
        return _session_redirect(password_login(s.demo_email, s.demo_password))
    except AuthError:
        raise HTTPException(status_code=503, detail="Demo hesabı şu an kullanılamıyor.") from None


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_form(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(request, "onboarding.html", {"user": user, "error": None})


@router.post("/onboarding", response_class=HTMLResponse)
def onboarding(request: Request, name: str = Form(...), user: User = Depends(current_user)):
    name = name.strip()
    if not 1 <= len(name) <= 120:
        return templates.TemplateResponse(
            request,
            "onboarding.html",
            {"user": user, "error": "İşletme adı 1-120 karakter olmalı."},
            status_code=422,
        )
    with user_db(user.claims) as conn:
        conn.execute("select public.create_organization(%s)", [name])
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/healthz")
def healthz() -> Response:
    return Response("ok", media_type="text/plain")
