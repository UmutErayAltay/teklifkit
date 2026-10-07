from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import User, current_user
from app.db import user_db
from app.domain.totals import VAT_RATES, Item, document_totals, line_net, q2
from app.web import KIND_TR, active_org, templates

router = APIRouter(prefix="/documents")

CURRENCIES = ("TRY", "EUR", "USD")
BLANK_ROW = {"description": "", "qty": "1", "unit_price": "", "vat_rate": "20"}


def parse_decimal(text: str) -> Decimal:
    """'1234.56', '1234,56' ve '1.234,56' biçimlerini kabul eder."""
    text = text.strip().replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Geçersiz sayı: {text!r}") from None
    if not value.is_finite():
        raise ValueError(f"Geçersiz sayı: {text!r}")
    return value


def build_rows(
    description: list[str], qty: list[str], unit_price: list[str], vat_rate: list[str]
) -> list[dict]:
    """Form dizilerini satır sözlüklerine çevirir; tamamen boş satırları atar."""
    rows = []
    for i, desc in enumerate(description):
        row = {
            "description": desc,
            "qty": qty[i] if i < len(qty) else "1",
            "unit_price": unit_price[i] if i < len(unit_price) else "",
            "vat_rate": vat_rate[i] if i < len(vat_rate) else "20",
        }
        if row["description"].strip() or row["unit_price"].strip():
            rows.append(row)
    return rows


def validate_rows(rows: list[dict]) -> list[Item]:
    """Satırları doğrulayıp Item'a çevirir; hata varsa ValueError (Türkçe mesaj)."""
    if not rows:
        raise ValueError("En az bir kalem ekleyin.")
    items = []
    for n, row in enumerate(rows, start=1):
        if not row["description"].strip():
            raise ValueError(f"{n}. kalemin açıklaması boş.")
        # DB sütun hassasiyetine (numeric(12,3) / numeric(12,2)) şimdiden yuvarla ki
        # önizleme ile kaydedilen tutar birebir aynı olsun.
        qty = parse_decimal(row["qty"]).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        price = q2(parse_decimal(row["unit_price"] or "0"))
        if qty <= 0 or qty >= Decimal("1e9"):
            raise ValueError(f"{n}. kalemin adedi 0'dan büyük olmalı.")
        if price < 0 or price >= Decimal("1e10"):
            raise ValueError(f"{n}. kalemin fiyatı geçersiz.")
        try:
            rate = int(row["vat_rate"])
        except ValueError:
            rate = -1
        if rate not in VAT_RATES:
            raise ValueError(f"{n}. kalemin KDV oranı geçersiz.")
        items.append(Item(qty=qty, unit_price=price, vat_rate=rate))
    return items


def preview_items(rows: list[dict]) -> list[Item]:
    """Canlı toplam için: geçersiz satırları sessizce atlar."""
    items = []
    for row in rows:
        try:
            items.extend(validate_rows([row]))
        except ValueError:
            continue
    return items


def client_visible(conn, client_id: UUID) -> bool:
    """RLS yalnız çağıranın org'undaki müşteriyi gösterir."""
    return (
        conn.execute("select 1 from public.clients where id = %s", [client_id]).fetchone()
        is not None
    )


def form_context(kind: str, clients: list[dict], **extra) -> dict:
    ctx = {
        "kind": kind,
        "clients": clients,
        "currencies": CURRENCIES,
        "rows": [dict(BLANK_ROW)],
        "client_id": "",
        "currency": "TRY",
        "due_date": "",
        "notes": "",
        "error": None,
        "totals": document_totals([]),
    }
    ctx.update(extra)
    return ctx


@router.get("", response_class=HTMLResponse)
def list_documents(request: Request, user: User = Depends(current_user)):
    with user_db(user.claims) as conn:
        active_org(conn)
        docs = conn.execute(
            "select d.*, c.name as client_name from public.documents d "
            "left join public.clients c on c.id = d.client_id "
            "order by d.created_at desc limit 100"
        ).fetchall()
        ids = [d["id"] for d in docs]
        item_rows = (
            conn.execute(
                "select document_id, qty, unit_price, vat_rate from public.document_items "
                "where document_id = any(%s)",
                [ids],
            ).fetchall()
            if ids
            else []
        )
    by_doc: dict = {}
    for r in item_rows:
        by_doc.setdefault(r["document_id"], []).append(
            Item(qty=r["qty"], unit_price=r["unit_price"], vat_rate=r["vat_rate"])
        )
    for d in docs:
        d["totals"] = document_totals(by_doc.get(d["id"], []))
    return templates.TemplateResponse(request, "documents.html", {"user": user, "docs": docs})


