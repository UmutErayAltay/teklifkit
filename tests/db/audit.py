"""Şema denetimi: Supabase'e özgü yaygın RLS/yetki açıklarını katalogdan yakalar.

Her fonksiyon bulguların (okunur metin) listesini döndürür; boş liste = temiz.
Vault dersleri: "always-true" politika açığı, SECURITY DEFINER'ın PUBLIC'e açık EXECUTE'u.
"""

import psycopg


def tables_without_rls(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind in ('r', 'p') and not c.relrowsecurity"
    )
    return [f"public.{r[0]}: RLS kapalı" for r in rows]


def always_true_policies(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "select tablename, policyname, qual, with_check from pg_policies "
        "where schemaname = 'public'"
    )
    findings = []
    for table, policy, qual, check in rows:
        for label, expr in (("USING", qual), ("WITH CHECK", check)):
            if expr is not None and expr.strip("() ").lower() == "true":
                findings.append(f"public.{table}.{policy}: {label}(true)")
    return findings


def anon_table_privileges(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "select c.relname, p.priv from pg_class c "
        "join pg_namespace n on n.oid = c.relnamespace "
        "cross join (values ('select'), ('insert'), ('update'), ('delete')) as p(priv) "
        "where n.nspname = 'public' and c.relkind in ('r', 'p', 'v') "
        "and has_table_privilege('anon', c.oid, p.priv)"
    )
    return [f"public.{r[0]}: anon {r[1]} yetkisi var" for r in rows]


def exposed_functions(conn: psycopg.Connection) -> list[str]:
    """public şemasındaki her fonksiyon: PUBLIC/anon EXECUTE'u olmamalı; SECURITY DEFINER ise
    search_path sabit olmalı."""
    rows = conn.execute(
        "select p.proname, p.prosecdef, coalesce(p.proconfig, '{}'), "
        "has_function_privilege('anon', p.oid, 'execute'), "
        "coalesce((select bool_or(a.grantee = 0) from aclexplode(p.proacl) a), true) "
        "from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'public'"
    )
    findings = []
    for name, secdef, config, anon_exec, public_exec in rows:
        if public_exec:
            findings.append(f"public.{name}(): PUBLIC EXECUTE açık")
        if anon_exec:
            findings.append(f"public.{name}(): anon EXECUTE açık")
        if secdef and not any(c.startswith("search_path=") for c in config):
            findings.append(f"public.{name}(): SECURITY DEFINER ama search_path sabit değil")
    return findings


# authenticated'ın çağırabileceği public fonksiyonların tam listesi (açık allowlist).
AUTHENTICATED_FUNCTIONS = {"is_member", "can_write", "create_organization", "next_document_number"}


def function_scope(conn: psycopg.Connection) -> list[str]:
    """authenticated yalnız allowlist'tekileri, portal rolü yalnız portal_* fonksiyonlarını
    çağırabilir. Yeni bir fonksiyon eklenip yanlışlıkla açılırsa burada kırmızı olur."""
    rows = conn.execute(
        "select p.proname, has_function_privilege('authenticated', p.oid, 'execute'), "
        "has_function_privilege('portal', p.oid, 'execute') "
        "from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'public'"
    )
    findings = []
    for name, auth_exec, portal_exec in rows:
        if auth_exec and name not in AUTHENTICATED_FUNCTIONS:
            findings.append(f"public.{name}(): authenticated EXECUTE var ama allowlist'te değil")
        if portal_exec and not name.startswith("portal_"):
            findings.append(f"public.{name}(): portal rolü portal_* dışı fonksiyon çağırabiliyor")
        if name.startswith("portal_") and auth_exec:
            findings.append(f"public.{name}(): portal fonksiyonu authenticated'a açık")
    return findings


def portal_table_privileges(conn: psycopg.Connection) -> list[str]:
    """portal rolünün public'teki hiçbir tabloda/görünümde yetkisi olmamalı."""
    rows = conn.execute(
        "select c.relname, p.priv from pg_class c "
        "join pg_namespace n on n.oid = c.relnamespace "
        "cross join (values ('select'), ('insert'), ('update'), ('delete')) as p(priv) "
        "where n.nspname = 'public' and c.relkind in ('r', 'p', 'v') "
        "and has_table_privilege('portal', c.oid, p.priv)"
    )
    return [f"public.{r[0]}: portal {r[1]} yetkisi var" for r in rows]


def write_policies_not_gated_by_can_write(conn: psycopg.Connection) -> list[str]:
    """INSERT/UPDATE/DELETE/ALL politikaları can_write() kullanmalı; yalnız is_member() olan bir
    yazma politikası salt-okunur (viewer) rolüne yazma izni verir."""
    rows = conn.execute(
        "select tablename, policyname, cmd, coalesce(qual, ''), coalesce(with_check, '') "
        "from pg_policies where schemaname = 'public' and cmd in ('INSERT','UPDATE','DELETE','ALL')"
    )
    findings = []
    for table, policy, cmd, qual, check in rows:
        expr = qual + " " + check
        if "can_write(" not in expr:
            findings.append(f"public.{table}.{policy}: {cmd} politikası can_write() kullanmıyor")
    return findings


ALL_CHECKS = (
    tables_without_rls,
    always_true_policies,
    anon_table_privileges,
    exposed_functions,
    function_scope,
    portal_table_privileges,
    write_policies_not_gated_by_can_write,
)


def run_all(conn: psycopg.Connection) -> list[str]:
    return [finding for check in ALL_CHECKS for finding in check(conn)]
