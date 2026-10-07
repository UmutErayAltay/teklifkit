-- teklifkit çekirdek şema: organizasyonlar, müşteriler, teklif/fatura, ödemeler.
-- Tasarım ilkeleri:
--   * Her tenant tablosunda RLS açık; politikalar is_member(org_id) üzerinden.
--   * anon rolünün hiçbir tabloya yetkisi yok (Supabase varsayılan yetkileri geri alınır).
--   * SECURITY DEFINER fonksiyonlar: search_path sabit, EXECUTE yalnız authenticated.
--   * Bileşik FK'lar (id, org_id) çocuk satırın başka bir org'un satırına bağlanmasını imkânsız kılar.

create schema if not exists private;
revoke all on schema private from public;

-- ---------------------------------------------------------------- organizasyon
create table public.organizations (
  id uuid primary key default gen_random_uuid(),
  name text not null check (length(btrim(name)) between 1 and 120),
  created_at timestamptz not null default now()
);

create table public.memberships (
  org_id uuid not null references public.organizations (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  role text not null default 'member' check (role in ('owner', 'member')),
  created_at timestamptz not null default now(),
  primary key (org_id, user_id)
);
create index memberships_user_idx on public.memberships (user_id);

-- Üyelik kontrolü. SECURITY DEFINER: memberships politikasında özyinelemeyi önler.
create function public.is_member(p_org uuid) returns boolean
language sql stable security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.memberships m
    where m.org_id = p_org and m.user_id = auth.uid()
  )
$$;
revoke all on function public.is_member(uuid) from public, anon;
grant execute on function public.is_member(uuid) to authenticated;

-- Organizasyon + owner üyeliği tek adımda (doğrudan insert politikası bilerek yok).
create function public.create_organization(p_name text) returns uuid
language plpgsql security definer
set search_path = ''
as $$
declare
  v_user uuid := auth.uid();
  v_org uuid;
begin
  if v_user is null then
    raise exception 'not authenticated' using errcode = '28000';
  end if;
  insert into public.organizations (name) values (p_name) returning id into v_org;
  insert into public.memberships (org_id, user_id, role) values (v_org, v_user, 'owner');
  return v_org;
end
$$;
revoke all on function public.create_organization(text) from public, anon;
grant execute on function public.create_organization(text) to authenticated;

-- --------------------------------------------------------------------- müşteriler
create table public.clients (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.organizations (id) on delete cascade,
  name text not null check (length(btrim(name)) between 1 and 200),
  email text,
  address text,
  tax_id text,
  created_at timestamptz not null default now(),
  unique (id, org_id)
);
create index clients_org_idx on public.clients (org_id);

-- ------------------------------------------------------------ teklif / fatura
create table public.document_counters (
  org_id uuid not null references public.organizations (id) on delete cascade,
  kind text not null check (kind in ('quote', 'invoice')),
  last_number integer not null,
  primary key (org_id, kind)
);

create table public.documents (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.organizations (id) on delete cascade,
  kind text not null check (kind in ('quote', 'invoice')),
  number integer not null,
  status text not null default 'draft',
  client_id uuid,
  currency text not null default 'TRY' check (currency in ('TRY', 'EUR', 'USD')),
  issue_date date not null default current_date,
  due_date date,
  notes text,
  converted_from_id uuid,
  created_at timestamptz not null default now(),
  unique (id, org_id),
  unique (org_id, kind, number),
  foreign key (client_id, org_id) references public.clients (id, org_id),
  foreign key (converted_from_id, org_id) references public.documents (id, org_id),
  check (
    (kind = 'quote' and status in ('draft', 'sent', 'accepted', 'rejected'))
    or (kind = 'invoice' and status in ('draft', 'sent', 'paid', 'overdue'))
  )
);
create index documents_org_idx on public.documents (org_id, created_at desc);

create table public.document_items (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null,
  document_id uuid not null,
  position integer not null,
  description text not null check (length(btrim(description)) > 0),
  qty numeric(12, 3) not null check (qty > 0),
  unit_price numeric(12, 2) not null check (unit_price >= 0),
  vat_rate smallint not null check (vat_rate in (0, 1, 10, 20)),
  foreign key (document_id, org_id) references public.documents (id, org_id) on delete cascade
);
create index document_items_doc_idx on public.document_items (document_id, position);

create table public.payments (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null,
  document_id uuid not null,
  amount numeric(12, 2) not null check (amount > 0),
  paid_on date not null default current_date,
  method text,
  note text,
  created_at timestamptz not null default now(),
  foreign key (document_id, org_id) references public.documents (id, org_id) on delete cascade
);
create index payments_doc_idx on public.payments (document_id);

-- Ardışık numara: tek ifadede upsert, satır kilidiyle eşzamanlılığa dayanıklı.
-- SECURITY INVOKER: RLS çağıranın org'u için geçerli kalır.
create function public.next_document_number(p_org uuid, p_kind text) returns integer
language sql volatile
set search_path = ''
as $$
  insert into public.document_counters as c (org_id, kind, last_number)
  values (p_org, p_kind, 1)
  on conflict (org_id, kind) do update set last_number = c.last_number + 1
  returning last_number
$$;
revoke all on function public.next_document_number(uuid, text) from public, anon;
grant execute on function public.next_document_number(uuid, text) to authenticated;

-- ---------------------------------------------------------------------- RLS
alter table public.organizations enable row level security;
alter table public.memberships enable row level security;
alter table public.clients enable row level security;
alter table public.document_counters enable row level security;
alter table public.documents enable row level security;
alter table public.document_items enable row level security;
alter table public.payments enable row level security;

create policy organizations_select on public.organizations
  for select to authenticated using (public.is_member(id));
create policy organizations_update on public.organizations
  for update to authenticated using (public.is_member(id)) with check (public.is_member(id));

-- Üyelik yazma politikası yok: yalnız create_organization() üyelik oluşturur.
create policy memberships_select on public.memberships
  for select to authenticated
  using (user_id = auth.uid() or public.is_member(org_id));

create policy clients_all on public.clients
  for all to authenticated
  using (public.is_member(org_id)) with check (public.is_member(org_id));
create policy document_counters_all on public.document_counters
  for all to authenticated
  using (public.is_member(org_id)) with check (public.is_member(org_id));
create policy documents_all on public.documents
  for all to authenticated
  using (public.is_member(org_id)) with check (public.is_member(org_id));
create policy document_items_all on public.document_items
  for all to authenticated
  using (public.is_member(org_id)) with check (public.is_member(org_id));
create policy payments_all on public.payments
  for all to authenticated
  using (public.is_member(org_id)) with check (public.is_member(org_id));

-- ----------------------------------------------------------------- yetkiler
-- Supabase varsayılan olarak yeni tablolara anon'a da yetki verir; açıkça geri al.
revoke all on public.organizations, public.memberships, public.clients,
  public.document_counters, public.documents, public.document_items, public.payments
  from public, anon;

grant usage on schema public to authenticated;
grant select, update on public.organizations to authenticated;
grant select on public.memberships to authenticated;
grant select, insert, update, delete on public.clients, public.document_counters,
  public.documents, public.document_items, public.payments to authenticated;
