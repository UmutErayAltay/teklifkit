"""M2 uçtan uca: durum geçişleri, faturaya dönüştürme, ödemeler, portal, PDF, dashboard."""

import hashlib
import io
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.main import app
from tests.web.helpers import create_client_id, create_doc, onboard, set_status

pytestmark = pytest.mark.usefixtures("db_env")

GROSS = "12.720,00 ₺"  # DOC_FORM: 10.000 + 12×50 = 10.600 net + %20 KDV


@pytest.fixture
def owner(client_for):
    c = client_for("owner")
    onboard(c, "Atölye Çelik")
    return c


def page(c, path):
    r = c.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


def sent_invoice(c, **extra):
    doc = create_doc(c, "invoice", **extra)
    assert set_status(c, doc, "sent").status_code == 303
    return doc


def accepted_quote(c, **extra):
    doc = create_doc(c, "quote", **extra)
    assert set_status(c, doc, "sent").status_code == 303
    assert set_status(c, doc, "accepted").status_code == 303
    return doc


# --------------------------------------------------------------------- durumlar
def test_quote_status_buttons_follow_the_state_machine(owner):
    doc = create_doc(owner)
    assert "Gönderildi olarak işaretle" in page(owner, f"/documents/{doc}")
    assert set_status(owner, doc, "sent").status_code == 303
    html = page(owner, f"/documents/{doc}")
    assert "Kabul edildi" in html and "Reddedildi" in html
    assert "Gönderildi olarak işaretle" not in html
    assert set_status(owner, doc, "rejected").status_code == 303
    html = page(owner, f"/documents/{doc}")
    assert "reddedildi" in html and "Kabul edildi</button>" not in html


def test_illegal_status_change_is_refused_with_message(owner):
    doc = create_doc(owner)
    r = set_status(owner, doc, "accepted")  # göndermeden kabul
    assert r.status_code == 409 and "Bu durum geçişi yapılamaz" in r.text
    inv = sent_invoice(owner)
    assert set_status(owner, inv, "paid").status_code == 409  # ödeme kaydı olmadan ödendi olmaz
    assert set_status(owner, inv, "nonsense").status_code == 409


# ------------------------------------------------------------------- dönüştürme
def test_convert_accepted_quote_to_invoice_copies_items_and_is_idempotent(owner):
    cid = create_client_id(owner, "Çelik Müşavirlik")
    draft = create_doc(owner, client_id=cid)
    assert owner.post(f"/documents/{draft}/convert").status_code == 409  # kabul edilmedi
    quote = accepted_quote(owner, client_id=cid)

    r = owner.post(f"/documents/{quote}/convert")
    assert r.status_code == 303
    invoice_url = r.headers["location"]
    html = page(owner, invoice_url)
    assert "Fatura FTR-0001" in html and "taslak" in html
    assert "Web tasarım" in html and "Barındırma" in html and GROSS in html
    assert "Çelik Müşavirlik" in html and "Kaynak teklif" in html
    due = (date.today() + timedelta(days=14)).strftime("%d.%m.%Y")
    assert due in html

    assert owner.post(f"/documents/{quote}/convert").headers["location"] == invoice_url
    quote_html = page(owner, f"/documents/{quote}")
    assert "Faturası" in quote_html and "Faturaya dönüştür" not in quote_html
    assert page(owner, "/documents").count("FTR-0001") == 1


def test_parallel_conversions_create_exactly_one_invoice(owner):
    quote = accepted_quote(owner)

    def convert(_):
        c = TestClient(app, follow_redirects=False)
        c.cookies.update(owner.cookies)
        return c.post(f"/documents/{quote}/convert").headers["location"]

    with ThreadPoolExecutor(6) as pool:
        locations = set(pool.map(convert, range(6)))
    assert len(locations) == 1
    assert page(owner, "/documents").count("FTR-") == 1


