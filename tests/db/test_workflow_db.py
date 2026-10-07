"""M2'nin DB garantileri: durum makinesi tetikleyicileri, değişmezlik, portal rolü."""

import hashlib
import uuid
from datetime import date

import psycopg
import pytest

from app.db import portal_db, user_db

pytestmark = pytest.mark.usefixtures("db_env")


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def world(make_user):
    """Bir kullanıcı/org, bir müşteri ve her türden belgeleri hazırlayan yardımcı."""
    claims = make_user("w")
    with user_db(claims) as conn:
        org = conn.execute("select public.create_organization('Org') as id").fetchone()["id"]
        client = conn.execute(
            "insert into public.clients (org_id, name) values (%s, 'Müşteri') returning id", [org]
        ).fetchone()["id"]

    class World:
        def __init__(self):
            self.claims, self.org, self.client = claims, org, client

        def doc(self, kind="quote", status="draft", items=1) -> str:
            """Taslak oluşturur, kalem ekler, sonra istenen duruma getirir (meşru geçişlerle)."""
            with user_db(claims) as conn:
                n = conn.execute(
                    "select public.next_document_number(%s, %s) as n", [org, kind]
                ).fetchone()["n"]
                doc = conn.execute(
                    "insert into public.documents (org_id, kind, number, client_id) "
                    "values (%s, %s, %s, %s) returning id",
                    [org, kind, n, client],
                ).fetchone()["id"]
                for pos in range(1, items + 1):
                    conn.execute(
                        "insert into public.document_items (org_id, document_id, position, "
                        "description, qty, unit_price, vat_rate) "
                        "values (%s, %s, %s, 'k', 2, 50, 20)",
                        [org, doc, pos],
                    )
                path = {
                    "draft": [],
                    "sent": ["sent"],
                    "accepted": ["sent", "accepted"],
                    "rejected": ["sent", "rejected"],
                    "paid": ["sent", "paid"],
                }[status]
                for step in path:
                    conn.execute(
                        "update public.documents set status = %s where id = %s", [step, doc]
                    )
            return doc

        def set_status(self, doc, status):
            with user_db(claims) as conn:
                conn.execute("update public.documents set status = %s where id = %s", [status, doc])

        def status(self, doc):
            with user_db(claims) as conn:
                return conn.execute(
                    "select status from public.documents where id = %s", [doc]
                ).fetchone()["status"]

        def token(self, doc, token="tok", days=30, revoked=False) -> str:
            token = f"{token}-{uuid.uuid4().hex[:12]}"  # özet global PK: her çağrı benzersiz
            with user_db(claims) as conn:
                conn.execute(
                    "insert into public.portal_tokens "
                    "(token_hash, org_id, document_id, expires_at, revoked_at) "
                    "values (%s, %s, %s, now() + make_interval(days => %s), "
                    "case when %s then now() end)",
                    [sha(token), org, doc, days, revoked],
                )
            return token

    return World()


# ------------------------------------------------------------------ durum makinesi
@pytest.mark.parametrize(
    ("kind", "path"),
    [
        ("quote", ["sent", "accepted"]),
        ("quote", ["sent", "rejected"]),
        ("invoice", ["sent", "paid", "sent"]),
    ],
)
def test_legal_transitions(world, kind, path):
    doc = world.doc(kind)
    for step in path:
        world.set_status(doc, step)
    assert world.status(doc) == path[-1]


@pytest.mark.parametrize(
    ("kind", "start", "target"),
    [
        ("quote", "draft", "accepted"),
        ("quote", "accepted", "sent"),
        ("quote", "rejected", "accepted"),
        ("quote", "sent", "draft"),
        ("invoice", "draft", "paid"),
        ("invoice", "sent", "draft"),
    ],
)
def test_illegal_transitions_are_rejected_by_the_database(world, kind, start, target):
    doc = world.doc(kind, start)
    with pytest.raises(psycopg.errors.CheckViolation, match="invalid status transition"):
        world.set_status(doc, target)
    assert world.status(doc) == start


def test_identity_columns_are_immutable(world):
    doc = world.doc("quote")
    for sql in (
        "update public.documents set number = number + 100 where id = %s",
        "update public.documents set kind = 'invoice' where id = %s",
    ):
        with pytest.raises(psycopg.errors.CheckViolation), user_db(world.claims) as conn:
            conn.execute(sql, [doc])


