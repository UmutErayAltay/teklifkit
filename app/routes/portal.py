"""Müşteri portalı: oturumsuz, token ile. Veritabanına yalnız `portal` rolüyle ve yalnız
portal_document()/portal_respond() fonksiyonlarıyla erişir (tablo yetkisi yok)."""

import hashlib

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.db import portal_db
from app.routes.workflow import pdf_response
from app.views import DocView, from_portal_json
from app.web import KIND_TR, templates

router = APIRouter(prefix="/p")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load(token: str) -> DocView:
    with portal_db() as conn:
        data = conn.execute("select public.portal_document(%s) as d", [_hash(token)]).fetchone()[
            "d"
        ]
    if data is None:  # geçersiz, süresi dolmuş, iptal edilmiş ya da taslak: ayırt ettirme
        raise HTTPException(status_code=404)
    return from_portal_json(data)


@router.get("/{token}", response_class=HTMLResponse)
def portal_page(request: Request, token: str):
    view = _load(token)
    return templates.TemplateResponse(
        request,
        "portal.html",
        {"v": view, "token": token, "KIND_TR": KIND_TR, "message": None, "user": None},
    )


@router.post("/{token}/respond", response_class=HTMLResponse)
def portal_respond(request: Request, token: str, decision: str = Form(...)):
    if decision not in ("accepted", "rejected"):
        raise HTTPException(status_code=422)
    with portal_db() as conn:
        result = conn.execute(
            "select public.portal_respond(%s, %s) as r", [_hash(token), decision]
        ).fetchone()["r"]
    if result is None:
        view = _load(token)  # token geçersizse 404; geçerliyse teklif artık yanıtlanamaz
        return templates.TemplateResponse(
            request,
            "portal.html",
            {
                "v": view,
                "token": token,
                "KIND_TR": KIND_TR,
                "message": "Bu teklif artık yanıtlanamaz.",
                "user": None,
            },
            status_code=409,
        )
    return RedirectResponse(f"/p/{token}", status_code=303)


@router.get("/{token}/pdf")
def portal_pdf(token: str) -> Response:
    return pdf_response(_load(token))
