"""
STEP 6 — Data Models (Schema Design)
======================================
This is the most important architectural decision in the project.
Every model has brand_id as a foreign key. Tenant isolation is enforced here.

═══════════════════════════════════════════════════════════════════════════════
MULTI-TENANCY STRATEGY: Shared Schema with brand_id Discriminator
═══════════════════════════════════════════════════════════════════════════════

We chose "shared schema" (one table for all tenants) over:
  a) Separate databases per tenant
  b) Separate PostgreSQL schemas (search_path) per tenant

WHY shared schema:
  PROS:
    + Simple operations: one DB to backup, migrate, monitor
    + New tenants are instant (just insert a Brand row)
    + Efficient for small-to-medium tenants (thousands of brands, millions of rows)
    + Phase 2 (parent company) is just a new table + FK, no schema rewrites

  CONS:
    - "Noisy neighbour" risk: one tenant's heavy query can slow others
      (mitigate with pg_stat_statements, connection limits per role)
    - A buggy query that omits brand_id WHERE clause leaks cross-tenant data
      (mitigate: query layer enforcement — see services/base.py)
    - Row-level security (RLS) in Postgres is another mitigation but adds complexity

WHY NOT separate schemas per tenant:
  - Schema per tenant → Alembic migrations must run N times (one per tenant)
  - Adding a new tenant requires a DDL migration, not just an INSERT
  - Hundreds/thousands of tenants = hundreds/thousands of schemas → Postgres catalog bloat

═══════════════════════════════════════════════════════════════════════════════
PHASE 2: PARENT COMPANY DESIGN (documented as required)
═══════════════════════════════════════════════════════════════════════════════

To support a ParentCompany owning multiple Brands without schema rewrites:

  1. Add a `parent_company_id` nullable FK column to Brand (already in settings JSONB
     for Phase 1 — can be promoted to a real FK column in a single ADD COLUMN migration).
  2. Add a ParentCompany model with its own settings JSONB.
  3. User membership stays Brand-scoped (a user logs in to a specific brand).
  4. Add a ParentUser model for parent-company-level admin users who can switch brand context.
  5. The JWT can carry `parent_company_id` as an additional claim for parent admins.

No existing table changes. No data migration. Just additive DDL.
"""
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    UUID,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
# TENANT ROOT
# ══════════════════════════════════════════════════════════════════════════════

