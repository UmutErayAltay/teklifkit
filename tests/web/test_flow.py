"""Uçtan uca web akışı: TestClient + gerçek Postgres + gerçek JWT doğrulama."""

import re
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE
from app.main import app
from tests.web.helpers import DOC_FORM, onboard, token_for

pytestmark = pytest.mark.usefixtures("db_env")


def test_anonymous_is_redirected_to_login():
    c = TestClient(app, follow_redirects=False)
    r = c.get("/documents")
    assert (r.status_code, r.headers["location"]) == (303, "/login")


def test_htmx_request_without_session_gets_hx_redirect():
    r = TestClient(app).get("/clients", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


def test_tampered_or_expired_token_is_rejected(make_user):
    claims = make_user()
    for token in (
        token_for(claims) + "x",
        token_for(claims, exp=int(time.time()) - 10),
        jwt.encode(
            {**claims, "exp": int(time.time()) + 600}, "baska-secret-baska-secret-baska!!", "HS256"
        ),
        token_for(claims, aud="baska"),
    ):
        c = TestClient(app, follow_redirects=False)
        c.cookies.set(COOKIE, token)
        assert c.get("/documents").headers["location"] == "/login"


def test_new_user_is_sent_to_onboarding_then_can_create_org(client_for):
    c = client_for()
    r = c.get("/documents")
    assert (r.status_code, r.headers["location"]) == (303, "/onboarding")
    assert c.post("/onboarding", data={"name": "  "}).status_code == 422
    onboard(c)
    assert c.get("/documents").status_code == 200


def test_cross_site_write_is_blocked(client_for):
    c = client_for()
    r = c.post("/onboarding", data={"name": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_client_crud_via_htmx(client_for):
    c = client_for()
    onboard(c)
    r = c.post("/clients", data={"name": "Acme A.Ş.", "email": "a@acme.test"})
    assert r.status_code == 200 and "Acme A.Ş." in r.text
    assert "Acme A.Ş." in c.get("/clients").text
    client_id = re.search(r"/clients/([0-9a-f-]{36})", r.text).group(1)
    assert c.delete(f"/clients/{client_id}").status_code == 200
    assert c.delete(f"/clients/{client_id}").status_code == 404
    assert "Acme" not in c.get("/clients").text


def test_quote_creation_and_totals(client_for):
    c = client_for()
    onboard(c)
    client_html = c.post("/clients", data={"name": "Acme"}).text
    client_id = re.search(r"/clients/([0-9a-f-]{36})", client_html).group(1)

    r = c.post("/documents", data={**DOC_FORM, "client_id": client_id})
    assert r.status_code == 303
    detail = c.get(r.headers["location"])
    assert detail.status_code == 200
    assert "TKL-0001" in detail.text and "Acme" in detail.text
    # 10.000 + 12×50 = 10.600 net; %20 KDV = 2.120; toplam 12.720
    assert "10.600,00 ₺" in detail.text
    assert "2.120,00 ₺" in detail.text
    assert "12.720,00 ₺" in detail.text

    second = c.post("/documents", data=DOC_FORM)
    assert "TKL-0002" in c.get(second.headers["location"]).text
    listing = c.get("/documents").text
    assert "TKL-0001" in listing and "TKL-0002" in listing and "12.720,00 ₺" in listing


def test_live_totals_preview_ignores_invalid_rows(client_for):
    c = client_for()
    r = c.post(
        "/documents/totals",
        data={
            "currency": "EUR",
            "description": ["a", "bozuk"],
            "qty": ["2", "x"],
            "unit_price": ["10", "5"],
            "vat_rate": ["10", "20"],
        },
    )
    assert r.status_code == 200
    assert "20,00 €" in r.text and "22,00 €" in r.text  # net 20, KDV %10 = 2, toplam 22
    assert "KDV %20" not in r.text


def test_invalid_document_rerenders_form_with_values_and_error(client_for):
    c = client_for()
    onboard(c)
    r = c.post("/documents", data={**DOC_FORM, "vat_rate": ["20", "18"]})
    assert r.status_code == 422
    assert "KDV oranı geçersiz" in r.text
    assert "Web tasarım" in r.text  # girilen değerler korunur
    assert (
        c.post("/documents", data={**DOC_FORM, "description": [], "unit_price": []}).status_code
        == 422
    )


def test_tenants_cannot_see_each_others_documents_or_clients(client_for):
    a, b = client_for("a"), client_for("b")
    onboard(a, "Org A")
    onboard(b, "Org B")
    a_client = a.post("/clients", data={"name": "Gizli Müşteri"}).text
    a_client_id = re.search(r"/clients/([0-9a-f-]{36})", a_client).group(1)
    a_doc = a.post("/documents", data={**DOC_FORM, "client_id": a_client_id}).headers["location"]

    assert b.get(a_doc).status_code == 404
    assert "Gizli Müşteri" not in b.get("/clients").text
    assert "TKL-0001" not in b.get("/documents").text
    assert b.delete(f"/clients/{a_client_id}").status_code == 404
    # B, A'nın müşterisini kendi belgesine bağlayamaz.
    r = b.post("/documents", data={**DOC_FORM, "client_id": a_client_id})
    assert r.status_code == 422 and "müşteri bulunamadı" in r.text
    assert a.get(a_doc).status_code == 200


def test_client_with_documents_cannot_be_deleted(client_for):
    c = client_for()
    onboard(c)
    cid = re.search(r"/clients/([0-9a-f-]{36})", c.post("/clients", data={"name": "X"}).text).group(
        1
    )
    c.post("/documents", data={**DOC_FORM, "client_id": cid})
    assert c.delete(f"/clients/{cid}").status_code == 409


def test_dev_login_is_disabled_by_default():
    assert TestClient(app).get("/dev-login").status_code == 404


def test_login_sets_httponly_cookie(monkeypatch, make_user):
    token = token_for(make_user())
    monkeypatch.setattr("app.routes.auth.password_login", lambda e, p: token)
    r = TestClient(app, follow_redirects=False).post(
        "/login", data={"email": "a@b.c", "password": "x"}
    )
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert f"{COOKIE}=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" in cookie


def test_login_failure_shows_error(monkeypatch):
    from app.auth import AuthError

    def boom(e, p):
        raise AuthError("E-posta veya parola hatalı")

    monkeypatch.setattr("app.routes.auth.password_login", boom)
    r = TestClient(app).post("/login", data={"email": "a@b.c", "password": "x"})
    assert r.status_code == 401 and "parola hatalı" in r.text
