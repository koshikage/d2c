"""
Shopify Integration — URL Constants
=====================================
All Shopify API URL patterns in one place.

WHY CENTRALISE URLS:
  - When Shopify bumps API version (2024-01 → 2024-10), one change here
    updates every caller automatically.
  - Integration tests can swap SHOPIFY_API_VERSION to test version migrations.
  - No magic strings scattered across sync logic.

PROS:
  + Single point of change for API version upgrades
  + Easy to grep what endpoints we use

CONS:
  - Extra indirection; trivial for small integrations
"""
from app.core.config import settings

SHOPIFY_API_VERSION = "2024-01"


class ShopifyURLs:
    """
    Build Shopify Admin REST API URLs for a given shop domain.
    Real usage: shop_domain = "brand-name.myshopify.com"
    Mock usage:  base_url = settings.SHOPIFY_MOCK_BASE_URL
    """

    def __init__(self, shop_domain: str):
        # In production: https://{shop_domain}/admin/api/{version}
        # In dev/test:    http://mock-shopify:8001/admin/api/{version}
        if shop_domain.startswith("http"):
            # Already a full URL (mock server)
            self._base = f"{shop_domain}/admin/api/{SHOPIFY_API_VERSION}"
        else:
            self._base = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}"

    @property
    def products(self) -> str:
        return f"{self._base}/products.json"

    @property
    def orders(self) -> str:
        return f"{self._base}/orders.json"

    @property
    def webhooks(self) -> str:
        return f"{self._base}/webhooks.json"

    def order(self, order_id: str) -> str:
        return f"{self._base}/orders/{order_id}.json"

    def product(self, product_id: str) -> str:
        return f"{self._base}/products/{product_id}.json"

    # OAuth endpoints (on the shop domain, not /admin/api)
    @staticmethod
    def oauth_authorize(shop_domain: str, client_id: str, state: str, redirect_uri: str) -> str:
        return (
            f"https://{shop_domain}/admin/oauth/authorize"
            f"?client_id={client_id}"
            f"&scope=read_products,read_orders,write_webhooks"
            f"&redirect_uri={redirect_uri}"
            f"&state={state}"
        )

    @staticmethod
    def oauth_token(shop_domain: str) -> str:
        return f"https://{shop_domain}/admin/oauth/access_token"
