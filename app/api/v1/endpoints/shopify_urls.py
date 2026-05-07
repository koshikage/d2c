"""
Shopify Integration Endpoints — URL Routes
============================================
/integrations/shopify/connect          POST  — initiate OAuth connect flow
/integrations/shopify/callback         GET   — complete OAuth callback (mocked)
/integrations/shopify/sync             POST  — trigger manual background sync
/integrations/shopify/webhook          POST  — receive order-created webhooks
/integrations/shopify/status           GET   — connection status for current brand
"""
from fastapi import APIRouter

router = APIRouter(prefix="/integrations/shopify", tags=["Shopify Integration"])