def test_sent_document_core_fields_are_frozen_but_due_date_and_notes_are_not(world):
    doc = world.doc("invoice", "sent")
    with user_db(world.claims) as conn:  # serbest alanlar
        conn.execute(
            "update public.documents set due_date = %s, notes = 'x' where id = %s",
            [date(2030, 1, 1), doc],
        )
    for sql in (
        "update public.documents set client_id = null where id = %s",
        "update public.documents set currency = 'EUR' where id = %s",
        "update public.documents set issue_date = issue_date - 1 where id = %s",
    ):
        with pytest.raises(psycopg.errors.CheckViolation), user_db(world.claims) as conn:
            conn.execute(sql, [doc])


def test_draft_document_core_fields_stay_editable(world):
    doc = world.doc("quote")
    with user_db(world.claims) as conn:
        conn.execute("update public.documents set currency = 'EUR' where id = %s", [doc])


# ---------------------------------------------------------------------- kalemler
def test_items_are_editable_only_while_draft(world):
    draft, sent = world.doc("quote"), world.doc("quote", "sent")
    with user_db(world.claims) as conn:
        conn.execute("update public.document_items set qty = 3 where document_id = %s", [draft])
        conn.execute("delete from public.document_items where document_id = %s", [draft])
    insert = (
        "insert into public.document_items (org_id, document_id, position, description, qty, "
        "unit_price, vat_rate) values (%s, %s, 9, 'ek', 1, 1, 0)"
    )
    for sql, args in (
        (insert, [world.org, sent]),
        ("update public.document_items set qty = 3 where document_id = %s", [sent]),
        ("delete from public.document_items where document_id = %s", [sent]),
    ):
        with pytest.raises(psycopg.errors.CheckViolation), user_db(world.claims) as conn:
            conn.execute(sql, args)


def test_conversion_copy_into_a_new_draft_is_allowed(world):
    """Dönüşüm sırasında kaynak (kabul edilmiş) teklifinden kopya kalem eklenir: yeni taslağa."""
    quote = world.doc("quote", "accepted", items=2)
    with user_db(world.claims) as conn:
        number = conn.execute(
            "select public.next_document_number(%s, 'invoice') as n", [world.org]
        ).fetchone()["n"]
        inv = conn.execute(
            "insert into public.documents (org_id, kind, number, client_id, converted_from_id) "
            "values (%s, 'invoice', %s, %s, %s) returning id",
            [world.org, number, world.client, quote],
        ).fetchone()["id"]
        copied = conn.execute(
            "insert into public.document_items (org_id, document_id, position, description, qty, "
            "unit_price, vat_rate) select org_id, %s, position, description, qty, unit_price, "
            "vat_rate from public.document_items where document_id = %s",
            [inv, quote],
        ).rowcount
    assert copied == 2


def test_one_invoice_per_quote(world):
    quote = world.doc("quote", "accepted")
    insert = (
        "insert into public.documents (org_id, kind, number, client_id, converted_from_id) "
        "values (%s, 'invoice', %s, %s, %s)"
    )
    with user_db(world.claims) as conn:
        conn.execute(insert, [world.org, 901, world.client, quote])
    with pytest.raises(psycopg.errors.UniqueViolation), user_db(world.claims) as conn:
        conn.execute(insert, [world.org, 902, world.client, quote])


# ---------------------------------------------------------------------- ödemeler
def add_payment(world, doc, amount=10):
    with user_db(world.claims) as conn:
        return conn.execute(
            "insert into public.payments (org_id, document_id, amount) values (%s, %s, %s) "
            "returning id",
            [world.org, doc, amount],
        ).fetchone()["id"]


def test_payments_only_on_sent_invoices(world):
    ok = world.doc("invoice", "sent")
    pid = add_payment(world, ok)
    for kind, status in (("invoice", "draft"), ("invoice", "paid"), ("quote", "sent")):
        with pytest.raises(psycopg.errors.CheckViolation):
            add_payment(world, world.doc(kind, status))
    # silinebilir, güncellenemez
    with pytest.raises(psycopg.errors.CheckViolation), user_db(world.claims) as conn:
        conn.execute("update public.payments set amount = 1 where id = %s", [pid])
    with user_db(world.claims) as conn:
        assert conn.execute("delete from public.payments where id = %s", [pid]).rowcount == 1


