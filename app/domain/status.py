"""Belge durum makinesi (uygulama tarafı; asıl garanti DB'deki documents_guard tetikleyicisi).

Teklif : taslak → gönderildi → kabul edildi | reddedildi
Fatura : taslak → gönderildi ⇄ ödendi   (ödendi/gönderildi geçişi yalnız ödeme kaydıyla)
"vadesi geçti" saklanmaz, hesaplanır: gönderilmiş, bakiyesi olan ve vadesi geçmiş fatura.
"""

from datetime import date
from decimal import Decimal

TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "quote": {
        "draft": {"sent"},
        "sent": {"accepted", "rejected"},
        "accepted": set(),
        "rejected": set(),
    },
    "invoice": {
        "draft": {"sent"},
        "sent": {"paid"},
        "paid": {"sent"},
    },
}

# Kullanıcının düğmeyle yapabildikleri. Fatura sent↔paid geçişleri ödeme kaydından türer.
MANUAL_LABELS: dict[tuple[str, str, str], str] = {
    ("quote", "draft", "sent"): "Gönderildi olarak işaretle",
    ("quote", "sent", "accepted"): "Kabul edildi",
    ("quote", "sent", "rejected"): "Reddedildi",
    ("invoice", "draft", "sent"): "Gönderildi olarak işaretle",
}


def can_transition(kind: str, old: str, new: str) -> bool:
    return new in TRANSITIONS.get(kind, {}).get(old, set())


def manual_actions(kind: str, status: str) -> list[tuple[str, str]]:
    """[(hedef durum, düğme etiketi)]"""
    return [
        (to, label) for (k, frm, to), label in MANUAL_LABELS.items() if k == kind and frm == status
    ]


def display_status(
    kind: str,
    status: str,
    due_date: date | None,
    balance: Decimal,
    today: date | None = None,
) -> str:
    today = today or date.today()
    if kind == "invoice" and status == "sent" and due_date and due_date < today and balance > 0:
        return "overdue"
    return status
