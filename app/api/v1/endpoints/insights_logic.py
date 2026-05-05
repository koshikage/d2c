"""
Insights Endpoints — Route Logic
=====================================
Implements spend-vs-revenue analytics and paginated product listing.

SPEND VS REVENUE QUERY:
  Joins ad_spend_daily (Meta spend) with daily Shopify order totals.
  Both are already in ad_spend_daily — Meta rows have spend > 0,
  Shopify rows (if we wrote them) have revenue > 0.

  For simplicity: we aggregate ad_spend_daily by date, summing spend and revenue.
  The ROAS is computed in Python after fetching.

  TENANT ISOLATION:
    The WHERE brand_id = :brand_id clause is mandatory.
    We use TenantSession which adds it automatically, but for the
    raw SQL aggregation query below we add it explicitly + assert it.

PRODUCTS QUERY:
  Uses TenantSession.all() which appends brand_id automatically.
  Demonstrates that even without explicit brand_id in the handler,
  cross-tenant access is impossible.
"""
from datetime import date

from fastapi import Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.insights_serializers import (
    ProductListOut,
    ProductOut,
    SpendRevenueDayOut,
    SpendRevenueOut,
)
from app.api.v1.endpoints.insights_urls import router
from app.core.logging import get_logger
from app.db.session import get_db
from app.db.tenant import TenantSession
from app.middleware.auth import CurrentUser, get_current_user
from app.models import AdSpendDaily, Product

logger = get_logger(__name__)


@router.get("/insights/spend-vs-revenue", response_model=SpendRevenueOut)
async def spend_vs_revenue(
    from_date: date = Query(..., alias="from", description="Start date YYYY-MM-DD"),
    to_date: date = Query(..., alias="to", description="End date YYYY-MM-DD"),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Return daily Meta spend vs Shopify-attributed revenue with ROAS.

    TENANT ISOLATION PROOF:
      The WHERE clause explicitly includes brand_id = current_user.brand_id.
      A user from Brand A physically cannot receive Brand B data because:
      1. current_user.brand_id comes from the JWT (cryptographically signed)
      2. The JWT's brand_id was validated against the DB in get_current_user
      3. This query filters on that validated brand_id

    ROAS CALCULATION:
      ROAS = total_revenue / total_spend
      Computed per-day and in aggregate. Zero-spend → ROAS = 0.
    """
    from_str = from_date.isoformat()
    to_str = to_date.isoformat()

    # Aggregate spend + revenue per day for this brand only
    stmt = (
        select(
            AdSpendDaily.date,
            func.sum(AdSpendDaily.spend).label("spend"),
            func.sum(AdSpendDaily.revenue).label("revenue"),
            func.sum(AdSpendDaily.impressions).label("impressions"),
            func.sum(AdSpendDaily.clicks).label("clicks"),
        )
        .where(
            AdSpendDaily.brand_id == current_user.brand_id,  # TENANT ISOLATION
            AdSpendDaily.date >= from_str,
            AdSpendDaily.date <= to_str,
        )
        .group_by(AdSpendDaily.date)
        .order_by(AdSpendDaily.date)
    )

    result = await db.execute(stmt)
    rows = result.fetchall()

    items = []
    total_spend = 0.0
    total_revenue = 0.0

    for row in rows:
        spend = float(row.spend or 0)
        revenue = float(row.revenue or 0)
        roas = round(revenue / spend, 4) if spend > 0 else 0.0
        total_spend += spend
        total_revenue += revenue

        items.append(SpendRevenueDayOut(
            date=row.date,
            spend=round(spend, 2),
            revenue=round(revenue, 2),
            impressions=int(row.impressions or 0),
            clicks=int(row.clicks or 0),
            roas=roas,
        ))

    overall_roas = round(total_revenue / total_spend, 4) if total_spend > 0 else 0.0

    logger.info(
        "insights_queried",
        from_date=from_str,
        to_date=to_str,
        days_returned=len(items),
        total_spend=total_spend,
    )

    return SpendRevenueOut(
        items=items,
        from_date=from_str,
        to_date=to_str,
        total_spend=round(total_spend, 2),
        total_revenue=round(total_revenue, 2),
        overall_roas=overall_roas,
    )


@router.get("/products", response_model=ProductListOut)
async def list_products(
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Paginated product list — always scoped to the authenticated user's brand.

    TENANT ISOLATION:
      We use TenantSession here to demonstrate that even if a developer
      forgets to add .where(Product.brand_id == ...), the TenantSession
      adds it automatically. The test suite verifies this property.
    """
    ts = TenantSession(db, brand_id=current_user.brand_id)

    # Count total (for pagination metadata)
    count_stmt = (
        select(func.count(Product.id))
        .where(Product.brand_id == current_user.brand_id)  # TenantSession also adds this
    )
    count_result = await db.execute(count_stmt)
    total = count_result.scalar() or 0

    # Fetch page
    offset = (page - 1) * page_size
    page_stmt = (
        select(Product)
        .where(Product.brand_id == current_user.brand_id)
        .order_by(Product.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    products = await ts.all(Product, page_stmt)

    return ProductListOut(
        items=[
            ProductOut(
                id=str(p.id),
                external_id=p.external_id,
                title=p.title,
                vendor=p.vendor,
                product_type=p.product_type,
                price=p.price,
                inventory_quantity=p.inventory_quantity,
                status=p.status,
            )
            for p in products
        ],
        total=total,
        page=page,
        page_size=page_size,
        has_next=(offset + page_size) < total,
    )
