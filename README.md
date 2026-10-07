# teklifkit

Freelancer'lar için teklif → fatura uygulaması. **FastAPI + Jinja2/HTMX + Postgres (Supabase)**.
Kiracı (işletme) izolasyonu uygulama koduna değil, **veritabanında Row Level Security**'ye emanet.

| Özet paneli | Teklif oluşturma (canlı toplam) |
| --- | --- |
| ![Özet](docs/screenshots/01-dashboard.png) | ![Teklif formu](docs/screenshots/04-quote-form.png) |
| **Fatura, kısmi ödeme** | **Müşteri portalı** (giriş yok, token ile) |
| ![Fatura](docs/screenshots/03-invoice.png) | ![Portal](docs/screenshots/06-portal.png) |

PDF çıktısı (WeasyPrint):

![PDF](docs/screenshots/05-pdf.png)

> Durum: kod tamam, 135 test yerel Postgres'e karşı yeşil. **Canlı yayın (Supabase + Render) henüz
> yapılmadı**, gerçek Supabase Auth ile giriş ve token yenileme denenmedi (aşağıda "Bilinen sınırlar").
> Görseller `python -m app.seed` ile kurulan demo veriden alındı.

## Ne yapar

* **Teklif → fatura akışı:** `taslak → gönderildi → kabul/red` (teklif), `taslak → gönderildi ⇄ ödendi` (fatura).
  Kabul edilen teklif tek tıkla faturaya dönüşür (kalemler kopyalanır, bir teklif en fazla bir faturaya).
* **Kısmi ödemeler:** bakiye sıfırlanınca fatura kendiliğinden `ödendi` olur, ödeme silinirse yeniden
  açılır. "Vadesi geçti" saklanmaz, hesaplanır.
* **Müşteri portalı:** gönderilen belge, 30 gün geçerli ve iptal edilebilir bir bağlantıyla açılır;
  müşteri giriş yapmadan görür, PDF indirir, teklifi kabul/red eder.
* **Para:** `numeric` + `Decimal`, TRY/EUR/USD, KDV %0/1/10/20; KDV oran grubu başına bir kez yuvarlanır.
  Özet paneli para birimlerini karıştırmaz.
* **Roller:** `owner`/`member` yazar, `viewer` salt okur (kamuya açık demo hesabı bu rolde).

## Mimari

```mermaid
flowchart LR
  B["Tarayıcı (HTMX)"] -- "çerez: Supabase JWT" --> A["FastAPI"]
  C["Müşteri"] -- "/p/token" --> A
  A -- "SET LOCAL ROLE authenticated<br/>+ JWT claim'leri" --> D[("Postgres + RLS")]
  A -- "SET LOCAL ROLE portal<br/>yalnız portal_*()" --> D
  A -. "JWKS ile imza doğrulama" .-> S["Supabase Auth"]
```

* Tek süreç, tek dağıtım: JS derleme adımı yok (HTMX dosyası `app/static/`'te), ORM yok (psycopg + düz SQL;
  RLS ve sorgular okunur kalsın).
* Backend veritabanına yüksek yetkili rolle bağlanır ama **her istek kendi transaction'ında**
  `authenticated` (ya da `portal`) rolüne düşer; politikalar `auth.uid()` ile çalışır (`app/db.py`).
* Şema ve politikalar `supabase/migrations/`'ta, SQL olarak; `python -m app.migrations` uygular
  (Supabase CLI ile de uygulanabilir ama ikisini karıştırma, geçmişi ayrı tutarlar).

## Güvenlik tasarımı

* **RLS backend'de de çalışır.** "Backend zaten service-role kullanıyor" savunma değildir: koddaki bir
  `WHERE org_id` unutkanlığı başka kiracının verisini sızdıramaz.
* **Politika deseni tek yerde:** okuma `is_member(org_id)`, yazma `can_write(org_id)`; ikisi de
  `SECURITY DEFINER` + `search_path = ''`, `EXECUTE` yalnız `authenticated`'a açık.
* **Bileşik FK'lar** `(id, org_id)`: belge/kalem/ödeme başka org'un müşterisine ya da belgesine
  bağlanamaz, aynı kullanıcı iki org'a üye olsa bile.
