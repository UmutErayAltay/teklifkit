"""Ortak web yardımcıları: şablon motoru, filtreler, aktif org çözümü."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE / "templates")

SYMBOLS = {"TRY": "₺", "EUR": "€", "USD": "$"}
STATUS_TR = {
    "draft": "taslak",
    "sent": "gönderildi",
    "accepted": "kabul edildi",
    "rejected": "reddedildi",
    "paid": "ödendi",
    "overdue": "vadesi geçti",
}
KIND_TR = {"quote": "Teklif", "invoice": "Fatura"}
KIND_PREFIX = {"quote": "TKL", "invoice": "FTR"}


class NoOrg(Exception):
    """Kullanıcının hiç organizasyonu yok; /onboarding'e yönlendirilir."""


def money(value: Decimal | int | float | str, currency: str = "TRY") -> str:
    text = f"{Decimal(value):,.2f}"  # 1,234.56
    text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return f"{text} {SYMBOLS.get(currency, currency)}"


def tr_number(value: Decimal | int | float | str) -> str:
    """Para birimi simgesiz Türkçe sayı: 15200 → '15.200,00' (form alanları için)."""
    return money(value, "").strip()


def doc_no(kind: str, number: int) -> str:
    return f"{KIND_PREFIX[kind]}-{number:04d}"


def tr_date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def qty_fmt(value: Decimal) -> str:
    """2.000 → '2', 1.500 → '1,5'."""
    text = format(Decimal(value).normalize(), "f")
    return text.replace(".", ",")


templates.env.filters.update(
    money=money,
    qty_fmt=qty_fmt,
    tr_number=tr_number,
    doc_no=lambda d: doc_no(d["kind"], d["number"]),
    doc_prefix=lambda kind: KIND_PREFIX[kind],
    tr_date=tr_date,
    status_tr=lambda s: STATUS_TR.get(s, s),
)
templates.env.globals.update(KIND_TR=KIND_TR, VAT_RATES=(0, 1, 10, 20))


def active_org(conn: psycopg.Connection) -> str:
    """Kullanıcının (RLS'in gösterdiği) ilk organizasyonu; M1'de tek aktif org."""
    row = conn.execute(
        "select m.org_id, o.name from public.memberships m "
        "join public.organizations o on o.id = m.org_id "
        "order by m.created_at limit 1"
    ).fetchone()
    if row is None:
        raise NoOrg
    return str(row["org_id"])
