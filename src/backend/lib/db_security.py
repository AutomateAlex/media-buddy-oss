"""Idempotent RLS / grant hardening for the public schema.

exposure class where `create_all` births a table with RLS OFF and a default
``GRANT ALL`` to the public PostgREST roles (``anon`` / ``authenticated``) —
one RLS slip away from being world-readable via the public anon key.

Two guarantees, both idempotent, applied only to tables that actually need it
(steady-state = zero DDL, zero locks):

  1) every ``public`` table has RLS enabled;
  2) any ``public`` table with **no** RLS policy is backend-only → the public
     roles' grants are revoked. (A 0-policy table already denies those roles
     under RLS, so this changes no behaviour — it just removes the latent
     ``GRANT`` landmine, giving defence in depth.)

Tables that DO have RLS policies are frontend-facing + row-scoped
(``auth.uid() = user_id``); they are left untouched so the app keeps working.
The backend itself connects as ``service_role`` (BYPASSRLS), so none of this
affects server-side access.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Never revoke on these (intentional public reads, e.g. active pricing tiers).
_PUBLIC_READ_ALLOWLIST: set[str] = {"plan_quotas"}


def _q(ident: str) -> str:
    # pg_class identifiers are trusted; strip any embedded quote defensively.
    return '"' + ident.replace('"', "") + '"'


def harden_public_schema(engine) -> None:
    try:
        with engine.begin() as c:
            tables = {
                row[0]: row[1]
                for row in c.execute(text(
                    "select c.relname, c.relrowsecurity "
                    "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                    "where n.nspname = 'public' and c.relkind = 'r'"
                )).fetchall()
            }
            policied = set(c.execute(text(
                "select tablename from pg_policies where schemaname = 'public'"
            )).scalars())
            granted = set(c.execute(text(
                "select distinct table_name from information_schema.role_table_grants "
                "where table_schema = 'public' and grantee in ('anon', 'authenticated')"
            )).scalars())

            enabled = revoked = 0
            for name, rls_on in tables.items():
                if not rls_on:
                    c.execute(text(f"ALTER TABLE public.{_q(name)} ENABLE ROW LEVEL SECURITY"))
                    enabled += 1
                if name not in policied and name not in _PUBLIC_READ_ALLOWLIST and name in granted:
                    c.execute(text(f"REVOKE ALL ON public.{_q(name)} FROM anon, authenticated"))
                    revoked += 1

        if enabled or revoked:
            logger.info(
                "db hardening: enabled RLS on %d table(s), revoked public grants on %d backend-only table(s)",
                enabled, revoked,
            )
    except Exception:
        # Never block API startup on a hardening hiccup — log and continue.
        logger.exception("db hardening step failed (non-fatal)")
