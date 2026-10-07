"""Salt-okunur `viewer` rolü: okur, hiçbir yoldan yazamaz (RLS ile garanti)."""

import psycopg
import pytest

from app.db import user_db
from app.seed import seed_demo

pytestmark = pytest.mark.usefixtures("db_env")

WRITE_STATEMENTS = {
    "clients insert": ("insert into public.clients (org_id, name) values (%(org)s, 'x')", {}),
    "clients update": ("update public.clients set name = 'ele geçirildi'", {}),
    "documents insert": (
        "insert into public.documents (org_id, kind, number) values (%(org)s, 'quote', 9999)",
        {},
    ),
    "documents update": ("update public.documents set notes = 'x'", {}),
    "items update": ("update public.document_items set qty = 99", {}),
    "payments insert": (
        "insert into public.payments (org_id, document_id, amount) "
        "select org_id, id, 1 from public.documents where kind = 'invoice' and status = 'sent' "
        "limit 1",
        {},
    ),
    "counter bump": ("select public.next_document_number(%(org)s, 'quote')", {}),
    "portal token": (
        "insert into public.portal_tokens (token_hash, org_id, document_id, expires_at) "
        "select repeat('a', 64), org_id, id, now() + interval '1 day' "
        "from public.documents limit 1",
        {},
    ),
    "org rename": ("update public.organizations set name = 'ele geçirildi'", {}),
}


@pytest.fixture
def demo(make_user, admin_conn):
    viewer = make_user("viewer")
    org = seed_demo(admin_conn, viewer["sub"])
    return {"claims": viewer, "org": org}


def test_viewer_can_read_everything_in_its_org(demo):
    with user_db(demo["claims"]) as conn:
        counts = {
            t: conn.execute(f"select count(*) as n from public.{t}").fetchone()["n"]
            for t in ("clients", "documents", "document_items", "payments")
        }
    assert counts["clients"] == 3 and counts["documents"] == 8
    assert counts["document_items"] > 8 and counts["payments"] == 2


@pytest.mark.parametrize("name", sorted(WRITE_STATEMENTS))
def test_viewer_cannot_write_anything(demo, name):
    sql, _ = WRITE_STATEMENTS[name]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), user_db(demo["claims"]) as conn:
        cur = conn.execute(sql, {"org": demo["org"]})
        # UPDATE/DELETE RLS ile sessizce 0 satıra düşer; yazma yetkisi yoksa bunu da hata say.
        if cur.rowcount == 0 and sql.startswith(("update", "delete")):
            raise psycopg.errors.InsufficientPrivilege("0 satır (RLS)")


def test_viewer_delete_affects_nothing(demo):
    with user_db(demo["claims"]) as conn:
        for table in ("payments", "document_items", "documents", "clients"):
            assert conn.execute(f"delete from public.{table}").rowcount == 0, table
        assert conn.execute("select count(*) as n from public.clients").fetchone()["n"] == 3


def test_can_write_matrix(make_user, demo, admin_conn):
    owner = make_user("owner")
    with user_db(owner) as conn:
        own_org = conn.execute("select public.create_organization('O') as id").fetchone()["id"]
        assert conn.execute("select public.can_write(%s) as w", [own_org]).fetchone()["w"]
        # yabancı org: üye değil → yazamaz
        assert not conn.execute("select public.can_write(%s) as w", [demo["org"]]).fetchone()["w"]
    with user_db(demo["claims"]) as conn:
        assert not conn.execute("select public.can_write(%s) as w", [demo["org"]]).fetchone()["w"]
        assert conn.execute("select public.is_member(%s) as m", [demo["org"]]).fetchone()["m"]
    admin_conn.execute(
        "update public.memberships set role = 'member' where org_id = %s and user_id = %s",
        [demo["org"], demo["claims"]["sub"]],
    )
    with user_db(demo["claims"]) as conn:  # rol yükselince yazabilir
        assert conn.execute("select public.can_write(%s) as w", [demo["org"]]).fetchone()["w"]


def test_unknown_role_is_rejected_by_constraint(demo, admin_conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        admin_conn.execute(
            "update public.memberships set role = 'admin' where org_id = %s", [demo["org"]]
        )


def test_seed_is_idempotent_and_resettable(make_user, admin_conn):
    u = make_user("seed")
    first = seed_demo(admin_conn, u["sub"])
    assert seed_demo(admin_conn, u["sub"]) == first  # ikinci çağrı dokunmaz
    rows = admin_conn.execute(
        "select count(*) from public.documents where org_id = %s", [first]
    ).fetchone()
    assert rows[0] == 8
    again = seed_demo(admin_conn, u["sub"], reset=True)
    assert again != first
    assert (
        admin_conn.execute(
            "select count(*) from public.organizations where id = %s", [first]
        ).fetchone()[0]
        == 0
    )


def test_viewer_updates_change_nothing(demo, admin_conn):
    with user_db(demo["claims"]) as conn:
        for sql in (
            "update public.clients set name = 'ele geçirildi'",
            "update public.documents set notes = 'ele geçirildi'",
            "update public.document_items set qty = 99",
            "update public.organizations set name = 'ele geçirildi'",
        ):
            assert conn.execute(sql).rowcount == 0, sql
    changed = admin_conn.execute(
        "select (select count(*) from public.clients where name = 'ele geçirildi') "
        "+ (select count(*) from public.documents where notes = 'ele geçirildi') "
        "+ (select count(*) from public.document_items where qty = 99) "
        "+ (select count(*) from public.organizations where name = 'ele geçirildi')"
    ).fetchone()[0]
    assert changed == 0
