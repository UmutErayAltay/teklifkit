"""Saf tutar hesabı (veritabanına dokunmaz, birim testlidir).

Kurallar:
  * Satır net tutarı = adet × birim fiyat, kuruşa (2 hane) ROUND_HALF_UP.
  * KDV, satır bazında değil KDV oranı grubunun net toplamı üzerinden hesaplanır
    ve grup başına bir kez yuvarlanır (AB tarzı fatura toplamı).
  * Genel toplam = net toplam + KDV toplamı.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
VAT_RATES = (0, 1, 10, 20)


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Item:
    qty: Decimal
    unit_price: Decimal
    vat_rate: int


@dataclass(frozen=True)
class Totals:
    net: Decimal
    vat_by_rate: dict[int, Decimal]  # yalnız net'i olan oranlar, orana göre sıralı
    vat: Decimal
    gross: Decimal


def line_net(item: Item) -> Decimal:
    return q2(item.qty * item.unit_price)


def document_totals(items: list[Item]) -> Totals:
    net_by_rate: dict[int, Decimal] = {}
    for item in items:
        net_by_rate[item.vat_rate] = net_by_rate.get(item.vat_rate, Decimal(0)) + line_net(item)
    vat_by_rate = {
        rate: q2(net * rate / 100) for rate, net in sorted(net_by_rate.items()) if rate > 0
    }
    net = sum(net_by_rate.values(), Decimal(0))
    vat = sum(vat_by_rate.values(), Decimal(0))
    return Totals(net=q2(net), vat_by_rate=vat_by_rate, vat=q2(vat), gross=q2(net + vat))
