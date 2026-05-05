"""
Insights Endpoints — Serializers
====================================
Query parameter and response schemas for insights and product endpoints.

SPEND VS REVENUE RESPONSE:
  We return per-day granularity + aggregate totals.
  ROAS = total_revenue / total_spend (guard against divide-by-zero).

  The day-level ROAS is: revenue / spend for that day.
  Zero-spend days have ROAS = 0.0 (not infinity).

PRODUCT RESPONSE:
  Standard cursor-style pagination.
  page + page_size → offset-based (simple for Phase 1).
  Phase 2: switch to keyset (cursor) pagination for large product catalogs.
"""
from datetime import date

from pydantic import BaseModel, Field, field_validator


class SpendRevenueQueryParams(BaseModel):
    """
    Query parameters for GET /insights/spend-vs-revenue
    Validated as a dependency to get clear error messages.
    """
    from_date: date = Field(..., alias="from")
    to_date: date = Field(..., alias="to")

    @field_validator("to_date")
    @classmethod
    def to_after_from(cls, v: date, info) -> date:
        if "from_date" in info.data and v < info.data["from_date"]:
            raise ValueError("'to' date must be >= 'from' date")
        return v

    model_config = {"populate_by_name": True}


class SpendRevenueDayOut(BaseModel):
    date: str
    spend: float
    revenue: float
    impressions: int
    clicks: int
    roas: float


class SpendRevenueOut(BaseModel):
    items: list[SpendRevenueDayOut]
    from_date: str
    to_date: str
    total_spend: float
    total_revenue: float
    overall_roas: float


class ProductOut(BaseModel):
    id: str
    external_id: str
    title: str
    vendor: str | None
    product_type: str | None
    price: float | None
    inventory_quantity: int | None
    status: str

    model_config = {"from_attributes": True}


class ProductListOut(BaseModel):
    items: list[ProductOut]
    total: int
    page: int
    page_size: int
    has_next: bool