* **`anon`'un hiçbir yetkisi yok**; Supabase'in yeni tablolara verdiği varsayılan yetkiler migrasyonda
  açıkça geri alınır.
* **İş kuralları veritabanında da:** `documents_guard` geçersiz durum geçişini, kimlik sütunlarının
  (org, tür, numara) değişmesini ve gönderilmiş belgenin müşteri/para birimi/tarihinin değişmesini
  reddeder; gönderilmiş belgenin kalemleri değişmez; ödeme yalnız gönderilmiş faturaya eklenir.
  Bir hata ya da doğrudan SQL bile gönderilmiş faturayı bozamaz.
* **Salt-okunur rol RLS'te:** `viewer`'ın yazma politikası yok. Arayüz düğmeleri gizler ve yazma uçları
  net 403 verir, ama asıl garanti veritabanındadır (UPDATE/DELETE RLS'te sessizce 0 satıra düşer;
  testler verinin gerçekten değişmediğini ayrıca doğrular).
* **Portal ayrı bir rolle çalışır** (`portal`): hiçbir tabloda yetkisi yok, yalnız iki
  `SECURITY DEFINER` fonksiyonu çağırır (`portal_document`, `portal_respond`). Portal koduna bir SQL
  hatası sızsa bile başka kiracının verisi okunamaz. Token 256 bit rastgele; DB'de yalnız SHA-256
  özeti tutulur ve token yalnızca oluşturulduğu anda görünür. Geçersiz / süresi dolmuş / iptal edilmiş /
  taslak ayırt edilmeden aynı 404'ü verir; yanıtlar `no-store`, `no-referrer`, `noindex` başlıklarıyla gelir.
* **Şema denetimi CI'da** (`tests/db/audit.py`): RLS'siz tablo, `USING(true)` politikası, anon yetkisi,
  `PUBLIC`'e açık ya da `search_path`'siz `SECURITY DEFINER` fonksiyon, allowlist dışında `authenticated`'a
  açılmış fonksiyon, portal rolünün tablo yetkisi ve `can_write()` kullanmayan yazma politikası CI'ı
  kırar. **Denetimin kendisi de bilerek bozulmuş şemalarla test edilir**; sessizce her şeyi "temiz" bulan
  bir denetim işe yaramaz.
* **Yarış durumları:** ödeme ve dönüştürme belge satırını `FOR UPDATE` ile kilitler. Paralel istek
  testleri kilit kaldırılınca kırılır (fazla ödeme ve çift fatura mümkün değil).
* Oturum: Supabase JWT'si httpOnly + SameSite=Lax çerezde, imza JWKS (ya da HS256 secret) ile doğrulanır.
  Cross-site yazma istekleri (`Sec-Fetch-Site`) reddedilir. `PUBLIC_BASE_URL` ayarlıysa portal bağlantısı
  ondan kurulur (Host başlığına güvenilmez). PDF üretimi dış kaynak çekmez, kullanıcı metni kaçışlanır.

## Tasarım kararları ve ödünleşimler

| Karar | Neden / bedeli |
| --- | --- |
| ORM yok, düz SQL | RLS ve tetikleyiciler SQL'de yaşıyor; sorgular okunur kalır. Bedel: elle eşleme. |
| İstek başına bağlantı, havuz yok | Supabase transaction-pooler ile uyumlu (`prepare_threshold=None`); basit. Yük altında `psycopg_pool` eklenir. |
| Toplamlar Python'da hesaplanır | KDV yuvarlama kuralı tek yerde (`app/domain/totals.py`), DB'de kopyası yok. Bedel: dashboard gönderilmiş belgeleri bellekte toplar; büyük hacimde SQL'e taşınmalı. |
| "Vadesi geçti" hesaplanır | Cron/zamanlayıcı gerekmez, saklanan durum bayatlayamaz. |
| Belge silme yok | Fatura numarasında boşluk olmasın. Hata için ters kayıt (iptal/iade) ileride. |
| Sunucu tarafı render + HTMX | Tek repo, tek dağıtım, JS derlemesi yok. Bedel: zengin etkileşimler sınırlı. |

## Bilinen sınırlar

