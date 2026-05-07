"""
Meta Ads Integration Endpoints — Serializers
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class MetaConnectRequest(BaseModel):
    ad_account_id: str = Field(
        ...,
        description="Meta Ad Account ID (format: act_XXXXXXXX)",
        examples=["act_123456789"],
    )


class MetaConnectResponse(BaseModel):
    authorize_url: str
    state: str
    message: str = "Redirect the user to authorize_url to complete Meta Ads connection"


class MetaCallbackRequest(BaseModel):
    code: str
    state: str
    ad_account_id: str


class MetaConnectionStatus(BaseModel):
    is_connected: bool
    ad_account_id: str | None
    last_synced_at: datetime | None

    model_config = {"from_attributes": True}


class MetaSyncResponse(BaseModel):
    message: str
    rows_synced: int
    brand_id: uuid.UUID