@router.get("/new", response_class=HTMLResponse)
def new_document(request: Request, kind: str = "quote", user: User = Depends(current_user)):
    if kind not in KIND_TR:
        raise HTTPException(status_code=404)
    with user_db(user.claims) as conn:
        active_org(conn)
        clients = conn.execute("select id, name from public.clients order by name").fetchall()
    return templates.TemplateResponse(
        request, "document_form.html", {"user": user, **form_context(kind, clients)}
    )


@router.get("/row", response_class=HTMLResponse)
def new_row(request: Request, _: User = Depends(current_user)):
    return templates.TemplateResponse(request, "_item_row.html", {"row": dict(BLANK_ROW)})


@router.post("/totals", response_class=HTMLResponse)
def live_totals(
    request: Request,
    currency: str = Form("TRY"),
    description: list[str] = Form(default=[]),
    qty: list[str] = Form(default=[]),
    unit_price: list[str] = Form(default=[]),
    vat_rate: list[str] = Form(default=[]),
    _: User = Depends(current_user),
):
    rows = build_rows(description, qty, unit_price, vat_rate)
    totals = document_totals(preview_items(rows))
    currency = currency if currency in CURRENCIES else "TRY"
    return templates.TemplateResponse(
        request, "_totals.html", {"totals": totals, "currency": currency}
    )


@router.post("", response_class=HTMLResponse)
def create_document(
    request: Request,
    kind: str = Form(...),
    client_id: str = Form(""),
    currency: str = Form("TRY"),
    due_date: str = Form(""),
    notes: str = Form(""),
    description: list[str] = Form(default=[]),
    qty: list[str] = Form(default=[]),
    unit_price: list[str] = Form(default=[]),
    vat_rate: list[str] = Form(default=[]),
    user: User = Depends(current_user),
):
    if kind not in KIND_TR or currency not in CURRENCIES:
        raise HTTPException(status_code=422)
    rows = build_rows(description, qty, unit_price, vat_rate)
    error = None
    items: list[Item] = []
    due: date | None = None
    client_uuid: UUID | None = None
    try:
        items = validate_rows(rows)
    except ValueError as exc:
        error = str(exc)
    if error is None:
        try:
            due = date.fromisoformat(due_date) if due_date else None
            client_uuid = UUID(client_id) if client_id else None
        except ValueError:
            error = "Vade tarihi veya müşteri geçersiz."

    with user_db(user.claims) as conn:
        org_id = active_org(conn)
        if error is None and client_uuid and not client_visible(conn, client_uuid):
            error = "Seçilen müşteri bulunamadı."
        if error is not None:
            clients = conn.execute("select id, name from public.clients order by name").fetchall()
            ctx = form_context(
                kind,
                clients,
                rows=rows or [dict(BLANK_ROW)],
                client_id=client_id,
                currency=currency,
                due_date=due_date,
                notes=notes,
                error=error,
                totals=document_totals(preview_items(rows)),
            )
            return templates.TemplateResponse(
                request, "document_form.html", {"user": user, **ctx}, status_code=422
            )
        number = conn.execute(
            "select public.next_document_number(%s, %s) as n", [org_id, kind]
        ).fetchone()["n"]
        doc_id = conn.execute(
            "insert into public.documents "
            "(org_id, kind, number, client_id, currency, due_date, notes) "
            "values (%s, %s, %s, %s, %s, %s, %s) returning id",
            [org_id, kind, number, client_uuid, currency, due, notes.strip() or None],
        ).fetchone()["id"]
        for pos, (row, item) in enumerate(zip(rows, items, strict=True), start=1):
            conn.execute(
                "insert into public.document_items "
                "(org_id, document_id, position, description, qty, unit_price, vat_rate) "
                "values (%s, %s, %s, %s, %s, %s, %s)",
                [
                    org_id,
                    doc_id,
                    pos,
                    row["description"].strip(),
                    item.qty,
                    item.unit_price,
                    item.vat_rate,
                ],
            )
    return RedirectResponse(f"/documents/{doc_id}", status_code=303)


@router.get("/{doc_id}", response_class=HTMLResponse)
def show_document(request: Request, doc_id: UUID, user: User = Depends(current_user)):
    with user_db(user.claims) as conn:
        active_org(conn)
        doc = conn.execute(
            "select d.*, c.name as client_name, c.email as client_email, "
            "c.address as client_address, c.tax_id as client_tax_id "
            "from public.documents d left join public.clients c on c.id = d.client_id "
            "where d.id = %s",
            [doc_id],
        ).fetchone()
        if doc is None:
            raise HTTPException(status_code=404)
        items = conn.execute(
            "select * from public.document_items where document_id = %s order by position",
            [doc_id],
        ).fetchall()
    calc = [Item(qty=i["qty"], unit_price=i["unit_price"], vat_rate=i["vat_rate"]) for i in items]
    for item, c in zip(items, calc, strict=True):
        item["net"] = line_net(c)
    return templates.TemplateResponse(
        request,
        "document_detail.html",
        {"user": user, "doc": doc, "items": items, "totals": document_totals(calc)},
    )
