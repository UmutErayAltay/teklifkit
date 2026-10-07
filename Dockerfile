FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# WeasyPrint (PDF) sistem kütüphaneleri + Türkçe karakter/₺ için font
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --create-home --uid 10001 app
WORKDIR /srv/teklifkit

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY supabase ./supabase
COPY db ./db
COPY docker-entrypoint.sh ./

USER app
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os,urllib.request as u; u.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"10000\")}/healthz', timeout=3)"
CMD ["./docker-entrypoint.sh"]
