"""Belge görünüm modeli: oturumlu detay sayfası, müşteri portalı ve PDF aynı yapıyı kullanır."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.domain.status import display_status
from app.domain.totals import Item, Totals, document_totals, line_net
from app.web import doc_no


@dataclass
class DocView:
    id: str
    kind: str
    number: int
    status: str
    currency: str
    issue_date: date
    due_date: date | None
    notes: str | None
    org_name: str
    client: dict[str, Any] | None
    items: list[dict[str, Any]]
    totals: Totals
    paid: Decimal
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def no(self) -> str:
        return doc_no(self.kind, self.number)

    @property
    def balance(self) -> Decimal:
        return self.totals.gross - self.paid

    @property
    def display_status(self) -> str:
        return display_status(self.kind, self.status, self.due_date, self.balance)


def with_line_nets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Her kaleme 'net' alanını ekler (adet × birim fiyat, kuruşa yuvarlı)."""
    for it in items:
        it["net"] = line_net(Item(it["qty"], it["unit_price"], it["vat_rate"]))
    return items


def totals_of(items: list[dict[str, Any]]) -> Totals:
    return document_totals([Item(i["qty"], i["unit_price"], i["vat_rate"]) for i in items])


def from_portal_json(data: dict[str, Any]) -> DocView:
    """portal_document() jsonb çıktısından; sayılar metin olarak gelir (float kaybı olmasın)."""
    items = with_line_nets(
        [
            {
                "description": i["description"],
                "qty": Decimal(i["qty"]),
                "unit_price": Decimal(i["unit_price"]),
                "vat_rate": int(i["vat_rate"]),
            }
            for i in data["items"]
        ]
    )
    return DocView(
        id=data["id"],
        kind=data["kind"],
        number=data["number"],
        status=data["status"],
        currency=data["currency"],
        issue_date=date.fromisoformat(data["issue_date"]),
        due_date=date.fromisoformat(data["due_date"]) if data["due_date"] else None,
        notes=data["notes"],
        org_name=data["org_name"],
        client=data["client"],
        items=items,
        totals=totals_of(items),
        paid=Decimal(data["paid"]),
    )