# ------------------------------------------------------------------------ portal
def test_portal_role_has_no_table_access(world):
    for sql in (
        "select * from public.documents",
        "select * from public.portal_tokens",
        "select * from public.clients",
        "select public.is_member(gen_random_uuid())",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), portal_db() as conn:
            conn.execute(sql)


def test_portal_document_returns_full_view_with_text_numbers(world):
    doc = world.doc("invoice", "sent", items=2)
    token = world.token(doc)
    add_payment(world, doc, "30.50")
    with portal_db() as conn:
        data = conn.execute("select public.portal_document(%s) as d", [sha(token)]).fetchone()["d"]
    assert data["id"] == str(doc) and data["kind"] == "invoice" and data["status"] == "sent"
    assert data["org_name"] == "Org" and data["client"]["name"] == "Müşteri"
    assert [i["description"] for i in data["items"]] == ["k", "k"]
    assert data["items"][0]["qty"] == "2.000" and data["items"][0]["unit_price"] == "50.00"
    assert data["paid"] == "30.50"


@pytest.mark.parametrize("case", ["unknown", "expired", "revoked", "draft", "short-hash"])
def test_portal_document_hides_everything_else(world, case):
    doc = world.doc("quote", "draft" if case == "draft" else "sent")
    token = world.token(doc, days=-1 if case == "expired" else 30, revoked=case == "revoked")
    lookup = {"unknown": sha("nope"), "short-hash": "abc"}.get(case, sha(token))
    with portal_db() as conn:
        found = conn.execute("select public.portal_document(%s) as d", [lookup]).fetchone()["d"]
    assert found is None


def test_portal_respond_accepts_once(world):
    doc = world.doc("quote", "sent")
    token = world.token(doc)
    with portal_db() as conn:
        first = conn.execute(
            "select public.portal_respond(%s, 'accepted') as r", [sha(token)]
        ).fetchone()["r"]
    assert first == "accepted" and world.status(doc) == "accepted"
    with portal_db() as conn:  # artık sent değil: ikinci yanıt işlemez
        second = conn.execute(
            "select public.portal_respond(%s, 'rejected') as r", [sha(token)]
        ).fetchone()["r"]
    assert second is None and world.status(doc) == "accepted"


def test_portal_respond_refuses_invoices_drafts_and_dead_tokens(world):
    cases = {
        "invoice": (world.doc("invoice", "sent"), {}),
        "draft": (world.doc("quote", "draft"), {}),
        "expired": (world.doc("quote", "sent"), {"days": -1}),
        "revoked": (world.doc("quote", "sent"), {"revoked": True}),
    }
    for name, (doc, opts) in cases.items():
        token = world.token(doc, f"t-{name}", **opts)
        with portal_db() as conn:
            res = conn.execute("select public.portal_respond(%s, 'accepted') as r", [sha(token)])
            assert res.fetchone()["r"] is None, name
        assert world.status(doc) in ("sent", "draft"), name


def test_portal_respond_rejects_unknown_decision(world):
    doc = world.doc("quote", "sent")
    token = world.token(doc)
    with pytest.raises(psycopg.errors.InvalidParameterValue), portal_db() as conn:
        conn.execute("select public.portal_respond(%s, 'paid')", [sha(token)])


def test_authenticated_and_anon_cannot_call_portal_functions(world):
    doc = world.doc("quote", "sent")
    token = world.token(doc)
    for claims in (world.claims, None):
        for sql in (
            "select public.portal_document(%s)",
            "select public.portal_respond(%s, 'accepted')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), user_db(claims) as conn:
                conn.execute(sql, [sha(token)])


def test_other_tenant_cannot_see_or_revoke_portal_tokens(world, make_user):
    doc = world.doc("quote", "sent")
    token = world.token(doc)
    intruder = make_user("x")
    with user_db(intruder) as conn:
        conn.execute("select public.create_organization('Intruder')")
        assert conn.execute("select 1 from public.portal_tokens").fetchone() is None
        revoked = conn.execute("update public.portal_tokens set revoked_at = now()").rowcount
        assert revoked == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "insert into public.portal_tokens (token_hash, org_id, document_id, expires_at) "
                "values (%s, %s, %s, now() + interval '1 day')",
                [sha("evil"), world.org, doc],
            )
    with portal_db() as conn:  # sahibin token'ı hâlâ geçerli
        assert conn.execute("select public.portal_document(%s) as d", [sha(token)]).fetchone()["d"]
