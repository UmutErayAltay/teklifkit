from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.auth import User, current_user
from app.db import user_db
from app.web import active_org, templates

router = APIRouter(prefix="/clients")


@router.get("", response_class=HTMLResponse)
def list_clients(request: Request, user: User = Depends(current_user)):
    with user_db(user.claims) as conn:
        active_org(conn)
        clients = conn.execute("select * from public.clients order by name").fetchall()
    return templates.TemplateResponse(request, "clients.html", {"user": user, "clients": clients})


@router.post("", response_class=HTMLResponse)
def create_client(
    request: Request,
    name: str = Form(...),
    email: str = Form(""),
    address: str = Form(""),
    tax_id: str = Form(""),
    user: User = Depends(current_user),
):
    name = name.strip()
    if not name or len(name) > 200:
        raise HTTPException(status_code=422, detail="Müşteri adı 1-200 karakter olmalı.")
    with user_db(user.claims) as conn:
        org_id = active_org(conn)
        client = conn.execute(
            "insert into public.clients (org_id, name, email, address, tax_id) "
            "values (%s, %s, %s, %s, %s) returning *",
            [org_id, name, email.strip() or None, address.strip() or None, tax_id.strip() or None],
        ).fetchone()
    return templates.TemplateResponse(request, "_client_row.html", {"c": client})


@router.delete("/{client_id}", response_class=HTMLResponse)
def delete_client(client_id: UUID, user: User = Depends(current_user)):
    try:
        with user_db(user.claims) as conn:
            deleted = conn.execute("delete from public.clients where id = %s", [client_id]).rowcount
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(
            status_code=409, detail="Bu müşteriye bağlı teklif/fatura var; silinemez."
        ) from None
    if not deleted:
        raise HTTPException(status_code=404)
    return HTMLResponse("")
