-- M3: salt-okunur rol. `viewer` org verisini okur ama yazamaz; kamuya açık demo hesabı bu rolde.
-- Garanti RLS'te: uygulama bir yazma yolunu kapatmayı unutsa bile viewer yazamaz.

alter table public.memberships drop constraint memberships_role_check;
alter table public.memberships
  add constraint memberships_role_check check (role in ('owner', 'member', 'viewer'));

create function public.can_write(p_org uuid) returns boolean
language sql stable security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.memberships m
    where m.org_id = p_org and m.user_id = auth.uid() and m.role in ('owner', 'member')
  )
$$;
revoke all on function public.can_write(uuid) from public, anon;
grant execute on function public.can_write(uuid) to authenticated;

-- Okuma: org üyesi (viewer dahil). Yazma: yalnız owner/member.
-- Tablo başına tek tek yazmak yerine döngü; politika adları <tablo>_select/_insert/_update/_delete.
do $$
declare
  t text;
begin
  foreach t in array array[
    'clients', 'document_counters', 'documents', 'document_items', 'payments', 'portal_tokens'
  ] loop
    execute format('drop policy %I on public.%I', t || '_all', t);
    execute format(
      'create policy %I on public.%I for select to authenticated using (public.is_member(org_id))',
      t || '_select', t);
    execute format(
      'create policy %I on public.%I for insert to authenticated '
      'with check (public.can_write(org_id))', t || '_insert', t);
    execute format(
      'create policy %I on public.%I for update to authenticated '
      'using (public.can_write(org_id)) with check (public.can_write(org_id))', t || '_update', t);
    execute format(
      'create policy %I on public.%I for delete to authenticated '
      'using (public.can_write(org_id))', t || '_delete', t);
  end loop;
end
$$;

-- İşletme adını da yalnız yazma yetkisi olanlar değiştirir.
drop policy organizations_update on public.organizations;
create policy organizations_update on public.organizations
  for update to authenticated
  using (public.can_write(id)) with check (public.can_write(id));
