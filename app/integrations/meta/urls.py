"""
Meta Ads Integration — URL Constants
======================================
All Meta Graph API URL patterns centralised.

Meta Graph API versioning: v19.0, v20.0, etc.
Unlike Shopify (REST), Meta uses a Graph-style flat URL structure.
"""
from app.core.config import settings

META_GRAPH_VERSION = "v19.0"
META_GRAPH_BASE = f"https://graph.facebook.com/{META_GRAPH_VERSION}"


class MetaURLs:
    """
    Build Meta Graph API URLs.
    In dev/test: points to the mock Meta server.
    In production: points to graph.facebook.com.
    """

    def __init__(self, use_mock: bool = True):
        if use_mock:
            self._base = f"{settings.META_MOCK_BASE_URL}/{META_GRAPH_VERSION}"
        else:
            self._base = META_GRAPH_BASE

    def insights(self, ad_account_id: str) -> str:
        """Campaign-level spend/ROAS insights for an ad account."""
        return f"{self._base}/{ad_account_id}/insights"

    def campaigns(self, ad_account_id: str) -> str:
        return f"{self._base}/{ad_account_id}/campaigns"

    @property
    def me(self) -> str:
        """Token introspection endpoint."""
        return f"{self._base}/me"

    # OAuth
    @staticmethod
    def oauth_dialog(app_id: str, state: str, redirect_uri: str) -> str:
        return (
            f"https://www.facebook.com/dialog/oauth"
            f"?client_id={app_id}"
            f"&redirect_uri={redirect_uri}"
            f"&state={state}"
            f"&scope=ads_read,ads_management"
        )

    @staticmethod
    def oauth_token() -> str:
        return "https://graph.facebook.com/oauth/access_token"
