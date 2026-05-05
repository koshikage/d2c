"""
Shopify Integration — Serializers (Pydantic Schemas)
======================================================
Pydantic models that validate and parse raw Shopify API responses
before they touch our database models.

WHY A SEPARATE SERIALIZER LAYER:
  Shopify's API shape ≠ our DB schema. For example:
    - Shopify returns `price` as a string ("19.99"), we store it as float
    - Shopify nests variants inside products; we flatten to one price
    - Shopify uses string IDs (GIDs); we store them as external_id

  Without this layer:
    - Sync logic is littered with dict key access and type casting
    - A Shopify API change (renamed field) breaks at runtime, not at parse time
    - No validation: a missing required field causes a KeyError deep in business logic

  WITH this layer:
    - Pydantic validates the shape at parse time with a clear error message
    - We transform Shopify's shape to our internal shape in one place
    - Unit-testable: pass fixture JSON, assert internal schema

PROS:
  + Validation at ingestion boundary — bad data rejected early
  + Transformation logic isolated and unit-testable
  + Pydantic's alias support handles snake_case ↔ camelCase mapping

CONS:
  - More models to maintain; Shopify schema drift requires updates here
  - Pydantic V2 parsing is fast but not zero-cost for large response pages
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Shopify Product Serializers ───────────────────────────────────────────────

class ShopifyVariant(BaseModel):
    id: str
    price: float = 0.0
    inventory_quantity: int = 0

    @field_validator("price", mode="before")
    @classmethod
    def parse_price(cls, v: Any) -> float:
        """Shopify returns price as string '19.99'. Convert to float."""
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0


class ShopifyProductIn(BaseModel):
    """
    Validates raw JSON from GET /products.json → products[].
    Maps Shopify field names to our internal shape.
    """
    id: str                          # Shopify GID: "gid://shopify/Product/12345"
    title: str
    vendor: str | None = None
    product_type: str | None = None
    status: str = "active"
    variants: list[ShopifyVariant] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict | None = None          # full payload preserved for raw_data column

    @model_validator(mode="before")
    @classmethod
    def capture_raw(cls, data: Any) -> Any:
        """Preserve the full raw payload before Pydantic strips unknown fields."""
        if isinstance(data, dict):
            data["raw"] = dict(data)
        return data

    @property
    def primary_price(self) -> float:
        """First variant price as canonical product price."""
        return self.variants[0].price if self.variants else 0.0

    @property
    def total_inventory(self) -> int:
        """Sum inventory across all variants."""
        return sum(v.inventory_quantity for v in self.variants)


class ShopifyProductsPage(BaseModel):
    """Wraps a full products page response."""
    products: list[ShopifyProductIn]
    next_page_url: str | None = None  # parsed from Link header externally


# ── Shopify Order Serializers ─────────────────────────────────────────────────

class ShopifyOrderIn(BaseModel):
    """
    Validates raw JSON from GET /orders.json → orders[].
    """
    id: str                           # Shopify GID
    order_number: str | None = None
    total_price: float = 0.0
    subtotal_price: float = 0.0
    currency: str = "USD"
    financial_status: str | None = None
    fulfillment_status: str | None = None
    created_at: datetime
    raw: dict | None = None

    @field_validator("total_price", "subtotal_price", mode="before")
    @classmethod
    def parse_price(cls, v: Any) -> float:
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    @model_validator(mode="before")
    @classmethod
    def capture_raw(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data["raw"] = dict(data)
        return data


class ShopifyOrdersPage(BaseModel):
    orders: list[ShopifyOrderIn]
    next_page_url: str | None = None


# ── Shopify Webhook Payload Serializer ────────────────────────────────────────

class ShopifyWebhookOrderPayload(BaseModel):
    """
    Validates the body of an orders/created or orders/updated webhook.
    We only extract what we need; extra fields are ignored.
    """
    id: str
    order_number: str | None = None
    total_price: float = 0.0
    subtotal_price: float = 0.0
    currency: str = "USD"
    financial_status: str | None = None
    fulfillment_status: str | None = None
    created_at: datetime

    @field_validator("total_price", "subtotal_price", mode="before")
    @classmethod
    def parse_price(cls, v: Any) -> float:
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0


# ── Outbound: API response schemas ───────────────────────────────────────────

class ProductResponse(BaseModel):
    """What our GET /products endpoint returns to API clients."""
    id: str
    external_id: str
    title: str
    vendor: str | None
    product_type: str | None
    price: float | None
    inventory_quantity: int | None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProductListResponse(BaseModel):
    items: list[ProductResponse]
    total: int
    page: int
    page_size: int
    has_next: bool