* **Resmi e-fatura/e-arşiv değildir.** GİB entegrasyonu kapsam dışı; bu bir teklif/faturalama aracıdır.
* Gerçek Supabase Auth ile giriş ve **token yenileme yok**: access token (1 saat) dolunca yeniden giriş.
* Kayıt ekranı ve üye daveti yok: kullanıcılar Supabase Auth'ta açılır, tek aktif org. `member`/`viewer`
  rolleri veritabanında hazır ama davet arayüzü yok.
* Portal ve `/demo-login` uçlarında oran sınırlama yok (token 256 bit; demo hesabı salt okunur).
* Sürüm sabitleme gevşek (`~=`); kilit dosyası yok.

## Yerelde çalıştırma

```bash
# PDF (WeasyPrint) için apt: libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core
pip install -r requirements-dev.txt
docker run -d --name tk-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
python -m app.migrations --stub        # Supabase auth taklidi + şema (Supabase'de --stub KULLANMA)
python -m app.seed --email demo@teklifkit.example --create-user   # demo veri (salt-okunur kullanıcı)
export JWT_SECRET=gelistirme-icin-en-az-32-karakterlik-secret DEV_LOGIN=1 DEMO_EMAIL=demo@teklifkit.example
uvicorn app.main:app --reload          # /login → demo hesabı ya da "Supabase olmadan giriş yap"
```

Testler (Postgres gerekir; yoksa atlanır, `REQUIRE_DB=1` ile hata verir):

```bash
export TEKLIFKIT_TEST_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/postgres
pytest -q
ruff check . && ruff format --check .
```

## Dağıtım (Supabase + Render)

`Dockerfile` ve `render.yaml` hazır (`docker-entrypoint.sh` açılışta `RUN_MIGRATIONS=1` ise migrasyon
uygular). Sıra:

1. **Supabase projesi, Render ile AYNI bölgede** (örn. ikisi Frankfurt). Bölgeler arası her sorgu turu
   yüzlerce ms ekler ve bir sayfa birkaç sorgu çalıştırır.
2. Auth'ta demo kullanıcıyı parolayla oluştur (e-posta onayı kapalı/onaylı).
3. Direct/session bağlantısıyla: `DATABASE_URL=... python -m app.migrations`, ardından
   `DATABASE_URL=... python -m app.seed --email <demo e-postası>`.
4. Render Blueprint'i bağla; `render.yaml`'daki `sync: false` değerlerini panelden gir
   (`DATABASE_URL`, `JWKS_URL`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `PUBLIC_BASE_URL`, `DEMO_*`).
5. Doğrula: `/healthz`, demo ile giriş, bir PDF, `viewer` ile yazma denemesi (403).

## Ortam değişkenleri

| Değişken | Açıklama |
| --- | --- |
| `DATABASE_URL` | Postgres bağlantısı. Migrasyon için direct/session; uygulama için pooler de olur |
| `JWKS_URL` | Supabase JWKS adresi (asimetrik anahtarlı projeler) |
| `JWT_SECRET` | HS256 secret (yerel / eski projeler); `JWKS_URL` yoksa kullanılır |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Parola ile girişte Supabase Auth çağrısı için |
| `PUBLIC_BASE_URL` | Portal bağlantılarının tabanı (örn. `https://teklifkit.onrender.com`) |
| `DEMO_EMAIL`, `DEMO_PASSWORD` | İsteğe bağlı salt-okunur demo hesabı; parola sunucuda kalır |
| `DEV_LOGIN=1` | Yalnız yerelde: Supabase'siz oturum. **Üretimde açma.** |
| `COOKIE_SECURE` | Varsayılan `1` (`DEV_LOGIN` açıkken `0`) |
| `RUN_MIGRATIONS=1` | Konteyner açılışında migrasyonları uygula |

## Dizin

```
app/                  FastAPI uygulaması (routes/, domain/, templates/, static/, seed.py)
supabase/migrations/  şema + RLS + iş kuralları + roller (SQL)
db/test_auth_stub.sql yerel/CI için auth.uid() ve rol taklidi
tests/                unit/ · db/ (RLS, tetikleyiciler, roller, şema denetimi) · web/ (uçtan uca)
docs/screenshots/     README görselleri
Dockerfile, render.yaml, docker-entrypoint.sh
```
