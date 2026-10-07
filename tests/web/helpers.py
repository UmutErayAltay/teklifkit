"""Web testleri için ortak yardımcılar."""

import re
import time

import jwt
from fastapi.testclient import TestClient

from tests.conftest import JWT_SECRET


def token_for(claims: dict, **overrides) -> str:
    payload = {**claims, "exp": int(time.time()) + 600, **overrides}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


DOC_FORM = {
    "kind": "quote",
    "currency": "TRY",
    "description": ["Web tasarım", "Barındırma"],
    "qty": ["1", "12"],
    "unit_price": ["10.000,00", "50"],
    "vat_rate": ["20", "20"],
}


def onboard(c: TestClient, name="Atölye"):
    r = c.post("/onboarding", data={"name": name})
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"


def create_client_id(c: TestClient, name="Acme") -> str:
    html = c.post("/clients", data={"name": name}).text
    return re.search(r"/clients/([0-9a-f-]{36})", html).group(1)


def create_doc(c: TestClient, kind="quote", **extra) -> str:
    """Belge oluşturur, id döndürür."""
    r = c.post("/documents", data={**DOC_FORM, "kind": kind, **extra})
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[1]


def set_status(c: TestClient, doc_id: str, to: str):
    return c.post(f"/documents/{doc_id}/status", data={"to": to})
