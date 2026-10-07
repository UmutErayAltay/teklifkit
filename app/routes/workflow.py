"""Belge detayı ve iş akışı: durum geçişi, faturaya dönüştürme, ödeme, portal bağlantısı, PDF."""

import hashlib
import secrets
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import User, current_user
from app.config import get_settings
from app.db import user_db
from app.domain.status import can_transition, manual_actions
from app.domain.totals import q2
from app.pdf import render_pdf
from app.queries import load_document, related
from app.routes.documents import parse_decimal
from app.views import DocView
from app.web import KIND_TR, active_org, money, require_write, templates

router = APIRouter(prefix="/documents")

INVOICE_DUE_DAYS = 14
PORTAL_LINK_DAYS = 30


def render_detail(
    request: Request,
    user: User,
    conn: psycopg.Connection,
    view: DocView,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    ctx = {
        "user": user,
        "v": view,
        "error": error,
        "actions": manual_actions(view.kind, view.status),
        "today": date.today(),
        **related(conn, view),
    }
    return templates.TemplateResponse(request, "document_detail.html", ctx, status_code=status_code)


def _load_or_404(
    conn: psycopg.Connection, doc_id: UUID, *, write: bool = False, lock: bool = False
) -> DocView:
    """write=True: yazma ucu (salt-okunur hesaba 403). lock=True: belge satırını kilitler."""
    active_org(conn)
    if write:
        require_write()
    view = load_document(conn, doc_id, lock=lock)
    if view is None:
        raise HTTPException(status_code=404)
    return view


def _redirect_to(doc_id: UUID | str) -> RedirectResponse:
    return RedirectResponse(f"/documents/{doc_id}", status_code=303)


@router.get("/{doc_id}", response_class=HTMLResponse)
def show_document(request: Request, doc_id: UUID, user: User = Depends(current_user)):
    with user_db(user.claims) as conn:
        view = _load_or_404(conn, doc_id)
        return render_detail(request, user, conn, view)


@router.post("/{doc_id}/status", response_class=HTMLResponse)
def change_status(
    request: Request, doc_id: UUID, to: str = Form(...), user: User = Depends(current_user)
):
    with user_db(user.claims) as conn:
        view = _load_or_404(conn, doc_id, write=True, lock=True)
        allowed = {t for t, _ in manual_actions(view.kind, view.status)}
        if to not in allowed or not can_transition(view.kind, view.status, to):
            return render_detail(
                request, user, conn, view, error="Bu durum geçişi yapılamaz.", status_code=409
            )
        conn.execute("update public.documents set status = %s where id = %s", [to, doc_id])
    return _redirect_to(doc_id)


@router.post("/{doc_id}/convert", response_class=HTMLResponse)
def convert_to_invoice(request: Request, doc_id: UUID, user: User = Depends(current_user)):
    """Kabul edilmiş teklifi taslak faturaya dönüştürür (kalemler kopyalanır). İdempotent."""
    with user_db(user.claims) as conn:
        quote = _load_or_404(
            conn, doc_id, write=True, lock=True
        )  # kilit: çift tıklama yarışını serileştirir
        existing = related(conn, quote)["converted_to"]
        if existing:
            return _redirect_to(existing["id"])
        if quote.kind != "quote" or quote.status != "accepted":
            return render_detail(
                request,
                user,
                conn,
                quote,
                error="Yalnızca kabul edilmiş teklif faturaya dönüştürülebilir.",
                status_code=409,
            )
        org_id = quote.extra["org_id"]
        number = conn.execute(
            "select public.next_document_number(%s, 'invoice') as n", [org_id]
        ).fetchone()["n"]
        invoice_id = conn.execute(
            "insert into public.documents (org_id, kind, number, client_id, currency, due_date, "
            "notes, converted_from_id) "
            "select org_id, 'invoice', %s, client_id, currency, %s, notes, id "
            "from public.documents where id = %s returning id",
            [number, date.today() + timedelta(days=INVOICE_DUE_DAYS), doc_id],
        ).fetchone()["id"]
        conn.execute(
            "insert into public.document_items "
            "(org_id, document_id, position, description, qty, unit_price, vat_rate) "
            "select org_id, %s, position, description, qty, unit_price, vat_rate "
            "from public.document_items where document_id = %s",
            [invoice_id, doc_id],
        )
    return _redirect_to(invoice_id)


@router.post("/{doc_id}/payments", response_class=HTMLResponse)
def add_payment(
    request: Request,
    doc_id: UUID,
    amount: str = Form(...),
    paid_on: str = Form(""),
    method: str = Form(""),
    note: str = Form(""),
    user: User = Depends(current_user),
):
    with user_db(user.claims) as conn:
        view = _load_or_404(
            conn, doc_id, write=True, lock=True
        )  # kilit: eşzamanlı ödemeler fazla ödeyemez

        def fail(message: str, code: int = 422) -> HTMLResponse:
            return render_detail(request, user, conn, view, error=message, status_code=code)

        if view.kind != "invoice" or view.status != "sent":
            return fail("Yalnızca gönderilmiş faturaya ödeme eklenebilir.", 409)
        try:
            value = q2(parse_decimal(amount))
            paid_date = date.fromisoformat(paid_on) if paid_on else date.today()
        except ValueError:
            return fail("Tutar veya tarih geçersiz.")
        if value <= 0:
            return fail("Ödeme tutarı 0'dan büyük olmalı.")
        if value > view.balance:
            return fail(f"Ödeme kalan bakiyeyi ({money(view.balance, view.currency)}) aşamaz.")
        conn.execute(
            "insert into public.payments (org_id, document_id, amount, paid_on, method, note) "
            "values (%s, %s, %s, %s, %s, %s)",
            [
                view.extra["org_id"],
                doc_id,
                value,
                paid_date,
                method.strip() or None,
                note.strip() or None,
            ],
        )
        if view.balance - value == Decimal("0.00"):
            conn.execute("update public.documents set status = 'paid' where id = %s", [doc_id])
    return _redirect_to(doc_id)


@router.post("/{doc_id}/payments/{payment_id}/delete", response_class=HTMLResponse)
def delete_payment(doc_id: UUID, payment_id: UUID, user: User = Depends(current_user)):
    with user_db(user.claims) as conn:
        view = _load_or_404(conn, doc_id, write=True, lock=True)
        removed = conn.execute(
            "delete from public.payments where id = %s and document_id = %s",
            [payment_id, doc_id],
        ).rowcount
        if not removed:
            raise HTTPException(status_code=404)
        paid_after = sum(
            (p["amount"] for p in view.extra["payments"] if p["id"] != payment_id), Decimal("0.00")
        )
        if view.status == "paid" and view.totals.gross - paid_after > 0:
            conn.execute("update public.documents set status = 'sent' where id = %s", [doc_id])
    return _redirect_to(doc_id)


@router.post("/{doc_id}/portal-link", response_class=HTMLResponse)
def create_portal_link(request: Request, doc_id: UUID, user: User = Depends(current_user)):
    """Müşteri bağlantısı üretir. Token yalnızca bu yanıtta görünür; DB'de yalnız SHA-256 özeti."""
    with user_db(user.claims) as conn:
        view = _load_or_404(conn, doc_id, write=True)
        if view.status == "draft":
            raise HTTPException(status_code=409, detail="Taslak belge müşteriyle paylaşılamaz.")
        token = secrets.token_urlsafe(32)
        conn.execute(
            "insert into public.portal_tokens (token_hash, org_id, document_id, expires_at) "
            "values (%s, %s, %s, now() + make_interval(days => %s))",
            [
                hashlib.sha256(token.encode()).hexdigest(),
                view.extra["org_id"],
                doc_id,
                PORTAL_LINK_DAYS,
            ],
        )
    base = get_settings().public_base_url or str(request.base_url).rstrip("/")
    url = f"{base}/p/{token}"
    return templates.TemplateResponse(
        request, "_portal_link.html", {"url": url, "days": PORTAL_LINK_DAYS}
    )


@router.post("/{doc_id}/portal-links/revoke", response_class=HTMLResponse)
def revoke_portal_links(doc_id: UUID, user: User = Depends(current_user)):
    with user_db(user.claims) as conn:
        _load_or_404(conn, doc_id, write=True)
        conn.execute(
            "update public.portal_tokens set revoked_at = now() "
            "where document_id = %s and revoked_at is null",
            [doc_id],
        )
    return _redirect_to(doc_id)


def pdf_response(view: DocView) -> Response:
    html = templates.get_template("pdf.html").render(v=view, KIND_TR=KIND_TR)
    return Response(
        render_pdf(html),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{view.no}.pdf"'},
    )


@router.get("/{doc_id}/pdf")
def document_pdf(doc_id: UUID, user: User = Depends(current_user)):
    with user_db(user.claims) as conn:
        view = _load_or_404(conn, doc_id)
    return pdf_response(view)