# ---------------------------------------------------------------------- ödemeler
def pay(c, doc, amount, **extra):
    return c.post(f"/documents/{doc}/payments", data={"amount": amount, **extra})


def test_partial_then_full_payment_closes_the_invoice(owner):
    inv = sent_invoice(owner)
    assert pay(owner, inv, "5.000,00", method="Havale").status_code == 303
    html = page(owner, f"/documents/{inv}")
    assert "Havale" in html and "7.720,00 ₺" in html and "gönderildi" in html

    assert pay(owner, inv, "7720").status_code == 303
    html = page(owner, f"/documents/{inv}")
    assert "ödendi" in html and "Ödeme ekle" not in html  # kapanınca form kalkar

    # ödeme silinince fatura yeniden açılır
    pid = re.search(r"/payments/([0-9a-f-]{36})/delete", html).group(1)
    assert owner.post(f"/documents/{inv}/payments/{pid}/delete").status_code == 303
    html = page(owner, f"/documents/{inv}")
    assert "gönderildi" in html and "Ödeme ekle" in html and "7.720,00 ₺" in html


def test_invalid_payments_are_rejected(owner):
    draft = create_doc(owner, "invoice")
    assert pay(owner, draft, "10").status_code == 409  # taslağa ödeme yok
    quote = create_doc(owner)
    assert pay(owner, quote, "10").status_code == 409
    inv = sent_invoice(owner)
    for amount in ("0", "-5", "abc", "12.720,01"):  # sonuncusu bakiyeyi aşar
        r = pay(owner, inv, amount)
        assert r.status_code == 422, amount
    assert "aşamaz" in pay(owner, inv, "99999").text
    assert pay(owner, inv, "10", paid_on="2026-13-45").status_code == 422
    assert "Henüz ödeme yok" in page(owner, f"/documents/{inv}")


def test_parallel_payments_cannot_overpay(owner):
    inv = sent_invoice(owner)

    def attempt(_):
        c = TestClient(app, follow_redirects=False)
        c.cookies.update(owner.cookies)
        return pay(c, inv, "12720").status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(attempt, range(8)))
    assert codes.count(303) == 1, codes
    assert page(owner, f"/documents/{inv}").count("/delete") == 1


def test_overdue_is_shown_in_list_and_clears_when_paid(owner):
    inv = sent_invoice(owner, due_date="2020-01-01")
    assert "vadesi geçti" in page(owner, "/documents")
    assert "vadesi geçti" in page(owner, f"/documents/{inv}")
    pay(owner, inv, "12720")
    assert "vadesi geçti" not in page(owner, "/documents")


