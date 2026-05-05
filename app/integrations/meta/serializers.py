"""
Meta Ads Integration — Serializers (Pydantic Schemas)
======================================================
Validates and normalises raw Meta Graph API responses.

KEY NORMALISATION CHALLENGE:
  Meta's ROAS is nested inside an action array:
    "purchase_roas": [{"action_type": "purchase", "value": "3.2"}]

  We flatten this to a simple float during parsing.

  Revenue is not directly in Meta's response — it's spend * ROAS.
  We compute it here so the AdSpendDaily record has a revenue field
  that can be joined against Shopify's order revenue.

SHARED SCHEMA (AdSpendDaily):
  Both Shopify revenue and Meta spend end up in the same normalised
  ad_spend_daily table. This is the "normalise into a shared schema"
  requirement. The join in the insights endpoint is:
    SELECT date, SUM(spend), SUM(revenue), SUM(revenue)/SUM(spend) as roas
    FROM ad_spend_daily
    WHERE brand_id = X AND date BETWEEN :from AND :to
    GROUP BY date

  This works because Meta populates (spend, revenue=spend*ROAS) and
  Shopify order aggregation populates (spend=0, revenue=order_total).

PROS:
  + Normalised schema → single query for multi-platform analytics
  + Pydantic catches missing / mis-typed fields at ingestion time
  + ROAS and revenue computed once here, not repeated in query layer

CONS:
  - Revenue from Meta is an estimate (attributed, not actual Shopify revenue)
  - Different attribution windows (1-day click vs 7-day click) affect ROAS value
  - Pydantic overhead for large insight datasets (acceptable at daily granularity)
"""
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Meta Raw Response Schemas ─────────────────────────────────────────────────

class MetaROASAction(BaseModel):
    action_type: str
    value: float = 0.0

    @field_validator("value", mode="before")
    @classmethod
    def parse_value(cls, v: Any) -> float:
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0


class MetaInsightRow(BaseModel):
    """
    One row from Meta's /insights endpoint at campaign + day granularity.
    """
    campaign_id: str
    campaign_name: str | None = None
    date_start: str                     # "2024-01-15"
    date_stop: str                      # same as date_start for daily
    spend: float = 0.0
    impressions: int = 0
    clicks: int = 0
    purchase_roas: list[MetaROASAction] = Field(default_factory=list)

    # Internal computed field — not from Meta API
    _revenue: float = 0.0

    @field_validator("spend", mode="before")
    @classmethod
    def parse_spend(cls, v: Any) -> float:
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    @field_validator("impressions", "clicks", mode="before")
    @classmethod
    def parse_int(cls, v: Any) -> int:
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    @model_validator(mode="before")
    @classmethod
    def extract_revenue(cls, data: Any) -> Any:
        """Compute revenue from spend * ROAS before Pydantic processes fields."""
        if isinstance(data, dict):
            roas_list = data.get("purchase_roas", [])
            roas = 0.0
            for action in roas_list:
                if isinstance(action, dict) and action.get("action_type") == "purchase":
                    try:
                        roas = float(action.get("value", 0))
                    except (ValueError, TypeError):
                        pass
            spend = 0.0
            try:
                spend = float(data.get("spend", 0))
            except (ValueError, TypeError):
                pass
            data["__revenue"] = spend * roas
        return data

    @property
    def roas(self) -> float:
        """Compute ROAS from purchase_roas actions."""
        for action in self.purchase_roas:
            if action.action_type == "purchase":
                return action.value
        return 0.0

    @property
    def revenue(self) -> float:
        """Attributed revenue = spend * ROAS."""
        return self.spend * self.roas


class MetaInsightsPage(BaseModel):
    """Wraps a single page of Meta insights with pagination cursor."""
    data: list[MetaInsightRow]
    next_cursor: str | None = None


class MetaPaging(BaseModel):
    cursors: dict[str, str] = Field(default_factory=dict)
    next: str | None = None


class MetaInsightsResponse(BaseModel):
    """Full Meta API response shape."""
    data: list[dict]
    paging: MetaPaging | None = None


# ── Outbound: API response schemas ───────────────────────────────────────────

class SpendRevenueDay(BaseModel):
    """One day of spend vs revenue for the /insights/spend-vs-revenue endpoint."""
    date: str
    spend: float
    revenue: float
    impressions: int
    clicks: int
    roas: float

    model_config = {"from_attributes": True}


class SpendRevenueResponse(BaseModel):
    items: list[SpendRevenueDay]
    from_date: str
    to_date: str
    total_spend: float
    total_revenue: float
    overall_roas: float
