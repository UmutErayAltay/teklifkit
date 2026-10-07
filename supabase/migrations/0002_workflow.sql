-- M2: iş akışı kuralları veritabanında + müşteri portalı.
-- Uygulama katmanı da aynı kuralları denetler ama asıl garanti burada: RLS'e tabi bir kullanıcı
-- (ya da koddaki bir hata) gönderilmiş bir faturayı değiştiremez, geçersiz durum geçişi yapamaz.

-- Bir teklif en fazla bir faturaya dönüşür (çift tıklama / yarış durumuna karşı).
create unique index documents_one_invoice_per_quote
  on public.documents (converted_from_id) where converted_from_id is not null;

-- ------------------------------------------------------------ durum makinesi
-- Tetikleyici fonksiyonlar tetikleme sırasında EXECUTE yetkisi gerektirmez; kimseye açma.
create function public.documents_guard() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.org_id <> old.org_id or new.kind <> old.kind or new.number <> old.number
     or new.converted_from_id is distinct from old.converted_from_id then
    raise exception 'document identity columns are immutable' using errcode = '23514';
  end if;

  if new.status <> old.status and not (
    (old.kind = 'quote' and (old.status, new.status) in
      (('draft', 'sent'), ('sent', 'accepted'), ('sent', 'rejected')))
    or
    (old.kind = 'invoice' and (old.status, new.status) in
      (('draft', 'sent'), ('sent', 'paid'), ('paid', 'sent')))
  ) then
    raise exception 'invalid status transition: % -> %', old.status, new.status
      using errcode = '23514';
  end if;

  -- Gönderilmiş belgenin tarafları ve tutar çerçevesi değişmez.
  if old.status <> 'draft' and (
    new.client_id is distinct from old.client_id
    or new.currency <> old.currency
    or new.issue_date <> old.issue_date
  ) then
    raise exception 'sent documents are immutable' using errcode = '23514';
  end if;
  return new;
end
$$;
revoke all on function public.documents_guard() from public, anon, authenticated;
create trigger documents_guard before update on public.documents
  for each row execute function public.documents_guard();

-- Kalemler yalnız taslakta değişebilir.
create function public.document_items_guard() returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_status text;
begin
  if tg_op = 'UPDATE' and (new.document_id <> old.document_id or new.org_id <> old.org_id) then
    raise exception 'items cannot move between documents' using errcode = '23514';
  end if;
  select d.status into v_status from public.documents d
  where d.id = coalesce(new.document_id, old.document_id)
    and d.org_id = coalesce(new.org_id, old.org_id);
  -- Belge zaten silinmişse (cascade) v_status null'dır: izin ver.
  if v_status is not null and v_status <> 'draft' then
    raise exception 'items of a sent document are immutable' using errcode = '23514';
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end
$$;
revoke all on function public.document_items_guard() from public, anon, authenticated;
create trigger document_items_guard before insert or update or delete on public.document_items
  for each row execute function public.document_items_guard();

-- Ödeme yalnız gönderilmiş faturaya eklenir; kayıt eklendikten sonra tutarı değişmez.
create function public.payments_guard() returns trigger
language plpgsql
set search_path = ''
as $$
declare
  v_kind text;
  v_status text;
begin
  if tg_op = 'UPDATE' then
    raise exception 'payments are immutable; delete and re-create' using errcode = '23514';
  end if;
  select d.kind, d.status into v_kind, v_status from public.documents d
  where d.id = new.document_id and d.org_id = new.org_id;
  if v_kind is distinct from 'invoice' or v_status <> 'sent' then
    raise exception 'payments can only be added to a sent invoice' using errcode = '23514';
  end if;
  return new;
end
$$;
revoke all on function public.payments_guard() from public, anon, authenticated;
create trigger payments_guard before insert or update on public.payments
  for each row execute function public.payments_guard();

