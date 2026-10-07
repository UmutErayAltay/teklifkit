from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from app.domain.status import TRANSITIONS, can_transition, display_status, manual_actions


def test_quote_flow():
    assert can_transition("quote", "draft", "sent")
    assert can_transition("quote", "sent", "accepted")
    assert can_transition("quote", "sent", "rejected")


@pytest.mark.parametrize(
    ("kind", "old", "new"),
    [
        ("quote", "draft", "accepted"),  # göndermeden kabul olmaz
        ("quote", "accepted", "sent"),
        ("quote", "rejected", "accepted"),
        ("quote", "sent", "draft"),
        ("invoice", "draft", "paid"),  # ödeme kaydı olmadan ödendi olmaz
        ("invoice", "sent", "draft"),
        ("invoice", "paid", "draft"),
        ("invoice", "sent", "accepted"),  # teklif durumu faturada geçersiz
        ("receipt", "draft", "sent"),
    ],
)
def test_forbidden_transitions(kind, old, new):
    assert not can_transition(kind, old, new)


def test_invoice_paid_and_back_only_via_payments():
    assert can_transition("invoice", "sent", "paid")
    assert can_transition("invoice", "paid", "sent")
    # ...ama düğmeyle yapılamaz
    assert manual_actions("invoice", "sent") == []
    assert manual_actions("invoice", "paid") == []


def test_manual_actions():
    assert [t for t, _ in manual_actions("quote", "draft")] == ["sent"]
    assert sorted(t for t, _ in manual_actions("quote", "sent")) == ["accepted", "rejected"]
    assert manual_actions("quote", "accepted") == []
    assert [t for t, _ in manual_actions("invoice", "draft")] == ["sent"]


def test_manual_actions_are_valid_transitions():
    for kind, states in TRANSITIONS.items():
        for status in states:
            for to, _ in manual_actions(kind, status):
                assert can_transition(kind, status, to)


def test_overdue_is_derived_not_stored():
    today = date(2026, 10, 7)
    yesterday, tomorrow = today - timedelta(days=1), today + timedelta(days=1)
    assert display_status("invoice", "sent", yesterday, D("10"), today) == "overdue"
    assert (
        display_status("invoice", "sent", today, D("10"), today) == "sent"
    )  # vade günü geçmiş değil
    assert display_status("invoice", "sent", tomorrow, D("10"), today) == "sent"
    assert display_status("invoice", "sent", yesterday, D("0"), today) == "sent"  # bakiye yok
    assert display_status("invoice", "paid", yesterday, D("0"), today) == "paid"
    assert display_status("invoice", "sent", None, D("10"), today) == "sent"
    assert display_status("quote", "sent", yesterday, D("10"), today) == "sent"
