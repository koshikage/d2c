"""
STEP 11 — Mock Meta Ads Server
================================
Simulates the Meta Marketing API (Graph API) for campaign insights.

KEY DIFFERENCES FROM SHOPIFY:
  - Meta uses cursor-based pagination via "paging.cursors.after"
  - Meta returns insights (aggregated metrics) not raw events
  - Rate limits are "Application-level Throttling" based on Business Use Case tier
  - Meta tokens can be user tokens OR system user tokens (we use long-lived page tokens)

META INSIGHTS STRUCTURE:
  /act_ACCOUNT_ID/insights returns:
  {
    "data": [
      {
        "campaign_id": "...",
        "campaign_name": "...",
        "date_start": "2024-01-01",
        "date_stop": "2024-01-01",
        "spend": "42.50",
        "impressions": "12500",
        "clicks": "340",
        "purchase_roas": [{"action_type": "purchase", "value": "3.2"}]
      }
    ],
    "paging": {"cursors": {"before": "...", "after": "..."}, "next": "..."}
  }

ROAS CALCULATION:
  ROAS = Revenue / Spend
  Meta returns this as purchase_roas[0].value
  We also store raw spend + impressions + clicks for our own computation.
"""
import random
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

mock_meta = FastAPI(title="Mock Meta Ads API", version="1.0.0")

CAMPAIGN_NAMES = [
    "Summer Sale 2024 - Retargeting",
    "New Customer Acquisition - Lookalike",
    "Brand Awareness - Video",
    "Product Catalog - DPA",
    "Holiday Campaign - Conversion",
]


def _generate_insights(ad_account_id: str, date_preset: str, after_cursor: str | None) -> dict:
    """Generate realistic-looking Meta campaign insights."""
    random.seed(ad_account_id + (after_cursor or ""))

    num_campaigns = 5
    start_date = datetime.now(timezone.utc) - timedelta(days=30)

    data = []
    for i, name in enumerate(CAMPAIGN_NAMES):
        for day_offset in range(7):  # 7 days of data per page
            date = start_date + timedelta(days=day_offset)
            spend = round(random.uniform(50.0, 500.0), 2)
            impressions = random.randint(5000, 50000)
            clicks = random.randint(100, int(impressions * 0.05))
            roas = round(random.uniform(1.5, 6.0), 2)
            revenue = round(spend * roas, 2)

            data.append({
                "campaign_id": f"{ad_account_id}_camp_{i+1:03d}",
                "campaign_name": name,
                "date_start": date.strftime("%Y-%m-%d"),
                "date_stop": date.strftime("%Y-%m-%d"),
                "spend": str(spend),
                "impressions": str(impressions),
                "clicks": str(clicks),
                "purchase_roas": [{"action_type": "purchase", "value": str(roas)}],
                "actions": [
                    {"action_type": "purchase", "value": str(int(clicks * 0.03))}
                ],
                "__revenue": revenue,  # convenience field for our normaliser
            })

    # Paginate: return first 20 items, provide next cursor if more
    page_size = 20
    start = 0
    if after_cursor:
        try:
            start = int(after_cursor)
        except ValueError:
            start = 0

    page_data = data[start: start + page_size]
    has_next = start + page_size < len(data)

    paging: dict = {
        "cursors": {
            "before": str(max(0, start - page_size)),
            "after": str(start + page_size),
        }
    }
    if has_next:
        paging["next"] = (
            f"https://graph.facebook.com/v19.0/{ad_account_id}/insights"
            f"?after={start + page_size}"
        )

    return {"data": page_data, "paging": paging}


@mock_meta.get("/v19.0/{ad_account_id}/insights")
async def get_insights(
    ad_account_id: str,
    date_preset: str = Query(default="last_30d"),
    fields: str = Query(default="campaign_id,campaign_name,spend,impressions,clicks"),
    level: str = Query(default="campaign"),
    after: str | None = Query(default=None),
    access_token: str | None = Query(default=None),
):
    """
    Simulate Meta Ads Insights API.
    Returns paginated campaign-level spend/ROAS data.
    """
    if not access_token:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Invalid OAuth access token", "code": 190}},
        )

    return JSONResponse(content=_generate_insights(ad_account_id, date_preset, after))


@mock_meta.get("/v19.0/me")
async def get_me(access_token: str | None = Query(default=None)):
    """Token introspection / user info endpoint."""
    if not access_token:
        return JSONResponse(
            status_code=401,
            content={"error": {"message": "Invalid OAuth access token", "code": 190}},
        )
    return JSONResponse(content={"id": "mock_user_123", "name": "Mock Meta User"})


@mock_meta.get("/v19.0/{ad_account_id}/campaigns")
async def list_campaigns(
    ad_account_id: str,
    access_token: str | None = Query(default=None),
):
    campaigns = [
        {"id": f"{ad_account_id}_camp_{i+1:03d}", "name": name, "status": "ACTIVE"}
        for i, name in enumerate(CAMPAIGN_NAMES)
    ]
    return JSONResponse(content={"data": campaigns})
