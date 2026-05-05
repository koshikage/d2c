"""
Meta Ads Integration — Sync Service
=====================================
Orchestrates pulling Meta campaign insights and writing to AdSpendDaily.

CURSOR PAGINATION (Meta style):
  Meta uses cursor-based pagination different from Shopify.
  Response includes paging.next URL or paging.cursors.after.
  We follow paging.next until it's absent.

NORMALISATION:
  Each MetaInsightRow → one AdSpendDaily row.
  UNIQUE(brand_id, platform, campaign_id, date) ensures idempotent upserts.

INCREMENTAL SYNC:
  Real implementation would pass date_preset=custom_range with since/until
  based on connection.last_synced_at. For the mock we pull last_30d always.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.encryption import decrypt_token, encrypt_token
from app.core.logging import get_logger
from app.integrations.base_client import IntegrationHTTPClient
from app.integrations.meta.serializers import MetaInsightRow, MetaInsightsResponse
from app.integrations.meta.urls import MetaURLs
from app.models import AdSpendDaily, MetaConnection

logger = get_logger(__name__)


# ── OAuth Flow ────────────────────────────────────────────────────────────────

async def initiate_meta_oauth(
    db: AsyncSession,
    brand_id: uuid.UUID,
) -> dict:
    """
    Initiate Meta OAuth. Returns the dialog URL.
    Same CSRF-state pattern as Shopify.
    """
    import secrets
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    state = secrets.token_urlsafe(32)

    stmt = pg_insert(MetaConnection).values(
        id=uuid.uuid4(),
        brand_id=brand_id,
        oauth_state=state,
        is_connected=False,
    ).on_conflict_do_update(
        index_elements=["brand_id"],
        set_={"oauth_state": state, "is_connected": False},
    )
    await db.execute(stmt)
    await db.commit()

    dialog_url = MetaURLs.oauth_dialog(
        app_id=settings.META_APP_ID,
        state=state,
        redirect_uri="https://your-app.com/integrations/meta/callback",
    )

    logger.info("meta_oauth_initiated", brand_id=str(brand_id))
    return {"authorize_url": dialog_url, "state": state}


async def complete_meta_oauth(
    db: AsyncSession,
    brand_id: uuid.UUID,
    code: str,
    state: str,
    ad_account_id: str,
) -> MetaConnection:
    """
    Exchange code for token and store encrypted.
    ad_account_id must be provided by the frontend after the OAuth flow
    (Meta requires the user to select their ad account).
    """
    from sqlalchemy import select

    result = await db.execute(
        select(MetaConnection).where(MetaConnection.brand_id == brand_id)
    )
    connection = result.scalars().first()

    if not connection:
        raise ValueError(f"No pending Meta connection for brand {brand_id}")

    if connection.oauth_state != state:
        raise PermissionError("OAuth state mismatch")

    mock_token = f"EAAMock_{brand_id}_{ad_account_id}"

    connection.access_token_enc = encrypt_token(mock_token)
    connection.ad_account_id = ad_account_id
    connection.is_connected = True
    connection.oauth_state = None
    db.add(connection)
    await db.commit()
    await db.refresh(connection)

    logger.info("meta_oauth_completed", brand_id=str(brand_id), ad_account=ad_account_id)
    return connection


# ── Insights Sync ────────────────────────────────────────────────────────────

async def sync_meta_insights(
    db: AsyncSession,
    brand_id: uuid.UUID,
    connection: MetaConnection,
) -> int:
    """
    Pull campaign insights from Meta and upsert into ad_spend_daily.

    FIELDS REQUESTED:
      campaign_id, campaign_name, date_start, date_stop,
      spend, impressions, clicks, purchase_roas

    PAGINATION:
      Follow paging.next cursor until exhausted.
    """
    if not connection.access_token_enc or not connection.ad_account_id:
        raise ValueError(f"Meta connection for brand {brand_id} is not fully configured")

    token = decrypt_token(connection.access_token_enc)
    urls = MetaURLs(use_mock=True)

    total_upserted = 0
    insights_url = urls.insights(connection.ad_account_id)
    after_cursor: str | None = None

    async with IntegrationHTTPClient() as client:
        while True:
            params: dict = {
                "access_token": token,
                "date_preset": "last_30d",
                "level": "campaign",
                "fields": "campaign_id,campaign_name,date_start,date_stop,spend,impressions,clicks,purchase_roas",
            }
            if after_cursor:
                params["after"] = after_cursor

            logger.info("meta_insights_page_fetch", brand_id=str(brand_id), cursor=after_cursor)
            raw = await client.get(insights_url, params=params)

            # Parse response
            response_model = MetaInsightsResponse(
                data=raw.get("data", []),
                paging=raw.get("paging"),
            )

            rows = [MetaInsightRow(**row) for row in response_model.data]
            count = await _upsert_ad_spend(db, brand_id, rows)
            total_upserted += count

            # Advance cursor
            if response_model.paging and response_model.paging.next:
                after_cursor = response_model.paging.cursors.get("after")
                if not after_cursor:
                    break
            else:
                break

    connection.last_synced_at = datetime.now(timezone.utc)
    db.add(connection)
    await db.commit()

    logger.info("meta_insights_sync_complete", brand_id=str(brand_id), total=total_upserted)
    return total_upserted


async def _upsert_ad_spend(
    db: AsyncSession,
    brand_id: uuid.UUID,
    rows: list[MetaInsightRow],
) -> int:
    """
    Upsert ad spend rows.
    UNIQUE(brand_id, platform, campaign_id, date) makes this idempotent.
    """
    if not rows:
        return 0

    values = [
        {
            "id": uuid.uuid4(),
            "brand_id": brand_id,
            "platform": "meta",
            "campaign_id": row.campaign_id,
            "campaign_name": row.campaign_name,
            "date": row.date_start,
            "spend": row.spend,
            "impressions": row.impressions,
            "clicks": row.clicks,
            "revenue": row.revenue,
        }
        for row in rows
    ]

    stmt = pg_insert(AdSpendDaily).values(values).on_conflict_do_update(
        constraint="uq_ad_spend_daily",
        set_={
            "spend": pg_insert(AdSpendDaily).excluded.spend,
            "impressions": pg_insert(AdSpendDaily).excluded.impressions,
            "clicks": pg_insert(AdSpendDaily).excluded.clicks,
            "revenue": pg_insert(AdSpendDaily).excluded.revenue,
            "campaign_name": pg_insert(AdSpendDaily).excluded.campaign_name,
        },
    )
    await db.execute(stmt)
    await db.commit()
    return len(values)