# ------------------------------------------------------------------------ portal
def make_link(c, doc) -> str:
    r = c.post(f"/documents/{doc}/portal-link", headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    return re.search(r"/p/([A-Za-z0-9_-]{20,})", r.text).group(1)


def anon() -> TestClient:
    return TestClient(app, follow_redirects=False)


def test_portal_link_is_not_stored_in_plaintext(owner, admin_conn):
    doc = accepted_quote(owner)
    token = make_link(owner, doc)
    hashed = hashlib.sha256(token.encode()).hexdigest()
    rows = admin_conn.execute("select token_hash from public.portal_tokens").fetchall()
    assert (hashed,) in rows and all(token not in h for (h,) in rows)


def test_customer_can_view_and_accept_quote_without_logging_in(owner):
    cid = create_client_id(owner, "Acme")
    doc = create_doc(owner, client_id=cid)
    assert owner.post(f"/documents/{doc}/portal-link").status_code == 409  # taslak paylaşılmaz
    set_status(owner, doc, "sent")
    token = make_link(owner, doc)

    r = anon().get(f"/p/{token}")
    assert r.status_code == 200
    assert "Atölye Çelik" in r.text and "Teklifi kabul et" in r.text and GROSS in r.text
    assert "Çıkış" not in r.text and 'href="/documents' not in r.text  # uygulama menüsü yok
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in r.headers["x-robots-tag"]

    resp = anon().post(f"/p/{token}/respond", data={"decision": "accepted"})
    assert resp.status_code == 303 and resp.headers["location"] == f"/p/{token}"
    assert "kabul ettiniz" in anon().get(f"/p/{token}").text
    assert "kabul edildi" in page(owner, f"/documents/{doc}")

    again = anon().post(f"/p/{token}/respond", data={"decision": "rejected"})
    assert again.status_code == 409 and "artık yanıtlanamaz" in again.text
    assert "kabul edildi" in page(owner, f"/documents/{doc}")


def test_customer_can_reject_and_bad_decisions_are_refused(owner):
    doc = create_doc(owner)
    set_status(owner, doc, "sent")
    token = make_link(owner, doc)
    assert anon().post(f"/p/{token}/respond", data={"decision": "paid"}).status_code == 422
    assert anon().post(f"/p/{token}/respond", data={"decision": "rejected"}).status_code == 303
    assert "reddedildi" in page(owner, f"/documents/{doc}")


def test_customer_sees_invoice_balance_but_has_no_actions(owner):
    inv = sent_invoice(owner)
    pay(owner, inv, "2.000,00")
    token = make_link(owner, inv)
    html = anon().get(f"/p/{token}").text
    assert "10.720,00 ₺" in html and "2.000,00 ₺" in html
    assert "kabul et" not in html
    assert anon().post(f"/p/{token}/respond", data={"decision": "accepted"}).status_code == 409


def test_revoked_and_unknown_links_are_404(owner):
    doc = create_doc(owner)
    set_status(owner, doc, "sent")
    token = make_link(owner, doc)
    assert anon().get(f"/p/{token}").status_code == 200
    assert "1 aktif bağlantı" in page(owner, f"/documents/{doc}")

    assert owner.post(f"/documents/{doc}/portal-links/revoke").status_code == 303
    assert anon().get(f"/p/{token}").status_code == 404
    assert anon().get(f"/p/{token}/pdf").status_code == 404
    assert anon().post(f"/p/{token}/respond", data={"decision": "accepted"}).status_code == 404
    assert anon().get("/p/" + "x" * 43).status_code == 404
    assert "aktif bağlantı" not in page(owner, f"/documents/{doc}")


def test_links_do_not_cross_tenants(client_for):
    a, b = client_for("a"), client_for("b")
    onboard(a, "A")
    onboard(b, "B")
    doc = create_doc(a)
    set_status(a, doc, "sent")
    assert b.post(f"/documents/{doc}/portal-link").status_code == 404
    token = make_link(a, doc)
    assert b.post(f"/documents/{doc}/portal-links/revoke").status_code == 404
    assert anon().get(f"/p/{token}").status_code == 200  # B iptal edemedi


# --------------------------------------------------------------------------- PDF
def pdf_text(content: bytes) -> str:
    assert content.startswith(b"%PDF")
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(content)).pages)


