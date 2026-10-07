# teklifkit

Freelancer'lar için teklif → fatura uygulaması. **FastAPI + Jinja2/HTMX + Supabase Postgres**,
tenant izolasyonu **Row Level Security** ile veritabanı katmanında sağlanır.

> Durum: M2 (iş akışı). Müşteri yönetimi, teklif/fatura, canlı toplamlar, durum makinesi,
> teklif → fatura dönüşümü, kısmi ödemeler, PDF, müşteri portalı ve özet paneli hazır.
> Canlı yayın (Supabase + Render) sıradaki aşamada.

## Akış

`taslak → gönderildi → kabul/red` (teklif) · `taslak → gönderildi ⇄ ödendi` (fatura). Kabul edilen
teklif tek tıkla faturaya dönüşür (kalemler kopyalanır, bir teklif en fazla bir faturaya). Fatura
ödemeleri kısmi olabilir; bakiye sıfırlanınca fatura kendiliğinden `ödendi` olur, ödeme silinirse
yeniden açılır. "Vadesi geçti" saklanmaz, hesaplanır. Gönderilen belge müşteriye 30 gün geçerli,
iptal edilebilir bir bağlantıyla açılır; müşteri giriş yapmadan teklifi görür, PDF indirir, kabul/red eder.

## Güvenlik tasarımı (özet)

* **RLS backend'de de çalışır.** Backend veritabanına yüksek yetkili bir rolle bağlanır, ama her
  istek kendi transaction'ında `SET LOCAL ROLE authenticated` + JWT claim'lerini set eder
  (`app/db.py`). Politikalar `auth.uid()` ile çalıştığı için, koddaki bir `WHERE org_id` unutkanlığı
  başka kiracının verisini sızdıramaz. "Backend zaten service-role kullanıyor" savunması değildir.
* **Politika deseni** tek yerde: `is_member(org_id)` (`SECURITY DEFINER`, `search_path = ''`).
* **Bileşik FK'lar** `(id, org_id)`: bir belge/kalem/ödeme başka bir org'un müşterisine ya da
  belgesine bağlanamaz, aynı kullanıcı iki org'a üye olsa bile.
* **anon rolünün hiçbir yetkisi yok**; Supabase'in yeni tablolara verdiği varsayılan yetkiler
  migrasyonda açıkça geri alınır.
* **Şema denetimi testi** (`tests/db/audit.py`): RLS'siz tablo, `USING(true)` politikası, anon
  yetkisi, `PUBLIC`'e açık veya `search_path`'siz `SECURITY DEFINER` fonksiyon CI'ı kırar. Denetimin
  kendisi de bilerek bozulmuş şemalarla test edilir.
* **Kurallar veritabanında da**: `documents_guard` geçersiz durum geçişini, kimlik sütunlarının
  (org, tür, numara) değişmesini ve gönderilmiş belgenin müşteri/para birimi/tarihinin değişmesini
  reddeder; gönderilmiş belgenin kalemleri değiştirilemez; ödeme yalnız gönderilmiş faturaya eklenir.
  Bir hata ya da doğrudan SQL bile gönderilmiş faturayı bozamaz.
* **Müşteri portalı ayrı bir rolle çalışır** (`portal`): hiçbir tabloda yetkisi yok, yalnız iki
  `SECURITY DEFINER` fonksiyonu (`portal_document`, `portal_respond`) çağırabilir. Portal koduna bir SQL
  hatası sızsa bile başka kiracının verisi okunamaz. Bağlantı token'ı 256 bit rastgele; veritabanında
  yalnız SHA-256 özeti tutulur ve token yalnızca oluşturulduğu anda görünür. Geçersiz / süresi dolmuş /
  iptal edilmiş / taslak ayırt edilmeden aynı 404'ü verir; portal yanıtları `no-store`, `no-referrer`,
  `noindex` başlıklarıyla gelir.
* **Şema denetimi** bunlara da bakar: `authenticated` yalnız açık bir fonksiyon allowlist'ini
  çağırabilir, `portal` yalnız `portal_*` fonksiyonlarını ve hiçbir tabloyu.
* **Yarış durumları**: ödeme ve dönüştürme belge satırı `FOR UPDATE` ile kilitlenir (paralel testler
  kilit kaldırılınca kırılır); fazla ödeme ve çift fatura mümkün değil.
* Oturum: Supabase JWT'si httpOnly + SameSite=Lax çerezde; imza JWKS (veya HS256 secret) ile
  doğrulanır. Cross-site yazma istekleri (`Sec-Fetch-Site`) reddedilir.
* Para: `numeric` + `Decimal`; KDV oran grubu başına bir kez yuvarlanır (`app/domain/totals.py`).

## Yerelde çalıştırma

```bash
# PDF (WeasyPrint) için: libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core (apt)
pip install -r requirements-dev.txt
docker run -d --name tk-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
python -m app.migrations --stub        # Supabase auth taklidi + şema
export JWT_SECRET=gelistirme-icin-en-az-32-karakterlik-secret DEV_LOGIN=1
uvicorn app.main:app --reload          # /login → "Supabase olmadan giriş yap"
```

Testler (Postgres gerekir):

```bash
export TEKLIFKIT_TEST_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/postgres
pytest -q          # REQUIRE_DB=1 ile Postgres yoksa atlamak yerine hata verir
ruff check . && ruff format --check .
```

## Ortam değişkenleri

| Değişken | Açıklama |
| --- | --- |
| `DATABASE_URL` | Postgres bağlantısı (Supabase'de pooler/direct URL) |
| `JWKS_URL` | Supabase JWKS adresi (asimetrik anahtarlı projeler) |
| `JWT_SECRET` | HS256 secret (yerel / eski projeler); `JWKS_URL` yoksa kullanılır |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Parola ile girişte Supabase Auth çağrısı için |
| `DEV_LOGIN=1` | Yalnız yerelde: `/dev-login` ile Supabase'siz oturum. **Üretimde açma.** |
| `COOKIE_SECURE` | Varsayılan `1` (DEV_LOGIN açıkken `0`) |

## Dizin

```
app/            FastAPI uygulaması (routes/, domain/, templates/, static/)
supabase/migrations/   şema + RLS (Supabase CLI ile de uygulanabilir)
db/test_auth_stub.sql  yerel/CI için auth.uid() ve rol taklidi
tests/          unit/ · db/ (RLS izolasyonu + şema denetimi) · web/ (uçtan uca)
```
