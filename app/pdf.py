"""PDF üretimi (WeasyPrint). Şablon yalnızca kendi içeriğini kullanır; dış kaynak çekilmez."""

from weasyprint import HTML


def _deny_fetch(url: str, *args, **kwargs):
    raise ValueError(f"PDF üretiminde dış kaynak yüklenemez: {url}")


def render_pdf(html: str) -> bytes:
    return HTML(string=html, url_fetcher=_deny_fetch).write_pdf()
