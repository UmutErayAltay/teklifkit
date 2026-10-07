"""İstek başına, RLS'e tabi veritabanı bağlantısı.

Backend veritabanına yüksek yetkili bir rolle bağlanır (Supabase'de `postgres`), ama her
istek kendi transaction'ında `authenticated` rolüne düşer ve JWT claim'lerini set eder.
Böylece `auth.uid()` tabanlı politikalar backend sorgularına da uygulanır: koddaki bir
`WHERE org_id = ...` unutkanlığı başka kiracının verisini sızdıramaz.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings


@contextmanager
def user_db(claims: dict | None) -> Iterator[psycopg.Connection]:
    """Tek transaction: çıkışta commit, hatada rollback. claims=None → anonim rol."""
    with psycopg.connect(get_settings().database_url, row_factory=dict_row) as conn:
        # set_config(..., is_local=true) ve SET LOCAL transaction ile sınırlıdır.
        conn.execute(
            "select set_config('request.jwt.claims', %s, true)", [json.dumps(claims or {})]
        )
        conn.execute("set local role " + ("authenticated" if claims else "anon"))
        yield conn


@contextmanager
def portal_db() -> Iterator[psycopg.Connection]:
    """Müşteri portalı: tablo yetkisi olmayan `portal` rolü, yalnız portal_*() fonksiyonları."""
    with psycopg.connect(get_settings().database_url, row_factory=dict_row) as conn:
        conn.execute("set local role portal")
        yield conn