class Brand(Base):
    """
    The tenant root. Every other model hangs off brand_id.

    settings JSONB stores flexible per-brand config:
      - timezone, currency, fiscal_year_start
      - feature_flags
      - parent_company_id (Phase 2 — will become a real FK column)
      - notification preferences

    INDEX STRATEGY:
      - GIN index on settings: enables fast JSONB key/value lookups
        e.g. WHERE settings @> '{"timezone": "UTC"}' uses the GIN index.
        Without GIN, every JSONB predicate is a full table scan.
      - Partial index on (domain) WHERE is_active = true:
        The overwhelming majority of brand lookups filter active brands.
        A partial index is smaller and faster than a full index.

    WHY JSONB for settings vs normalised columns:
      JSONB: schema-free, no migration needed to add a new setting,
             GIN-indexable for arbitrary key queries.
      Normalised: type-safe, FK constraints, JOIN-able.
      Decision: settings is truly variable and config-like. Columns like
      `timezone` could be normalised but settings grow faster than schema
      change tolerance. Promote to columns only when queried in GROUP BY / JOIN.
    """
    __tablename__ = "brands"
    __table_args__ = (
        # GIN index: powers WHERE settings @> '{"key": "value"}' queries
        Index("ix_brands_settings_gin", "settings", postgresql_using="gin"),
        # Partial index: login/lookup queries always filter is_active=true
        Index("ix_brands_domain_active", "domain", postgresql_where=text("is_active = true")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Flexible config. Promoted to columns when needed in JOINs/GROUP BY.
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Relationships
    users: Mapped[list["User"]] = relationship("User", back_populates="brand", lazy="noload")
    shopify_connection: Mapped["ShopifyConnection | None"] = relationship(
        "ShopifyConnection", back_populates="brand", uselist=False, lazy="noload"
    )
    meta_connection: Mapped["MetaConnection | None"] = relationship(
        "MetaConnection", back_populates="brand", uselist=False, lazy="noload"
    )
    products: Mapped[list["Product"]] = relationship("Product", back_populates="brand", lazy="noload")
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="brand", lazy="noload")
    ad_spend_records: Mapped[list["AdSpendDaily"]] = relationship("AdSpendDaily", back_populates="brand", lazy="noload")


class User(Base):
    """
    Application users. Each user belongs to EXACTLY ONE brand.

    WHY single-brand membership (Phase 1):
      Simpler JWT: brand_id is unambiguous.
      Phase 2 will add a ParentUser model for cross-brand admin access.

    SECURITY NOTE: brand_id here is the source of truth for tenant scope.
    It's read from the DB on login and baked into the JWT. Even if a user
    somehow crafts a JWT with a different brand_id, our middleware re-validates
    that the JWT's brand_id matches the DB user's brand_id.
    """
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    brand: Mapped["Brand"] = relationship("Brand", back_populates="users")


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION CONNECTIONS
# ══════════════════════════════════════════════════════════════════════════════

class ShopifyConnection(Base):
    """
    Stores OAuth connection state for Shopify, one per brand.

    access_token_enc: encrypted with Fernet (see core/encryption.py).
    oauth_state: the random state parameter used in OAuth CSRF protection.
    last_synced_at: drives incremental sync (only pull data newer than this).

    WHY uselist=False on the relationship:
      One brand → one Shopify store. UniqueConstraint enforces this at DB level.
    """
    __tablename__ = "shopify_connections"
    __table_args__ = (UniqueConstraint("brand_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    shop_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    access_token_enc: Mapped[str | None] = mapped_column(Text)         # encrypted at rest
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    oauth_state: Mapped[str | None] = mapped_column(String(255))       # CSRF token
    is_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    brand: Mapped["Brand"] = relationship("Brand", back_populates="shopify_connection")


class MetaConnection(Base):
    """
    Stores OAuth connection state for Meta Ads. Same shape as Shopify.
    ad_account_id: the Meta Ad Account ID (act_XXXXXXXX) connected.
    """
    __tablename__ = "meta_connections"
    __table_args__ = (UniqueConstraint("brand_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    ad_account_id: Mapped[str | None] = mapped_column(String(255))
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    oauth_state: Mapped[str | None] = mapped_column(String(255))
    is_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    brand: Mapped["Brand"] = relationship("Brand", back_populates="meta_connection")


# ══════════════════════════════════════════════════════════════════════════════
# SHOPIFY DATA
# ══════════════════════════════════════════════════════════════════════════════

class Product(Base):
    """
    Shopify product. brand_id is always set.

    external_id: Shopify's product GID — used for idempotency.
    UNIQUE(brand_id, external_id) ensures a second sync doesn't duplicate products.

    WHY NOT use external_id as primary key:
      We control our own UUIDs. External IDs from Shopify are strings of unknown format.
    """
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("brand_id", "external_id", name="uq_product_brand_external"),
        Index("ix_products_brand_id", "brand_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(255))
    product_type: Mapped[str | None] = mapped_column(String(255))
    price: Mapped[float | None] = mapped_column(Float)
    inventory_quantity: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(50), default="active")
    raw_data: Mapped[dict | None] = mapped_column(JSONB)  # full Shopify response preserved
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    brand: Mapped["Brand"] = relationship("Brand", back_populates="products")


class Order(Base):
    """
    Shopify order. Can arrive via background sync OR webhook.

    IDEMPOTENCY:
      UNIQUE(brand_id, external_id) — processing the same order twice is safe.
      The upsert logic in the sync service uses ON CONFLICT DO UPDATE.

    webhook_received: True if this order came in via webhook (useful for analytics).
    """
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("brand_id", "external_id", name="uq_order_brand_external"),
        Index("ix_orders_brand_id", "brand_id"),
        Index("ix_orders_ordered_at", "ordered_at"),  # for date-range queries
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    order_number: Mapped[str | None] = mapped_column(String(100))
    total_price: Mapped[float] = mapped_column(Float, default=0.0)
    subtotal_price: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    financial_status: Mapped[str | None] = mapped_column(String(50))
    fulfillment_status: Mapped[str | None] = mapped_column(String(50))
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    webhook_received: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_data: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    brand: Mapped["Brand"] = relationship("Brand", back_populates="orders")


class WebhookEvent(Base):
    """
    Stores raw webhook payloads for audit, replay, and debugging.

    WHY store raw payloads:
      - If processing fails, we can replay without re-requesting from Shopify
      - Audit trail of what events arrived and when
      - Idempotency: check processed=True before re-processing

    DESIGN NOTE: We persist first, process second. This means even if the
    processing logic crashes, the event is not lost.
    """
    __tablename__ = "webhook_events"
    __table_args__ = (Index("ix_webhook_events_external_id", "external_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)   # "shopify" | "meta"
    event_type: Mapped[str] = mapped_column(String(100), nullable=False) # "orders/created"
    external_id: Mapped[str | None] = mapped_column(String(255))         # order GID etc.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processing_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ══════════════════════════════════════════════════════════════════════════════
# NORMALISED AD SPEND (Shopify + Meta unified)
# ══════════════════════════════════════════════════════════════════════════════

class AdSpendDaily(Base):
    """
    Normalised daily ad spend — the unified analytics table.

    SCHEMA DESIGN RATIONALE:
      platform: "meta" | "shopify" | "google" — extensible without schema change.
      date: proper DATE column (not String). Enables native date arithmetic,
            range operators (BETWEEN, >=), and correct index range scans.
            Storing as String("YYYY-MM-DD") would make date arithmetic require
            casts on every query — a performance footgun.

    INDEX STRATEGY:
      Primary query pattern: WHERE brand_id = X AND date BETWEEN :from AND :to
      The composite index (brand_id, date) supports this exactly:
        - brand_id equality narrows to one tenant
        - date range scan on the remaining rows
      Without this index: full table scan across ALL brands for every analytics query.

      Covering index for the spend-vs-revenue query:
        (brand_id, date) INCLUDE (spend, revenue, impressions, clicks)
        Postgres can satisfy the entire query from the index (index-only scan)
        without touching the heap pages. This matters at scale (millions of rows).

    IDEMPOTENCY:
      UNIQUE(brand_id, platform, campaign_id, date) — safe to re-sync any day.
      The upsert service uses ON CONFLICT DO UPDATE so the latest data wins.

    UNIFIED SCHEMA:
      Meta rows:    spend > 0, revenue = spend * ROAS (Meta-attributed)
      Shopify rows: spend = 0, revenue = order total (actual Shopify revenue)
      The insights query sums both — giving true blended ROAS when both are present.
    """
    __tablename__ = "ad_spend_daily"
    __table_args__ = (
        UniqueConstraint("brand_id", "platform", "campaign_id", "date", name="uq_ad_spend_daily"),
        # Primary analytics query index — supports range scans on date within a brand
        Index("ix_ad_spend_brand_date", "brand_id", "date"),
        # Covering index: index-only scan for the spend-vs-revenue aggregation query
        # postgresql_include is a Postgres 11+ feature
        Index(
            "ix_ad_spend_covering",
            "brand_id", "date",
            postgresql_include=["spend", "revenue", "impressions", "clicks"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    campaign_id: Mapped[str] = mapped_column(String(255), nullable=False)
    campaign_name: Mapped[str | None] = mapped_column(String(500))
    date: Mapped[date] = mapped_column(Date, nullable=False)   # proper DATE, not String
    spend: Mapped[float] = mapped_column(Float, default=0.0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    brand: Mapped["Brand"] = relationship("Brand", back_populates="ad_spend_records")
