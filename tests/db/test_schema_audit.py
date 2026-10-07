"""Şema denetimi hem gerçek şemada temiz çıkmalı hem de bilerek kırılmış şemada kırmızı olmalı
(denetimin kendisinin test edilmesi: sessizce her şeyi 'temiz' bulan bir denetim işe yaramaz)."""

import psycopg
import pytest

from tests.db import audit


@pytest.fixture
def conn(database_url):
    with psycopg.connect(database_url) as c:
        yield c
        c.rollback()  # denetim testlerinin yarattığı bozuk nesneler asla kalıcı olmaz


def test_real_schema_is_clean(conn):
    assert audit.run_all(conn) == []


def test_detects_table_without_rls(conn):
    conn.execute("create table public.audit_probe (id int)")
    assert "public.audit_probe: RLS kapalı" in audit.tables_without_rls(conn)


def test_detects_always_true_policy(conn):
    conn.execute("create table public.audit_probe (id int)")
    conn.execute("alter table public.audit_probe enable row level security")
    conn.execute("create policy leaky on public.audit_probe for all using (true) with check (true)")
    findings = audit.always_true_policies(conn)
    assert "public.audit_probe.leaky: USING(true)" in findings
    assert "public.audit_probe.leaky: WITH CHECK(true)" in findings


def test_detects_anon_privilege(conn):
    conn.execute("create table public.audit_probe (id int)")
    conn.execute("grant select on public.audit_probe to anon")
    assert "public.audit_probe: anon select yetkisi var" in audit.anon_table_privileges(conn)


def test_detects_exposed_security_definer(conn):
    conn.execute(
        "create function public.audit_probe_fn() returns int language sql security definer "
        "as 'select 1'"
    )
    findings = audit.exposed_functions(conn)
    assert "public.audit_probe_fn(): PUBLIC EXECUTE açık" in findings
    assert "public.audit_probe_fn(): SECURITY DEFINER ama search_path sabit değil" in findings


def test_detects_function_outside_allowlist_granted_to_authenticated(conn):
    conn.execute(
        "create function public.audit_probe_fn() returns int language sql "
        "set search_path = '' as 'select 1'"
    )
    conn.execute("revoke all on function public.audit_probe_fn() from public, anon")
    assert audit.function_scope(conn) == []  # authenticated'a verilmediyse temiz
    conn.execute("grant execute on function public.audit_probe_fn() to authenticated")
    assert any("audit_probe_fn" in f and "allowlist" in f for f in audit.function_scope(conn))


def test_detects_portal_role_calling_non_portal_function(conn):
    conn.execute(
        "create function public.audit_probe_fn() returns int language sql "
        "set search_path = '' as 'select 1'"
    )
    conn.execute("revoke all on function public.audit_probe_fn() from public, anon")
    conn.execute("grant execute on function public.audit_probe_fn() to portal")
    assert any("audit_probe_fn" in f and "portal" in f for f in audit.function_scope(conn))


def test_detects_portal_table_privilege(conn):
    conn.execute("create table public.audit_probe (id int)")
    conn.execute("grant select on public.audit_probe to portal")
    assert "public.audit_probe: portal select yetkisi var" in audit.portal_table_privileges(conn)