def test_pdf_for_owner_and_portal_contain_the_document(owner):
    cid = create_client_id(owner, "Çelik Müşavirlik")
    doc = create_doc(owner, client_id=cid)
    set_status(owner, doc, "sent")
    r = owner.get(f"/documents/{doc}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert 'filename="TKL-0001.pdf"' in r.headers["content-disposition"]
    text = pdf_text(r.content)
    for needle in ("Atölye Çelik", "Teklif TKL-0001", "Çelik Müşavirlik", "Web tasarım", GROSS):
        assert needle in text, needle

    portal = anon().get(f"/p/{make_link(owner, doc)}/pdf")
    assert portal.status_code == 200 and "TKL-0001" in pdf_text(portal.content)


def test_pdf_escapes_user_content(owner):
    doc = create_doc(
        owner, description=["<img src=http://127.0.0.1:1/x>", "ok"], unit_price=["1", "1"]
    )
    r = owner.get(f"/documents/{doc}/pdf")
    assert r.status_code == 200
    assert "<img src=http://127.0.0.1:1/x>" in pdf_text(r.content)  # metin olarak basılır


def test_pdf_requires_login_and_tenant_match(client_for, owner):
    doc = create_doc(owner)
    assert anon().get(f"/documents/{doc}/pdf").headers["location"] == "/login"
    other = client_for("o")
    onboard(other, "Başka")
    assert other.get(f"/documents/{doc}/pdf").status_code == 404


# ---------------------------------------------------------------------- dashboard
def test_dashboard_buckets(owner):
    create_doc(owner)  # taslak: hiçbir kutuda yok
    sent_quote = create_doc(owner)
    set_status(owner, sent_quote, "sent")
    accepted_quote(owner)  # faturalanmamış kabul
    converted = accepted_quote(owner)
    owner.post(f"/documents/{converted}/convert")  # artık faturalanacak sayılmaz
    inv = sent_invoice(owner, due_date="2020-01-01")  # vadesi geçmiş
    pay(owner, inv, "720")
    sent_invoice(owner)  # vadesi geçmemiş (varsayılan: vade yok)

    html = page(owner, "/dashboard")
    cards = {
        m.group(1): m.group(2)
        for m in re.finditer(
            r'<div class="muted">([^<]+)</div>(.*?)<div class="muted small">', html, re.S
        )
    }
    assert GROSS in cards["Açık teklifler"] and "1 belge" in cards["Açık teklifler"]
    assert GROSS in cards["Faturalanacak"] and "1 belge" in cards["Faturalanacak"]
    assert (
        "24.720,00 ₺" in cards["Tahsil edilecek"]
    )  # 12.000 + 12.720 and "2 belge" in cards["Tahsil edilecek"]
    assert "12.000,00 ₺" in cards["Vadesi geçmiş"] and "1 belge" in cards["Vadesi geçmiş"]
    assert "Vadesi geçenler" in html and "FTR-" in html


def test_dashboard_empty_state_and_isolation(client_for):
    a, b = client_for("a"), client_for("b")
    onboard(a, "A")
    onboard(b, "B")
    sent_invoice(a, due_date="2020-01-01")
    html = page(b, "/dashboard")
    assert "12.720" not in html and "Vadesi geçenler" not in html


# ------------------------------------------------------------ kiracı izolasyonu
def test_other_tenant_cannot_touch_workflow_endpoints(client_for):
    a, b = client_for("a"), client_for("b")
    onboard(a, "A")
    onboard(b, "B")
    quote = accepted_quote(a)
    invoice = sent_invoice(a)
    pay(a, invoice, "100")
    pid = re.search(r"/payments/([0-9a-f-]{36})/delete", page(a, f"/documents/{invoice}")).group(1)

    attempts = [
        ("post", f"/documents/{quote}/status", {"to": "rejected"}),
        ("post", f"/documents/{quote}/convert", {}),
        ("post", f"/documents/{invoice}/payments", {"amount": "1"}),
        ("post", f"/documents/{invoice}/payments/{pid}/delete", {}),
        ("post", f"/documents/{invoice}/portal-link", {}),
        ("get", f"/documents/{invoice}/pdf", None),
        ("get", f"/documents/{invoice}", None),
    ]
    for method, url, data in attempts:
        r = getattr(b, method)(url, **({"data": data} if data is not None else {}))
        assert r.status_code == 404, (method, url, r.status_code)

    assert "kabul edildi" in page(a, f"/documents/{quote}")
    assert "Henüz ödeme yok" not in page(a, f"/documents/{invoice}")


def test_portal_link_uses_public_base_url_when_configured(owner, monkeypatch):
    doc = create_doc(owner)
    set_status(owner, doc, "sent")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://teklifkit.example/")
    r = owner.post(
        f"/documents/{doc}/portal-link", headers={"HX-Request": "true", "Host": "evil.test"}
    )
    assert 'value="https://teklifkit.example/p/' in r.text
    assert "evil.test" not in r.text
