from decimal import Decimal as D

from app.domain.totals import Item, document_totals, line_net
from app.routes.documents import parse_decimal, validate_rows
from app.web import money, qty_fmt


def item(qty, price, vat):
    return Item(qty=D(qty), unit_price=D(price), vat_rate=vat)


def test_empty_document_is_zero():
    t = document_totals([])
    assert (t.net, t.vat, t.gross, t.vat_by_rate) == (D("0.00"), D("0.00"), D("0.00"), {})


def test_single_line_with_vat():
    t = document_totals([item("2", "100.00", 20)])
    assert t.net == D("200.00")
    assert t.vat_by_rate == {20: D("40.00")}
    assert t.gross == D("240.00")


def test_zero_rate_has_no_vat_line():
    t = document_totals([item("1", "50", 0)])
    assert t.vat_by_rate == {}
    assert t.gross == D("50.00")


def test_line_net_rounds_half_up():
    assert line_net(item("1.5", "0.33", 0)) == D("0.50")  # 0.495 → 0.50
    assert line_net(item("3", "0.335", 0)) == D("1.01")  # 1.005 → 1.01


def test_vat_is_rounded_once_per_rate_group_not_per_line():
    # 3 satır × 0.03 net, %10: satır bazlı yuvarlama 0.003→0.00 (×3 = 0.00),
    # grup bazlı 0.09 × 0.1 = 0.009 → 0.01.
    t = document_totals([item("1", "0.03", 10)] * 3)
    assert t.vat_by_rate == {10: D("0.01")}
    assert t.gross == D("0.10")


def test_mixed_rates_sorted_and_summed():
    t = document_totals([item("1", "100", 20), item("2", "10", 1), item("1", "5", 10)])
    assert list(t.vat_by_rate) == [1, 10, 20]
    assert t.net == D("125.00")
    assert t.vat == D("20.70")  # 0.20 + 0.50 + 20.00
    assert t.gross == D("145.70")


def test_parse_decimal_formats():
    assert parse_decimal("1234.56") == D("1234.56")
    assert parse_decimal("1234,56") == D("1234.56")
    assert parse_decimal("1.234,56") == D("1234.56")
    assert parse_decimal(" 7 ") == D("7")


def test_parse_decimal_rejects_garbage():
    for bad in ("abc", "NaN", "Infinity", ""):
        try:
            parse_decimal(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} reddedilmeliydi")


def row(desc="x", qty="1", price="10", vat="20"):
    return {"description": desc, "qty": qty, "unit_price": price, "vat_rate": vat}


def test_validate_rows_quantizes_to_db_precision():
    (it,) = validate_rows([row(qty="1.0005", price="9.995")])
    assert it.qty == D("1.001")
    assert it.unit_price == D("10.00")


def test_validate_rows_errors():
    for rows in (
        [],
        [row(desc="  ")],
        [row(qty="0")],
        [row(qty="-1")],
        [row(price="-5")],
        [row(vat="18")],
        [row(vat="abc")],
        [row(qty="0.0001")],
    ):
        try:
            validate_rows(rows)
        except ValueError:
            continue
        raise AssertionError(f"{rows!r} reddedilmeliydi")


def test_money_formatting_is_turkish():
    assert money(D("1234.5")) == "1.234,50 ₺"
    assert money(D("0"), "EUR") == "0,00 €"
    assert money(D("1234567.891"), "USD") == "1.234.567,89 $"


def test_qty_fmt():
    assert qty_fmt(D("2.000")) == "2"
    assert qty_fmt(D("1.500")) == "1,5"
    assert qty_fmt(D("100.000")) == "100"
