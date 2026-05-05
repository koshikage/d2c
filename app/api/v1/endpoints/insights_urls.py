"""
Insights Endpoints — URL Routes
==================================
/insights/spend-vs-revenue   GET  — daily Meta spend vs Shopify revenue with ROAS
/products                    GET  — paginated product list (tenant-scoped)
"""
from fastapi import APIRouter

router = APIRouter(tags=["Insights & Products"])
