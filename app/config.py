import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str | None  # HS256 (yerel/eski Supabase projeleri)
    jwks_url: str | None  # asimetrik anahtarlı Supabase projeleri
    supabase_url: str | None
    supabase_anon_key: str | None
    dev_login: bool
    cookie_secure: bool


def get_settings() -> Settings:
    """Ortam değişkenlerini her çağrıda okur (testlerde monkeypatch kolay olsun diye)."""
    env = os.environ
    dev_login = env.get("DEV_LOGIN") == "1"
    return Settings(
        database_url=env.get("DATABASE_URL", "postgresql://postgres@localhost:5432/teklifkit"),
        jwt_secret=env.get("JWT_SECRET") or None,
        jwks_url=env.get("JWKS_URL") or None,
        supabase_url=(env.get("SUPABASE_URL") or "").rstrip("/") or None,
        supabase_anon_key=env.get("SUPABASE_ANON_KEY") or None,
        dev_login=dev_login,
        cookie_secure=env.get("COOKIE_SECURE", "0" if dev_login else "1") == "1",
    )
