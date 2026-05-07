"""
Meta Ads Integration Endpoints — Route Logic
"""
from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.meta_serializers import (
    MetaCallbackRequest,
    MetaConnectRequest,
    MetaConnectResponse,
    MetaConnectionStatus,
    MetaSyncResponse,
)
from app.api.v1.endpoints.meta_urls import router
from app.core.logging import get_logger
from app.db.session import get_db
from app.integrations.meta.service import (
    complete_meta_oauth,
    initiate_meta_oauth,
    sync_meta_insights,
)
from app.middleware.auth import CurrentUser, get_current_user
from app.models import MetaConnection

logger = get_logger(__name__)


@router.post("/connect", response_model=MetaConnectResponse)
async def connect_meta(
    body: MetaConnectRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Initiate Meta Ads OAuth flow for the authenticated brand."""
    result = await initiate_meta_oauth(db=db, brand_id=current_user.brand_id)
    # Store ad_account_id in the connection for later use
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    import uuid
    stmt = pg_insert(MetaConnection).values(
        id=uuid.uuid4(),
        brand_id=current_user.brand_id,
        ad_account_id=body.ad_account_id,
        is_connected=False,
    ).on_conflict_do_update(
        index_elements=["brand_id"],
        set_={"ad_account_id": body.ad_account_id},
    )
    await db.execute(stmt)
    await db.commit()

    return MetaConnectResponse(authorize_url=result["authorize_url"], state=result["state"])


@router.get("/callback")
async def meta_oauth_callback(
    code: str,
    state: str,
    ad_account_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Complete Meta OAuth. Call with the state from /connect."""
    connection = await complete_meta_oauth(
        db=db,
        brand_id=current_user.brand_id,
        code=code,
        state=state,
        ad_account_id=ad_account_id,
    )
    return {"message": "Meta Ads connected successfully", "ad_account_id": connection.ad_account_id}


@router.post("/sync", response_model=MetaSyncResponse)
async def trigger_meta_sync(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Trigger immediate Meta Ads insights sync for the authenticated brand."""
    result = await db.execute(
        select(MetaConnection).where(
            MetaConnection.brand_id == current_user.brand_id,
            MetaConnection.is_connected == True,  # noqa: E712
        )
    )
    connection = result.scalars().first()

    if not connection:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active Meta connection. Please connect first.",
        )

    rows = await sync_meta_insights(db, current_user.brand_id, connection)

    return MetaSyncResponse(
        message="Meta sync completed",
        rows_synced=rows,
        brand_id=current_user.brand_id,
    )


@router.get("/status", response_model=MetaConnectionStatus)
async def meta_connection_status(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return Meta connection status for the authenticated brand."""
    result = await db.execute(
        select(MetaConnection).where(MetaConnection.brand_id == current_user.brand_id)
    )
    connection = result.scalars().first()

    if not connection:
        return MetaConnectionStatus(is_connected=False, ad_account_id=None, last_synced_at=None)

    return MetaConnectionStatus(
        is_connected=connection.is_connected,
        ad_account_id=connection.ad_account_id,
        last_synced_at=connection.last_synced_at,
    )