-- ------------------------------------------------------------ müşteri portalı
-- Portal ayrı, hiçbir tablo yetkisi olmayan bir rolle çalışır: yalnız aşağıdaki iki fonksiyonu
-- çağırabilir. Portal kodunda bir SQL hatası olsa bile başka kiracının verisine ulaşamaz.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'portal') then
    create role portal nologin noinherit;
  end if;
end
$$;
grant portal to current_user;
grant usage on schema public to portal;

create table public.portal_tokens (
  token_hash text primary key check (token_hash ~ '^[0-9a-f]{64}$'),
  org_id uuid not null,
  document_id uuid not null,
  created_by uuid default auth.uid(),
  expires_at timestamptz not null,
  revoked_at timestamptz,
  created_at timestamptz not null default now(),
  foreign key (document_id, org_id) references public.documents (id, org_id) on delete cascade
);
create index portal_tokens_doc_idx on public.portal_tokens (document_id);
alter table public.portal_tokens enable row level security;
create policy portal_tokens_all on public.portal_tokens
  for all to authenticated
  using (public.is_member(org_id)) with check (public.is_member(org_id));
revoke all on public.portal_tokens from public, anon;
grant select, insert, update on public.portal_tokens to authenticated;

-- Token'ın kendisi saklanmaz; yalnız SHA-256 özeti. Taslak belge portalda görünmez.
create function public.portal_document(p_hash text) returns jsonb
language sql stable security definer
set search_path = ''
as $$
  select jsonb_build_object(
    'id', d.id,
    'kind', d.kind,
    'number', d.number,
    'status', d.status,
    'currency', d.currency,
    'issue_date', d.issue_date,
    'due_date', d.due_date,
    'notes', d.notes,
    'org_name', o.name,
    'client', case when c.id is null then null else jsonb_build_object(
      'name', c.name, 'email', c.email, 'address', c.address, 'tax_id', c.tax_id) end,
    'items', coalesce((
      select jsonb_agg(jsonb_build_object(
        'description', i.description, 'qty', i.qty::text,
        'unit_price', i.unit_price::text, 'vat_rate', i.vat_rate) order by i.position)
      from public.document_items i where i.document_id = d.id and i.org_id = d.org_id
    ), '[]'::jsonb),
    'paid', coalesce((
      select sum(p.amount) from public.payments p
      where p.document_id = d.id and p.org_id = d.org_id
    ), 0)::text
  )
  from public.portal_tokens t
  join public.documents d on d.id = t.document_id and d.org_id = t.org_id
  join public.organizations o on o.id = d.org_id
  left join public.clients c on c.id = d.client_id and c.org_id = d.org_id
  where t.token_hash = p_hash
    and t.revoked_at is null
    and t.expires_at > now()
    and d.status <> 'draft'
$$;

-- Müşteri teklifi kabul/red eder; yalnız gönderilmiş teklif için. Sonuç: yeni durum ya da null.
create function public.portal_respond(p_hash text, p_decision text) returns text
language plpgsql volatile security definer
set search_path = ''
as $$
declare
  v_doc uuid;
  v_org uuid;
begin
  if p_decision not in ('accepted', 'rejected') then
    raise exception 'invalid decision' using errcode = '22023';
  end if;
  select d.id, d.org_id into v_doc, v_org
  from public.portal_tokens t
  join public.documents d on d.id = t.document_id and d.org_id = t.org_id
  where t.token_hash = p_hash
    and t.revoked_at is null
    and t.expires_at > now()
    and d.kind = 'quote'
    and d.status = 'sent'
  for update of d;
  if v_doc is null then
    return null;
  end if;
  update public.documents set status = p_decision where id = v_doc and org_id = v_org;
  return p_decision;
end
$$;

revoke all on function public.portal_document(text) from public, anon, authenticated;
revoke all on function public.portal_respond(text, text) from public, anon, authenticated;
grant execute on function public.portal_document(text) to portal;
grant execute on function public.portal_respond(text, text) to portal;
