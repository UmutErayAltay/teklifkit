import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE
from app.main import app
from tests.web.helpers import token_for


@pytest.fixture
def client_for(make_user):
    """Her çağrı yeni bir kullanıcı + kendi çerezli TestClient döndürür."""

    def _make(label="u") -> TestClient:
        claims = make_user(label)
        c = TestClient(app, follow_redirects=False)
        c.cookies.set(COOKIE, token_for(claims))
        c.claims = claims
        return c

    return _make
