from collections import defaultdict
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import User, current_user
from app.db import user_db
from app.domain.status import display_status
from app.queries import paid_for, totals_for
from app.web import active_org, doc_no, templates

router = APIRouter()


def _bucket() -> dict:
    return {"count": 0, "amounts": defaultdict(lambda: Decimal("0.00"))}


def _add(bucket: dict, currency: str, amount: Decimal) -> None:
    bucket["count"] += 1
    bucket["amounts"][currency] += amount


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(current_user)):
    """Açık teklifler, faturalanmamış kabuller, tahsil edilmemiş ve vadesi geçmiş tutarlar.
    Para birimleri karıştırılmaz: her kutu para birimi başına ayrı toplam gösterir."""
    with user_db(user.claims) as conn:
        active_org(conn)
        docs = conn.execute(
            "select d.id, d.kind, d.number, d.status, d.currency, d.due_date, "
            "c.name as client_name from public.documents d "
            "left join public.clients c on c.id = d.client_id "
            "where d.status in ('sent', 'accepted')"
        ).fetchall()
        invoiced = {
            r["converted_from_id"]
            for r in conn.execute(
                "select converted_from_id from public.documents where converted_from_id is not null"
            )
        }
        ids = [d["id"] for d in docs]
        totals = totals_for(conn, ids)
        paid = paid_for(conn, ids)

    open_quotes, to_invoice, receivable, overdue = _bucket(), _bucket(), _bucket(), _bucket()
    overdue_rows = []
    for d in docs:
        gross = totals[d["id"]].gross
        balance = gross - paid[d["id"]]
        if d["kind"] == "quote":
            if d["status"] == "sent":
                _add(open_quotes, d["currency"], gross)
            elif d["id"] not in invoiced:
                _add(to_invoice, d["currency"], gross)
        elif d["status"] == "sent":
            _add(receivable, d["currency"], balance)
            if display_status("invoice", "sent", d["due_date"], balance) == "overdue":
                _add(overdue, d["currency"], balance)
                overdue_rows.append({**d, "no": doc_no(d["kind"], d["number"]), "balance": balance})
    overdue_rows.sort(key=lambda r: r["due_date"])
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "open_quotes": open_quotes,
            "to_invoice": to_invoice,
            "receivable": receivable,
            "overdue": overdue,
            "overdue_rows": overdue_rows[:10],
        },
    )
