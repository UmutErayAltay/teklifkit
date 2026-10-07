"""Test altyapısı.

DB testleri için bir Postgres gerekir. Bağlantı: TEKLIFKIT_TEST_ADMIN_URL (varsayılan
postgresql://postgres:postgres@localhost:5432/postgres). Oturum başına temiz bir veritabanı
yaratılır, auth taklidi + migrasyonlar uygulanır. Her test kendi kullanıcı/org'unu uuid ile
üretir, bu yüzden testler arası temizlik gerekmez.

Postgres'e ulaşılamazsa DB testleri atlanır; REQUIRE_DB=1 (CI) ise hata verir.
"""

import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.migrations import apply_migrations

ADMIN_URL = os.environ.get(
    "TEKLIFKIT_TEST_ADMIN_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
TEST_DB = "teklifkit_test"
JWT_SECRET = "test-secret-test-secret-test-secret-32b"


def _db_url() -> str:
    info = conninfo_to_dict(ADMIN_URL)
    info["dbname"] = TEST_DB
    return make_conninfo(**info)


@pytest.fixture(scope="session")
def database_url() -> str:
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3) as admin:
            admin.execute(f"drop database if exists {TEST_DB} with (force)")
            admin.execute(
                f"create database {TEST_DB} encoding 'UTF8' template template0 locale 'C.UTF-8'"
            )
    except psycopg.OperationalError as exc:
        if os.environ.get("REQUIRE_DB") == "1":
            raise
        pytest.skip(
            f"Postgres yok ({exc}); TEKLIFKIT_TEST_ADMIN_URL ayarla", allow_module_level=False
        )
    url = _db_url()
    with psycopg.connect(url) as conn:
        apply_migrations(conn, stub=True)
    return url


@pytest.fixture
def admin_conn(database_url):
    """Yüksek yetkili bağlantı (RLS'i atlar): yalnız test verisi hazırlamak/okumak için."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
def make_user(admin_conn):
    def _make(label: str = "u") -> dict:
        uid = str(uuid.uuid4())
        email = f"{label}-{uid[:8]}@example.com"
        admin_conn.execute("insert into auth.users (id, email) values (%s, %s)", [uid, email])
        return {"sub": uid, "email": email, "role": "authenticated", "aud": "authenticated"}

    return _make


@pytest.fixture
def db_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.delenv("JWKS_URL", raising=False)
    monkeypatch.setenv("DEV_LOGIN", "0")
    return database_url
