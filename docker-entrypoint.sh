#!/bin/sh
# RUN_MIGRATIONS=1: açılışta bekleyen migrasyonları uygula (tek örnekli dağıtım için).
# DATABASE_URL migrasyon için doğrudan/session bağlantı olmalı (transaction pooler değil).
set -eu

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    python -m app.migrations
fi

# --forwarded-allow-ips="*": konteynere yalnız platformun ters vekili (Render) ulaşır; böylece
# X-Forwarded-Proto/For güvenilir ve oluşturulan bağlantılar https olur.
exec uvicorn app.main:app \
    --host 0.0.0.0 --port "${PORT:-10000}" \
    --proxy-headers --forwarded-allow-ips="*"
