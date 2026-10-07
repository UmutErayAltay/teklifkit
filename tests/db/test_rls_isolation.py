"""RLS izolasyonu: iki kiracı, gerçek Postgres, backend'in kullandığı user_db() yoluyla."""

import uuid

import psycopg
import pytest

from app.db import user_db

pytestmark = pytest.mark.usefixtures("db_env")


@pytest.fixture
def tenants(make_user):
    """İki bağımsız kullanıcı/org; her birinde bir müşteri ve bir teklif."""
    out = {}
    for name in ("a", "b"):
        claims = make_user(name)
        with user_db(claims) as conn:
            org = conn.execute(
                "select public.create_organization(%s) as id", [f"Org {name}"]
            ).fetchone()["id"]
            client = conn.execute(
                "insert into public.clients (org_id, name) values (%s, %s) returning id",
                [org, f"Müşteri {name}"],
            ).fetchone()["id"]
            number = conn.execute(
                "select public.next_document_number(%s, 'quote') as n", [org]
            ).fetchone()["n"]
            doc = conn.execute(
                "insert into public.documents (org_id, kind, number, client_id) "
                "values (%s, 'quote', %s, %s) returning id",
                [org, number, client],
            ).fetchone()["id"]
            conn.execute(
                "insert into public.document_items "
                "(org_id, document_id, position, description, qty, unit_price, vat_rate) "
                "values (%s, %s, 1, 'kalem', 1, 100, 20)",
                [org, doc],
            )
        out[name] = {"claims": claims, "org": org, "client": client, "doc": doc}
    return out


TENANT_TABLES = ("clients", "documents", "document_items", "document_counters")


def test_each_tenant_sees_only_its_own_rows(tenants):
    a, b = tenants["a"], tenants["b"]
    for table in TENANT_TABLES:
        with user_db(a["claims"]) as conn:
            orgs = {r["org_id"] for r in conn.execute(f"select org_id from public.{table}")}
        assert orgs == {a["org"]}, table
        with user_db(b["claims"]) as conn:
            orgs = {r["org_id"] for r in conn.execute(f"select org_id from public.{table}")}
        assert orgs == {b["org"]}, table


def test_organizations_and_memberships_are_scoped(tenants):
    a, b = tenants["a"], tenants["b"]
    with user_db(a["claims"]) as conn:
        assert [r["id"] for r in conn.execute("select id from public.organizations")] == [a["org"]]
        members = conn.execute("select org_id, user_id from public.memberships").fetchall()
        assert members == [{"org_id": a["org"], "user_id": uuid.UUID(a["claims"]["sub"])}]
        # Başka org'u doğrudan id ile istese bile görmez.
        assert not conn.execute(
            "select 1 from public.organizations where id = %s", [b["org"]]
        ).fetchone()


def test_cannot_update_or_delete_other_tenants_rows(tenants):
    a, b = tenants["a"], tenants["b"]
    with user_db(b["claims"]) as conn:
        upd = conn.execute(
            "update public.clients set name = 'ele geçirildi' where id = %s", [a["client"]]
        )
        dele = conn.execute("delete from public.documents where id = %s", [a["doc"]])
        assert (upd.rowcount, dele.rowcount) == (0, 0)
    with user_db(a["claims"]) as conn:
        name = conn.execute("select name from public.clients where id = %s", [a["client"]])
        assert name.fetchone()["name"] == "Müşteri a"
        assert conn.execute("select 1 from public.documents where id = %s", [a["doc"]]).fetchone()


def test_cannot_insert_into_other_tenants_org(tenants):
    a, b = tenants["a"], tenants["b"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), user_db(b["claims"]) as conn:
        conn.execute("insert into public.clients (org_id, name) values (%s, 'x')", [a["org"]])


def test_cannot_move_own_row_into_other_tenants_org(tenants):
    a, b = tenants["a"], tenants["b"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), user_db(b["claims"]) as conn:
        conn.execute("update public.clients set org_id = %s where id = %s", [a["org"], b["client"]])


def test_cannot_join_an_org_by_inserting_membership(tenants):
    a, b = tenants["a"], tenants["b"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), user_db(b["claims"]) as conn:
        conn.execute(
            "insert into public.memberships (org_id, user_id, role) values (%s, %s, 'owner')",
            [a["org"], b["claims"]["sub"]],
        )


def test_cannot_attach_items_to_other_tenants_document(tenants):
    a, b = tenants["a"], tenants["b"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), user_db(b["claims"]) as conn:
        conn.execute(
            "insert into public.document_items "
            "(org_id, document_id, position, description, qty, unit_price, vat_rate) "
            "values (%s, %s, 9, 'sızma', 1, 1, 0)",
            [a["org"], a["doc"]],
        )


def test_cannot_burn_other_tenants_document_numbers(tenants):
    a, b = tenants["a"], tenants["b"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), user_db(b["claims"]) as conn:
        conn.execute("select public.next_document_number(%s, 'invoice')", [a["org"]])


def test_document_cannot_reference_a_client_of_another_org(tenants, make_user):
    """Aynı kullanıcı iki org'a üye olsa bile bileşik FK org'lar arası referansı engeller."""
    a = tenants["a"]
    with user_db(a["claims"]) as conn:
        other_org = conn.execute("select public.create_organization('Org a2') as id").fetchone()[
            "id"
        ]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "insert into public.documents (org_id, kind, number, client_id) "
                "values (%s, 'quote', 1, %s)",
                [other_org, a["client"]],
            )


def test_anonymous_role_has_no_access(tenants):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), user_db(None) as conn:
        conn.execute("select * from public.clients")


def test_authenticated_without_membership_sees_nothing(make_user, tenants):
    loner = make_user("loner")
    with user_db(loner) as conn:
        for table in TENANT_TABLES + ("organizations", "memberships"):
            assert conn.execute(f"select 1 from public.{table}").fetchone() is None, table


def test_create_organization_requires_authentication(db_env):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), user_db(None) as conn:
        conn.execute("select public.create_organization('hayalet')")
    # Rol authenticated ama sub yok: fonksiyonun kendi kontrolü devreye girer.
    with (
        pytest.raises(psycopg.errors.InvalidAuthorizationSpecification),
        user_db({"role": "authenticated"}) as conn,
    ):
        conn.execute("select public.create_organization('subsuz')")


def test_document_numbers_are_sequential_per_org_and_kind(tenants):
    a = tenants["a"]
    with user_db(a["claims"]) as conn:
        nums = [
            conn.execute(
                "select public.next_document_number(%s, 'quote') as n", [a["org"]]
            ).fetchone()["n"]
            for _ in range(3)
        ]
        first_invoice = conn.execute(
            "select public.next_document_number(%s, 'invoice') as n", [a["org"]]
        ).fetchone()["n"]
    assert nums == [2, 3, 4]  # fixture ilk teklif için 1'i kullandı
    assert first_invoice == 1
