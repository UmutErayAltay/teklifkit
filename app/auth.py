"""JWT doğrulama ve oturum çerezi.

Oturum, Supabase Auth'un verdiği access token'ın httpOnly çerezde taşınmasıdır; backend
her istekte imzayı doğrular ve claim'leri RLS bağlamına (app/db.py) aktarır.
"""

import time
import uuid
from dataclasses import dataclass
from functools import lru_cache

import httpx
import jwt
from fastapi import Request
from jwt import PyJWKClient

from app.config import get_settings

COOKIE = "tk_session"
AUDIENCE = "authenticated"


class AuthError(Exception):
    pass


class LoginRequired(Exception):
    """Route'larda giriş yoksa fırlatılır; main.py /login'e yönlendirir."""


@dataclass(frozen=True)
class User:
    id: str
    email: str | None
    claims: dict


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_keys=True)


def verify_token(token: str) -> dict:
    s = get_settings()
    options = {"require": ["exp", "sub"]}
    try:
        if s.jwks_url:
            key = _jwks_client(s.jwks_url).get_signing_key_from_jwt(token).key
            return jwt.decode(
                token, key, algorithms=["ES256", "RS256"], audience=AUDIENCE, options=options
            )
        if s.jwt_secret:
            return jwt.decode(
                token, s.jwt_secret, algorithms=["HS256"], audience=AUDIENCE, options=options
            )
    except jwt.PyJWTError as exc:
        raise AuthError(str(exc)) from exc
    raise AuthError("JWT doğrulaması yapılandırılmamış (JWKS_URL veya JWT_SECRET)")


def current_user(request: Request) -> User:
    token = request.cookies.get(COOKIE)
    if not token:
        raise LoginRequired
    try:
        claims = verify_token(token)
        uuid.UUID(claims["sub"])
    except (AuthError, ValueError) as exc:
        raise LoginRequired from exc
    return User(id=claims["sub"], email=claims.get("email"), claims=claims)


def password_login(email: str, password: str) -> str:
    """Supabase Auth'a e-posta/parola ile giriş; access token döndürür."""
    s = get_settings()
    if not (s.supabase_url and s.supabase_anon_key):
        raise AuthError("SUPABASE_URL / SUPABASE_ANON_KEY yapılandırılmamış")
    resp = httpx.post(
        f"{s.supabase_url}/auth/v1/token",
        params={"grant_type": "password"},
        headers={"apikey": s.supabase_anon_key},
        json={"email": email, "password": password},
        timeout=10,
    )
    if resp.status_code != 200:
        raise AuthError("E-posta veya parola hatalı")
    return resp.json()["access_token"]


def mint_dev_token(user_id: str, email: str, ttl: int = 3600) -> str:
    """Yalnızca DEV_LOGIN=1 iken ve JWT_SECRET ile: yerelde Supabase olmadan oturum."""
    secret = get_settings().jwt_secret
    if not secret:
        raise AuthError("JWT_SECRET gerekli")
    now = int(time.time())
    claims = {
        "sub": user_id,
        "email": email,
        "aud": AUDIENCE,
        "role": AUDIENCE,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(claims, secret, algorithm="HS256")
