"""Demo verisi: `python -m app.seed --email demo@... [--create-user] [--reset]`.

Kullanıcı Supabase Auth'ta önceden oluşturulmuş olmalıdır (auth.users'ta görünür); yerel
geliştirmede --create-user auth taklidine ekler. Demo kullanıcı org'a `viewer` (salt-okunur)
olarak bağlanır: veriyi gezer, değiştiremez. Veri yönetici bağlantısıyla (RLS atlanır) yazılır
ama durum makinesi tetikleyicileri yine de çalışır; yani seed meşru geçişlerle ilerler.
"""

import argparse
from datetime import date, timedelta
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings
from app.domain.totals import Item, document_totals, q2

DEMO_ORG = "Demo Atölye"

# (ad, e-posta, vergi no)
CLIENTS = [
    ("Çelik Müşavirlik Ltd.", "muhasebe@celik.example", "1234567890"),
    ("Yıldız Mimarlık", "info@yildiz.example", "9876543210"),
    ("Kaya Gıda A.Ş.", "satinalma@kaya.example", "5556667778"),
]


def _items(*rows: tuple[str, str, str, int]) -> list[dict]:
    return [
        {"description": d, "qty": Decimal(q), "unit_price": Decimal(p), "vat_rate": v}
        for d, q, p, v in rows
    ]


def _gross(items: list[dict]) -> Decimal:
    return document_totals([Item(i["qty"], i["unit_price"], i["vat_rate"]) for i in items]).gross


def _make_doc(
    cur: psycopg.Cursor,
    org: str,
    kind: str,
    client: str,
    items: list[dict],
    path: list[str],
    *,
    currency: str = "TRY",
    issued_days_ago: int = 0,
    due_in_days: int | None = None,
    notes: str | None = None,
    converted_from: str | None = None,
) -> str:
    number = cur.execute("select public.next_document_number(%s, %s) as n", [org, kind]).fetchone()[
        "n"
    ]
    doc = cur.execute(
        "insert into public.documents (org_id, kind, number, client_id, currency, issue_date, "
        "due_date, notes, converted_from_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "returning id",
        [
            org,
            kind,
            number,
            client,
            currency,
            date.today() - timedelta(days=issued_days_ago),
            date.today() + timedelta(days=due_in_days) if due_in_days is not None else None,
            notes,
            converted_from,
        ],
    ).fetchone()["id"]
    for pos, it in enumerate(items, start=1):
        cur.execute(
            "insert into public.document_items "
            "(org_id, document_id, position, description, qty, unit_price, vat_rate) "
            "values (%s, %s, %s, %s, %s, %s, %s)",
            [org, doc, pos, it["description"], it["qty"], it["unit_price"], it["vat_rate"]],
        )
    for status in path:  # yalnız meşru geçişler; tetikleyici geçersizini reddederdi
        cur.execute("update public.documents set status = %s where id = %s", [status, doc])
    return str(doc)


def _pay(cur: psycopg.Cursor, org: str, doc: str, amount: Decimal, days_ago: int, how: str):
    cur.execute(
        "insert into public.payments (org_id, document_id, amount, paid_on, method) "
        "values (%s, %s, %s, %s, %s)",
        [org, doc, amount, date.today() - timedelta(days=days_ago), how],
    )


def seed_demo(conn: psycopg.Connection, user_id: str, *, reset: bool = False) -> str:
    """Demo org kurar, id döndürür. Varsa dokunmaz; reset=True ise silip yeniden kurar."""
    cur = conn.cursor(row_factory=dict_row)  # çağıranın satır fabrikasından bağımsız
    existing = cur.execute(
        "select o.id from public.organizations o join public.memberships m on m.org_id = o.id "
        "where o.name = %s and m.user_id = %s",
        [DEMO_ORG, user_id],
    ).fetchone()
    if existing and not reset:
        return str(existing["id"])
    if existing:
        cur.execute("delete from public.organizations where id = %s", [existing["id"]])

    org = str(
        cur.execute(
            "insert into public.organizations (name) values (%s) returning id", [DEMO_ORG]
        ).fetchone()["id"]
    )
    cur.execute(
        "insert into public.memberships (org_id, user_id, role) values (%s, %s, 'viewer')",
        [org, user_id],
    )
    ids = [
        str(
            cur.execute(
                "insert into public.clients (org_id, name, email, tax_id) values (%s, %s, %s, %s) "
                "returning id",
                [org, name, email, tax],
            ).fetchone()["id"]
        )
        for name, email, tax in CLIENTS
    ]
    celik, yildiz, kaya = ids

    web = _items(
        ("Kurumsal web sitesi tasarımı", "1", "24000", 20),
        ("İçerik girişi (sayfa)", "12", "350", 20),
    )
    quote1 = _make_doc(
        cur, org, "quote", celik, web, ["sent", "accepted"], issued_days_ago=40, notes="50% peşin"
    )
    invoice1 = _make_doc(
        cur,
        org,
        "invoice",
        celik,
        web,
        ["sent"],
        issued_days_ago=30,
        due_in_days=15,
        converted_from=quote1,
    )
    _pay(cur, org, invoice1, q2(_gross(web) / 2), 25, "Havale")

    _make_doc(
        cur,
        org,
        "quote",
        yildiz,
        _items(("Mobil uygulama prototipi", "1", "38000", 20), ("UX araştırması", "3", "4500", 20)),
        ["sent"],
        issued_days_ago=6,
        notes="Teklif 30 gün geçerlidir.",
    )
    _make_doc(
        cur,
        org,
        "quote",
        kaya,
        _items(("Stok takip paneli", "1", "52000", 20)),
        ["sent", "accepted"],
        issued_days_ago=3,
    )
    _make_doc(
        cur,
        org,
        "quote",
        yildiz,
        _items(("Logo ve kurumsal kimlik", "1", "9000", 20)),
        ["sent", "rejected"],
        issued_days_ago=70,
    )
    _make_doc(cur, org, "quote", kaya, _items(("Bakım sözleşmesi (yıllık)", "1", "18000", 20)), [])

    _make_doc(
        cur,
        org,
        "invoice",
        yildiz,
        _items(("API entegrasyonu", "20", "75", 0), ("Barındırma (yıllık)", "1", "300", 0)),
        ["sent"],
        currency="EUR",
        issued_days_ago=45,
        due_in_days=-20,
    )
    paid_items = _items(("Danışmanlık (saat)", "8", "1500", 20))
    paid = _make_doc(
        cur, org, "invoice", kaya, paid_items, ["sent"], issued_days_ago=60, due_in_days=-45
    )
    _pay(cur, org, paid, _gross(paid_items), 50, "Kredi kartı")
    cur.execute("update public.documents set status = 'paid' where id = %s", [paid])
    return org


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo verisini kur")
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--create-user", action="store_true", help="auth.users'a ekle (yalnız yerel)"
    )
    parser.add_argument("--reset", action="store_true", help="varsa demo org'unu silip yeniden kur")
    args = parser.parse_args()

    with psycopg.connect(get_settings().database_url, row_factory=dict_row) as conn:
        if args.create_user:
            conn.execute(
                "insert into auth.users (email) values (%s) on conflict (email) do nothing",
                [args.email],
            )
        user = conn.execute("select id from auth.users where email = %s", [args.email]).fetchone()
        if user is None:
            raise SystemExit(f"{args.email} auth.users'ta yok (önce Supabase Auth'ta oluştur)")
        org = seed_demo(conn, str(user["id"]), reset=args.reset)
    print(f"demo org: {org}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
