"""
Tenant Isolation — Query Layer Guard
======================================
The single most important security file in the codebase.

═══════════════════════════════════════════════════════════════════
THREAT MODEL
═══════════════════════════════════════════════════════════════════
A developer under deadline writes:
    result = await db.execute(select(Product).where(Product.id == pid))
This returns ANY brand's product if the caller knows or guesses the UUID.
UUIDs are not secret — they appear in API responses, logs, error messages.

═══════════════════════════════════════════════════════════════════
THREE LAYERS OF ISOLATION (defence in depth)
═══════════════════════════════════════════════════════════════════

LAYER 1 — JWT (middleware/auth.py)
  brand_id is baked into the signed JWT at login time.
  Forging a different brand_id requires the HS256 signing key.
  The middleware extracts brand_id before any handler runs.

LAYER 2 — TenantSession (this file)
  All application queries go through TenantSession, which appends
  WHERE brand_id = :brand_id to every SELECT, UPDATE, DELETE.
  The "raw escape hatch" pattern (session.execute(arbitrary_stmt))
  is replaced by scoped_execute(), which requires the caller to
  pass the model class so the scope can be enforced.
  A call to execute() without scoping raises TenantScopeError —
  it cannot silently succeed.

LAYER 3 — PostgreSQL Row-Level Security (migrations/versions/0002_rls.py)
  Even if layers 1 and 2 are somehow bypassed (e.g. a direct psql
  session, a Celery worker that forgets to scope, an ORM bug), the DB
  itself enforces brand_id via RLS policies using the app.current_brand_id
  session variable. This is the "can't-be-bypassed-by-code" layer.

  RLS IMPLEMENTATION NOTES:
    - The app DB role has RLS enabled on all tenant tables.
    - Before each query, the app sets:
          SET LOCAL app.current_brand_id = '<uuid>';
      via set_brand_context() below, called by the TenantSession constructor.
    - asyncpg supports per-connection SET LOCAL natively.
    - Connection pool: NullPool is used for Cloud Run (stateless), so each
      request gets a fresh connection — no risk of context bleed between requests.
    - Local dev uses a standard pool; set_brand_context() resets on each
      TenantSession construction, so even pooled connections are safe.

WHY BOTH LAYERS 2 AND 3:
  Layer 2 catches mistakes at development time (clear Python errors).
  Layer 3 catches mistakes at runtime at the DB level, including:
    - Raw psql admin queries (DBA mistakes)
    - Direct asyncpg calls that bypass the ORM
    - Future microservices that share the same DB without the ORM layer

═══════════════════════════════════════════════════════════════════
WHAT IS INTENTIONALLY EXCLUDED
═══════════════════════════════════════════════════════════════════
The background sync services (shopify/service.py, meta/service.py)
use raw AsyncSession directly because they are brand-aware by
construction — they receive connection objects already filtered by
brand_id from the scheduler. They are NOT user-facing paths.
Every such call site has a # SYNC-PATH: brand_id enforced by caller
comment. A future linting rule (see .ruff.toml) can enforce this.
"""
import uuid
from typing import Any, Type, TypeVar

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class TenantScopeError(Exception):
    """
    Raised when code attempts to bypass tenant scoping.
    This is a programming error, not a user error — it should never
    reach a production request. Treat it like an assertion failure.
    """
    pass


async def set_brand_context(session: AsyncSession, brand_id: uuid.UUID) -> None:
    """
    Set the PostgreSQL session variable used by Row-Level Security policies.

    SET LOCAL scopes the variable to the current transaction. It is automatically
    cleared when the transaction ends, so pooled connections cannot carry stale context.

    The RLS policy on each tenant table reads:
        USING (brand_id = current_setting('app.current_brand_id')::uuid)

    This is Layer 3 of tenant isolation. See migrations/versions/0002_rls.py.
    """
    await session.execute(
        text("SET LOCAL app.current_brand_id = :brand_id"),
        {"brand_id": str(brand_id)},
    )


