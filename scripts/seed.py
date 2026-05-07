"""
Seed Script
=============
Creates 2 brands, 2 users (one per brand), and mock integration data for both.

Run with:
    python scripts/seed.py

HOW IT WORKS:
  Uses SQLAlchemy sync engine (simpler for a one-off script).
  Idempotent: re-running clears and re-creates seed data only (not schema).

WHAT IT CREATES:
  Brand Alpha   → user: alice@alpha.com    → Shopify + Meta connected
  Brand Beta    → user: bob@beta.com       → Shopify + Meta connected
  + Mock ad_spend_daily rows for both brands (last 30 days)
"""
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.encryption import encrypt_token
from app.models import (
    AdSpendDaily,
    Brand,
    MetaConnection,
    ShopifyConnection,
    User,
)

engine = create_async_engine(settings.DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

BRAND_ALPHA_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
BRAND_BETA_ID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
USER_ALICE_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
USER_BOB_ID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")


async def seed() -> None:
    async with SessionLocal() as db:
        print("🌱 Seeding database...")

        # ── Clear existing seed data ──────────────────────────────────────────
        from sqlalchemy import delete
        for model in [AdSpendDaily, ShopifyConnection, MetaConnection, User, Brand]:
            await db.execute(delete(model).where(model.id.in_([
                BRAND_ALPHA_ID, BRAND_BETA_ID,
                USER_ALICE_ID, USER_BOB_ID,
            ])))
        await db.commit()

        # ── Brand Alpha ───────────────────────────────────────────────────────
        brand_alpha = Brand(
            id=BRAND_ALPHA_ID,
            name="Brand Alpha",
            domain="brand-alpha.myshopify.com",
            is_active=True,
            settings={
                "timezone": "America/New_York",
                "currency": "USD",
                "fiscal_year_start": "01-01",
                # Phase 2: parent_company_id will go here until promoted to FK column
                "parent_company_id": None,
            },
        )

        # ── Brand Beta ────────────────────────────────────────────────────────
        brand_beta = Brand(
            id=BRAND_BETA_ID,
            name="Brand Beta",
            domain="brand-beta.myshopify.com",
            is_active=True,
            settings={
                "timezone": "America/Los_Angeles",
                "currency": "USD",
                "fiscal_year_start": "01-01",
                "parent_company_id": None,
            },
        )

        db.add_all([brand_alpha, brand_beta])
        await db.flush()

        # ── Users ─────────────────────────────────────────────────────────────
        alice = User(
            id=USER_ALICE_ID,
            brand_id=BRAND_ALPHA_ID,
            email="alice@alpha.com",
            full_name="Alice Alpha",
            is_active=True,
        )
        bob = User(
            id=USER_BOB_ID,
            brand_id=BRAND_BETA_ID,
            email="bob@beta.com",
            full_name="Bob Beta",
            is_active=True,
        )
        db.add_all([alice, bob])
        await db.flush()

        # ── Shopify Connections ───────────────────────────────────────────────
        shopify_alpha = ShopifyConnection(
            id=uuid.uuid4(),
            brand_id=BRAND_ALPHA_ID,
            shop_domain="brand-alpha.myshopify.com",
            access_token_enc=encrypt_token(f"shpat_mock_{BRAND_ALPHA_ID}"),
            is_connected=True,
            last_synced_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        shopify_beta = ShopifyConnection(
            id=uuid.uuid4(),
            brand_id=BRAND_BETA_ID,
            shop_domain="brand-beta.myshopify.com",
            access_token_enc=encrypt_token(f"shpat_mock_{BRAND_BETA_ID}"),
            is_connected=True,
            last_synced_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        db.add_all([shopify_alpha, shopify_beta])

        # ── Meta Connections ──────────────────────────────────────────────────
        meta_alpha = MetaConnection(
            id=uuid.uuid4(),
            brand_id=BRAND_ALPHA_ID,
            ad_account_id="act_alpha_001",
            access_token_enc=encrypt_token(f"EAAMock_{BRAND_ALPHA_ID}"),
            is_connected=True,
            last_synced_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        meta_beta = MetaConnection(
            id=uuid.uuid4(),
            brand_id=BRAND_BETA_ID,
            ad_account_id="act_beta_001",
            access_token_enc=encrypt_token(f"EAAMock_{BRAND_BETA_ID}"),
            is_connected=True,
            last_synced_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        db.add_all([meta_alpha, meta_beta])

        # ── Mock AdSpendDaily (30 days × 2 brands × 2 campaigns) ─────────────
        import random
        random.seed(42)

        ad_rows = []
        for brand_id, prefix in [(BRAND_ALPHA_ID, "alpha"), (BRAND_BETA_ID, "beta")]:
            for day in range(30):
                date = (datetime.now(timezone.utc) - timedelta(days=day)).date()
                for camp_num in range(1, 3):
                    spend = round(random.uniform(50, 400), 2)
                    roas = round(random.uniform(2.0, 5.0), 2)
                    ad_rows.append(AdSpendDaily(
                        id=uuid.uuid4(),
                        brand_id=brand_id,
                        platform="meta",
                        campaign_id=f"act_{prefix}_001_camp_{camp_num:03d}",
                        campaign_name=f"Campaign {camp_num} - {prefix.title()}",
                        date=date,
                        spend=spend,
                        impressions=random.randint(5000, 40000),
                        clicks=random.randint(100, 2000),
                        revenue=round(spend * roas, 2),
                    ))

        db.add_all(ad_rows)
        await db.commit()

        print(f"✅ Created 2 brands, 2 users, 2 Shopify connections, 2 Meta connections")
        print(f"✅ Created {len(ad_rows)} ad_spend_daily rows")
        print()
        print("📋 Login credentials:")
        print("   alice@alpha.com  →  brand domain: brand-alpha.myshopify.com")
        print("   bob@beta.com     →  brand domain: brand-beta.myshopify.com")


if __name__ == "__main__":
    asyncio.run(seed())
