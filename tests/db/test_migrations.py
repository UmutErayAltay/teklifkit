import psycopg

from app.migrations import MIGRATIONS_DIR, apply_migrations


def test_migrations_are_idempotent_and_ordered(database_url):
    with psycopg.connect(database_url) as conn:
        assert apply_migrations(conn) == []  # fixture hepsini zaten uyguladı
        applied = [r[0] for r in conn.execute("select version from private.schema_migrations")]
    on_disk = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert sorted(applied) == on_disk
    assert on_disk == sorted(on_disk) and on_disk[0].startswith("0001_")