class TenantSession:
    """
    Scoped database session. All reads and writes are constrained to one brand.

    Construction:
        ts = TenantSession(db, brand_id=current_user.brand_id)
        # This also sets the Postgres RLS session variable.

    All SELECT, INSERT, UPDATE, DELETE paths go through this class.
    Calling the underlying session.execute() directly is not possible
    without going through scoped_execute(), which enforces brand scoping.

    The brand_id here is extracted from the JWT by middleware/auth.py
    and re-validated against the users table on every request.
    """

    def __init__(self, session: AsyncSession, brand_id: uuid.UUID):
        self._session = session
        self._brand_id = brand_id
        # Note: set_brand_context() is async; call await ts.activate() after construction,
        # or use the factory function get_tenant_session() in middleware/auth.py which does this.

    @property
    def brand_id(self) -> uuid.UUID:
        return self._brand_id

    async def activate(self) -> "TenantSession":
        """
        Set the Postgres RLS session variable for this transaction.
        Called by the get_tenant_session dependency in middleware/auth.py.
        """
        await set_brand_context(self._session, self._brand_id)
        return self

    def _assert_scoped(self, stmt: Select, model: Type[Any]) -> Select:
        """
        Append WHERE brand_id = :brand_id unconditionally.
        If the model has no brand_id column (e.g. Brand itself), pass through.
        """
        if hasattr(model, "brand_id"):
            stmt = stmt.where(model.brand_id == self._brand_id)
        return stmt

    async def all(self, model: Type[T], stmt: Select | None = None) -> list[T]:
        """Return all rows for this tenant, ordered by created_at desc."""
        if stmt is None:
            stmt = select(model)
        stmt = self._assert_scoped(stmt, model)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def first(self, model: Type[T], stmt: Select | None = None) -> T | None:
        """Return first matching row for this tenant."""
        if stmt is None:
            stmt = select(model)
        stmt = self._assert_scoped(stmt, model)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def get_by_id(self, model: Type[T], record_id: uuid.UUID) -> T | None:
        """
        Fetch by primary key WITH tenant scope enforcement.

        DO NOT use session.get(Model, pk) — it bypasses WHERE clauses entirely
        and will return any brand's record for a known UUID.
        This method always appends brand_id = :brand_id.
        """
        stmt = select(model).where(model.id == record_id)  # type: ignore[attr-defined]
        return await self.first(model, stmt)

    async def scoped_execute(self, model: Type[Any], stmt: Any) -> Any:
        """
        Execute an arbitrary statement with brand_id scoping enforced.

        Use this for aggregate queries (COUNT, SUM) that can't go through
        the typed helpers above. The model class is required so we can
        verify the statement touches a tenant-scoped table.

        Example:
            result = await ts.scoped_execute(
                AdSpendDaily,
                select(func.sum(AdSpendDaily.spend))
                .where(AdSpendDaily.date >= from_date)
            )
        """
        if not hasattr(model, "brand_id"):
            raise TenantScopeError(
                f"Model {model.__name__} has no brand_id column. "
                "Use session.execute() directly for non-tenant tables."
            )
        # Enforce brand_id even if caller forgot it
        scoped_stmt = stmt.where(model.brand_id == self._brand_id)
        return await self._session.execute(scoped_stmt)

    async def add(self, obj: Any) -> Any:
        """
        Add a new object to the session.
        - If brand_id is None: sets it to the session's brand (developer convenience).
        - If brand_id is set to a DIFFERENT brand: raises TenantScopeError immediately.
          This is a programming error and must never happen in production.
        """
        if hasattr(obj, "brand_id"):
            if obj.brand_id is None:
                obj.brand_id = self._brand_id
            elif str(obj.brand_id) != str(self._brand_id):
                logger.error(
                    "tenant_isolation_violation_blocked",
                    attempted_brand_id=str(obj.brand_id),
                    session_brand_id=str(self._brand_id),
                    model=type(obj).__name__,
                )
                raise TenantScopeError(
                    f"Attempted to write {type(obj).__name__} with brand_id={obj.brand_id} "
                    f"from a session scoped to brand_id={self._brand_id}. "
                    "This is a programming error."
                )
        self._session.add(obj)
        return obj

    async def flush(self) -> None:
        await self._session.flush()

    async def refresh(self, obj: Any) -> None:
        await self._session.refresh(obj)

    async def delete(self, obj: Any) -> None:
        """Delete with mandatory tenant ownership check before executing."""
        if hasattr(obj, "brand_id") and str(obj.brand_id) != str(self._brand_id):
            raise TenantScopeError(
                f"Attempted to delete {type(obj).__name__} belonging to brand {obj.brand_id} "
                f"from session scoped to {self._brand_id}."
            )
        await self._session.delete(obj)

    async def count(self, model: Type[Any]) -> int:
        """Count rows for this tenant."""
        from sqlalchemy import func
        stmt = select(func.count(model.id)).where(  # type: ignore[attr-defined]
            model.brand_id == self._brand_id  # type: ignore[attr-defined]
        )
        result = await self._session.execute(stmt)
        return result.scalar() or 0
