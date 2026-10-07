"""RLS'e tabi bağlantı üzerinde ortak sorgular (conn: app.db.user_db'den)."""

from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

from app.domain.totals import Item, Totals, document_totals
from app.views import DocView, totals_of, with_line_nets


def totals_for(conn: psycopg.Connection, doc_ids: list[UUID]) -> dict[UUID, Totals]:
    """Verilen belgelerin toplamları; kalemsiz belge sıfır toplam alır."""
    by_doc: dict[UUID, list[Item]] = {i: [] for i in doc_ids}
    if doc_ids:
        rows = conn.execute(
            "select document_id, qty, unit_price, vat_rate from public.document_items "
            "where document_id = any(%s)",
            [doc_ids],
        )
        for r in rows:
            by_doc[r["document_id"]].append(Item(r["qty"], r["unit_price"], r["vat_rate"]))
    return {i: document_totals(items) for i, items in by_doc.items()}


def paid_for(conn: psycopg.Connection, doc_ids: list[UUID]) -> dict[UUID, Decimal]:
    paid = {i: Decimal("0.00") for i in doc_ids}
    if doc_ids:
        rows = conn.execute(
            "select document_id, sum(amount) as total from public.payments "
            "where document_id = any(%s) group by document_id",
            [doc_ids],
        )
        for r in rows:
            paid[r["document_id"]] = r["total"]
    return paid


def load_document(conn: psycopg.Connection, doc_id: UUID, *, lock: bool = False) -> DocView | None:
    """Belge + müşteri + kalemler + ödemeler. RLS yüzünden başka org'un belgesi None döner.
    lock=True: belge satırını FOR UPDATE ile kilitler (ödeme/dönüştürme yarışlarına karşı)."""
    doc = conn.execute(
        "select d.*, o.name as org_name from public.documents d "
        "join public.organizations o on o.id = d.org_id "
        "where d.id = %s" + (" for update of d" if lock else ""),
        [doc_id],
    ).fetchone()
    if doc is None:
        return None
    client = (
        conn.execute(
            "select id, name, email, address, tax_id from public.clients where id = %s",
            [doc["client_id"]],
        ).fetchone()
        if doc["client_id"]
        else None
    )
    items = with_line_nets(
        conn.execute(
            "select description, qty, unit_price, vat_rate from public.document_items "
            "where document_id = %s order by position",
            [doc_id],
        ).fetchall()
    )
    payments = conn.execute(
        "select id, amount, paid_on, method, note from public.payments "
        "where document_id = %s order by paid_on, created_at",
        [doc_id],
    ).fetchall()
    view = DocView(
        id=str(doc["id"]),
        kind=doc["kind"],
        number=doc["number"],
        status=doc["status"],
        currency=doc["currency"],
        issue_date=doc["issue_date"],
        due_date=doc["due_date"],
        notes=doc["notes"],
        org_name=doc["org_name"],
        client=client,
        items=items,
        totals=totals_of(items),
        paid=sum((p["amount"] for p in payments), Decimal("0.00")),
    )
    view.extra = {
        "org_id": doc["org_id"],
        "payments": payments,
        "converted_from_id": doc["converted_from_id"],
    }
    return view


def related(conn: psycopg.Connection, view: DocView) -> dict[str, Any]:
    """Dönüşüm bağlantıları ve aktif portal bağlantısı sayısı (detay sayfası için)."""
    converted_to = conn.execute(
        "select id, kind, number from public.documents where converted_from_id = %s", [view.id]
    ).fetchone()
    converted_from = (
        conn.execute(
            "select id, kind, number from public.documents where id = %s",
            [view.extra["converted_from_id"]],
        ).fetchone()
        if view.extra["converted_from_id"]
        else None
    )
    active_links = conn.execute(
        "select count(*) as n from public.portal_tokens "
        "where document_id = %s and revoked_at is null and expires_at > now()",
        [view.id],
    ).fetchone()["n"]
    return {
        "converted_to": converted_to,
        "converted_from": converted_from,
        "active_links": active_links,
    }
