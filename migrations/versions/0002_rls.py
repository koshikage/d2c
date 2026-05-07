"""
Migration 0002 — PostgreSQL Row-Level Security
================================================
Adds database-level tenant isolation as Layer 3 of our defence-in-depth strategy.

WHY THIS EXISTS:
  Layer 1 (JWT) and Layer 2 (TenantSession) live in application code.
  A sufficiently bad bug, a compromised dependency, a direct psql session
  by a DBA, a future microservice that shares the DB — any of these can bypass
  application-layer checks.

  RLS enforces brand_id filtering at the Postgres storage engine level.
  Even a raw SELECT * FROM products returns only the current brand's rows.
  It cannot be bypassed by application code.

HOW IT WORKS:
  1. The app DB role (d2c_app) has RLS enforced on all tenant tables.
  2. A superuser/migration role (d2c_migrations) is EXEMPT from RLS
     (BYPASSRLS) so Alembic can run DDL freely.
  3. Before any query, the app calls:
         SET LOCAL app.current_brand_id = '<uuid>';
     (via TenantSession.activate() → set_brand_context())
  4. The RLS policy reads that variable and filters rows.

  SET LOCAL scopes to the current transaction. When the transaction ends
  (commit or rollback), the variable resets. Pooled connections cannot
  carry stale brand context from a previous request.

ROLLBACK:
  DROP POLICY then ALTER TABLE ... DISABLE ROW LEVEL SECURITY.

Revision ID: 0002_rls
Revises: 0001_initial
"""
from alembic import op

revision = "0002_rls"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

# Tables that hold per-brand data
TENANT_TABLES = [
    "products",
    "orders",
    "webhook_events",
    "ad_spend_daily",
    "shopify_connections",
    "meta_connections",
    "users",
]


def upgrade() -> None:
    # Create the restricted app role if it doesn't exist.
    # In production this is done by the Cloud SQL init script, not Alembic,
    # because Alembic runs as a superuser and we don't want it managing roles.
    # This is here as documentation of the intended role setup.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'd2c_app') THEN
                CREATE ROLE d2c_app LOGIN PASSWORD 'REPLACE_IN_PRODUCTION';
                GRANT CONNECT ON DATABASE d2c_db TO d2c_app;
                GRANT USAGE ON SCHEMA public TO d2c_app;
            END IF;
        END
        $$;
    """)

    # Grant the app role SELECT/INSERT/UPDATE/DELETE on all tenant tables
    for table in TENANT_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO d2c_app;")

    # Enable RLS on each tenant table
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        # FORCE ensures the table owner (superuser) is also subject to RLS.
        # Without FORCE, superuser queries bypass RLS.

    # Create SELECT policy for each table
    # current_setting() returns '' if not set; coerce to UUID safely with a guard.
    for table in TENANT_TABLES:
        if table == "users":
            # Users table: brand_id is the FK
            op.execute(f"""
                CREATE POLICY {table}_brand_isolation_select
                ON {table}
                FOR SELECT
                TO d2c_app
                USING (
                    brand_id = NULLIF(current_setting('app.current_brand_id', true), '')::uuid
                );
            """)
            op.execute(f"""
                CREATE POLICY {table}_brand_isolation_insert
                ON {table}
                FOR INSERT
                TO d2c_app
                WITH CHECK (
                    brand_id = NULLIF(current_setting('app.current_brand_id', true), '')::uuid
                );
            """)
            op.execute(f"""
                CREATE POLICY {table}_brand_isolation_update
                ON {table}
                FOR UPDATE
                TO d2c_app
                USING (
                    brand_id = NULLIF(current_setting('app.current_brand_id', true), '')::uuid
                )
                WITH CHECK (
                    brand_id = NULLIF(current_setting('app.current_brand_id', true), '')::uuid
                );
            """)
            op.execute(f"""
                CREATE POLICY {table}_brand_isolation_delete
                ON {table}
                FOR DELETE
                TO d2c_app
                USING (
                    brand_id = NULLIF(current_setting('app.current_brand_id', true), '')::uuid
                );
            """)
        else:
            # All other tenant tables use the same pattern
            op.execute(f"""
                CREATE POLICY {table}_brand_isolation_select
                ON {table}
                FOR SELECT
                TO d2c_app
                USING (
                    brand_id = NULLIF(current_setting('app.current_brand_id', true), '')::uuid
                );
            """)
            op.execute(f"""
                CREATE POLICY {table}_brand_isolation_write
                ON {table}
                FOR ALL
                TO d2c_app
                WITH CHECK (
                    brand_id = NULLIF(current_setting('app.current_brand_id', true), '')::uuid
                );
            """)

    # The brands table itself is NOT RLS-protected — the app needs to read
    # the brand record to validate login (before brand_id is known).
    # It is protected by application-layer checks instead.
    op.execute("GRANT SELECT ON brands TO d2c_app;")


def downgrade() -> None:
    for table in TENANT_TABLES:
        try:
            op.execute(f"DROP POLICY IF EXISTS {table}_brand_isolation_select ON {table};")
            op.execute(f"DROP POLICY IF EXISTS {table}_brand_isolation_insert ON {table};")
            op.execute(f"DROP POLICY IF EXISTS {table}_brand_isolation_update ON {table};")
            op.execute(f"DROP POLICY IF EXISTS {table}_brand_isolation_delete ON {table};")
            op.execute(f"DROP POLICY IF EXISTS {table}_brand_isolation_write ON {table};")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
        except Exception:
            pass
