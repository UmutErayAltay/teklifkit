"""Basit migrasyon çalıştırıcı: `python -m app.migrations [--stub]`.

--stub: Supabase olmayan ortamlar (yerel/CI) için auth şemasının taklidini önce yükler.
"""

import sys
from pathlib import Path

import psycopg

from app.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
STUB_SQL = ROOT / "db" / "test_auth_stub.sql"


def apply_migrations(conn: psycopg.Connection, *, stub: bool = False) -> list[str]:
    """Uygulanmamış migrasyonları sırayla çalıştırır; uygulananların adını döndürür."""
    if stub:
        conn.execute(STUB_SQL.read_text(encoding="utf-8"))
        conn.commit()
    conn.execute("create schema if not exists private")
    conn.execute("revoke all on schema private from public")
    conn.execute(
        "create table if not exists private.schema_migrations "
        "(version text primary key, applied_at timestamptz not null default now())"
    )
    conn.commit()
    done = {r[0] for r in conn.execute("select version from private.schema_migrations")}
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        conn.execute(path.read_text(encoding="utf-8"))
        conn.execute("insert into private.schema_migrations (version) values (%s)", [path.name])
        conn.commit()
        applied.append(path.name)
    return applied


def main(argv: list[str]) -> int:
    with psycopg.connect(get_settings().database_url, prepare_threshold=None) as conn:
        applied = apply_migrations(conn, stub="--stub" in argv)
    print("uygulandı:", ", ".join(applied) if applied else "(yeni migrasyon yok)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
