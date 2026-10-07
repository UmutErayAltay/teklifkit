"""Salt-okunur demo hesabı: web arayüzü gezilebilir, hiçbir yazma yolu çalışmaz."""

import re

import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE
from app.main import app
from app.seed import seed_demo
from tests.web.helpers import DOC_FORM, token_for

pytestmark = pytest.mark.usefixtures("db_env")


@pytest.fixture
def viewer(make_user, admin_conn):
    claims = make_user("viewer")
    seed_demo(admin_conn, claims["sub"])
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(COOKIE, token_for(claims))
    return c


def doc_ids(c) -> dict[str, str]:
    html = c.get("/documents").text
    return {
        no: i for i, no in re.findall(r'href="/documents/([0-9a-f-]{36})">([A-Z]{3}-\d{4})<', html)
    }


def test_viewer_browses_everything_with_a_banner_and_no_write_controls(viewer):
    docs = viewer.get("/documents").text
    assert "Salt okunur demo hesabı" in docs
    assert "+ Teklif" not in docs and "+ Fatura" not in docs
    assert len(doc_ids(viewer)) == 8

    clients = viewer.get("/clients").text
    assert clients.count("<tr>") == 1 + 3 and "Müşteri ekle" not in clients
    assert "Sil" not in clients

    ids = doc_ids(viewer)
    invoice = viewer.get(f"/documents/{ids['FTR-0001']}").text  # kısmi ödenmiş
    assert "Havale" in invoice and "16.920,00 ₺" in invoice
    for control in ("Ödeme ekle", "Müşteri bağlantısı oluştur", "Gönderildi olarak işaretle"):
        assert control not in invoice, control
    quote = viewer.get(f"/documents/{ids['TKL-0003']}").text  # kabul edilmiş, faturalanmamış
    assert "Faturaya dönüştür" not in quote

    pdf = viewer.get(f"/documents/{ids['FTR-0001']}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_dashboard_numbers_for_seeded_data(viewer):
    html = viewer.get("/dashboard").text
    cards = {
        m.group(1): m.group(2)
        for m in re.finditer(
            r'<div class="muted">([^<]+)</div>(.*?)<div class="muted small">', html, re.S
        )
    }
    assert "61.800,00 ₺" in cards["Açık teklifler"]  # (38.000 + 3×4.500) × 1,2
    assert "62.400,00 ₺" in cards["Faturalanacak"]  # 52.000 × 1,2
    assert "16.920,00 ₺" in cards["Tahsil edilecek"] and "1.800,00 €" in cards["Tahsil edilecek"]
    assert "1.800,00 €" in cards["Vadesi geçmiş"] and "1 belge" in cards["Vadesi geçmiş"]
    assert "+ Teklif" not in html


def test_every_write_path_is_denied_with_403(viewer):
    ids = doc_ids(viewer)
    paid = ids["FTR-0003"]
    attempts = [
        ("/clients", {"name": "Sızma"}),
        ("/documents", DOC_FORM),
        (f"/documents/{ids['TKL-0005']}/status", {"to": "sent"}),
        (f"/documents/{ids['TKL-0003']}/convert", {}),
        (f"/documents/{ids['FTR-0001']}/payments", {"amount": "1"}),
        (f"/documents/{paid}/payments/{'0' * 8}-0000-0000-0000-{'0' * 12}/delete", {}),
        (f"/documents/{ids['FTR-0001']}/portal-link", {}),
        (f"/documents/{ids['FTR-0001']}/portal-links/revoke", {}),
    ]
    for url, data in attempts:
        r = viewer.post(url, data=data)
        assert r.status_code == 403 and "yetkin yok" in r.text, (url, r.status_code)
    # istemciyi silme de reddedilir
    client_id = re.search(r"/clients/([0-9a-f-]{36})", viewer.get("/clients").text)
    assert client_id is None  # viewer'a silme düğmesi bile gösterilmez
    # hiçbir şey değişmedi
    assert len(doc_ids(viewer)) == 8
    assert "Sızma" not in viewer.get("/clients").text


def test_viewer_cannot_delete_a_client_even_with_the_url(viewer, admin_conn):
    cid = admin_conn.execute("select id from public.clients limit 1").fetchone()[0]
    r = viewer.delete(f"/clients/{cid}")
    assert r.status_code == 403
    assert admin_conn.execute("select count(*) from public.clients").fetchone()[0] >= 3


# ------------------------------------------------------------------- demo girişi
def test_demo_login_is_off_unless_configured(monkeypatch):
    for var in ("DEMO_EMAIL", "DEMO_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    c = TestClient(app, follow_redirects=False)
    assert c.post("/demo-login").status_code == 404
    assert "demo hesabıyla" not in c.get("/login").text


def test_demo_login_signs_in_with_server_side_password(monkeypatch, make_user):
    monkeypatch.setenv("DEMO_EMAIL", "demo@teklifkit.example")
    monkeypatch.setenv("DEMO_PASSWORD", "sunucuda-kalan-parola")
    seen = {}

    def fake_login(email, password):
        seen["creds"] = (email, password)
        return token_for(make_user())

    monkeypatch.setattr("app.routes.auth.password_login", fake_login)
    c = TestClient(app, follow_redirects=False)
    page = c.get("/login").text
    assert "demo hesabıyla" in page and "sunucuda-kalan-parola" not in page
    r = c.post("/demo-login")
    assert r.status_code == 303 and COOKIE in r.headers["set-cookie"]
    assert seen["creds"] == ("demo@teklifkit.example", "sunucuda-kalan-parola")


def test_demo_login_failure_is_503_not_a_leak(monkeypatch):
    from app.auth import AuthError

    monkeypatch.setenv("DEMO_EMAIL", "demo@teklifkit.example")
    monkeypatch.setenv("DEMO_PASSWORD", "x")

    def boom(*_):
        raise AuthError("E-posta veya parola hatalı")

    monkeypatch.setattr("app.routes.auth.password_login", boom)
    r = TestClient(app, follow_redirects=False).post("/demo-login")
    assert r.status_code == 503 and "parola" not in r.text


def test_demo_login_in_dev_mode_needs_no_password(monkeypatch):
    monkeypatch.setenv("DEV_LOGIN", "1")
    monkeypatch.setenv("DEMO_EMAIL", "demo-dev@teklifkit.example")
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    r = TestClient(app, follow_redirects=False).post("/demo-login")
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
