"""
Unit Tests: Core Security Components
=======================================
Tests for JWT creation/verification, token encryption, and webhook HMAC.
"""
import base64
import hashlib
import hmac
import uuid

import pytest
from jose import JWTError


class TestJWT:
    def test_create_and_decode_roundtrip(self):
        from app.core.jwt import create_access_token, decode_access_token

        user_id = str(uuid.uuid4())
        brand_id = str(uuid.uuid4())
        email = "test@example.com"

        token = create_access_token(user_id, brand_id, email)
        payload = decode_access_token(token)

        assert payload.user_id == user_id
        assert payload.brand_id == brand_id
        assert payload.email == email

    def test_tampered_token_raises(self):
        from app.core.jwt import create_access_token, decode_access_token

        token = create_access_token("u1", "b1", "x@test.com")
        tampered = token[:-5] + "XXXXX"

        with pytest.raises(JWTError):
            decode_access_token(tampered)

    def test_token_contains_jti(self):
        """JTI claim present for future revocation support."""
        from app.core.jwt import create_access_token, decode_access_token

        token = create_access_token("u1", "b1", "x@test.com")
        payload = decode_access_token(token)
        assert payload.jti != ""


class TestEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        from app.core.encryption import decrypt_token, encrypt_token

        secret = "shpat_mock_supersecret_token_12345"
        encrypted = encrypt_token(secret)

        assert encrypted != secret  # actually encrypted
        assert decrypt_token(encrypted) == secret

    def test_encrypted_value_is_different_each_time(self):
        """Fernet uses a random IV — same plaintext produces different ciphertext."""
        from app.core.encryption import encrypt_token

        token = "same_token"
        enc1 = encrypt_token(token)
        enc2 = encrypt_token(token)
        assert enc1 != enc2


class TestShopifyHMAC:
    def test_valid_hmac_passes(self):
        from app.integrations.shopify.service import verify_shopify_hmac
        from app.core.config import settings

        body = b'{"id":"test_order","total_price":"99.99"}'
        signature = base64.b64encode(
            hmac.new(
                settings.SHOPIFY_WEBHOOK_SECRET.get_secret_value().encode(),
                body,
                hashlib.sha256,
            ).digest()
        ).decode()

        assert verify_shopify_hmac(body, signature) is True

    def test_invalid_hmac_fails(self):
        from app.integrations.shopify.service import verify_shopify_hmac

        body = b'{"id":"test_order"}'
        assert verify_shopify_hmac(body, "invalidsignature==") is False

    def test_tampered_body_fails(self):
        from app.integrations.shopify.service import verify_shopify_hmac
        from app.core.config import settings

        original_body = b'{"id":"order_1"}'
        signature = base64.b64encode(
            hmac.new(
                settings.SHOPIFY_WEBHOOK_SECRET.get_secret_value().encode(),
                original_body,
                hashlib.sha256,
            ).digest()
        ).decode()

        tampered_body = b'{"id":"order_2","total_price":"9999.99"}'
        assert verify_shopify_hmac(tampered_body, signature) is False


class TestShopifySerializers:
    def test_product_price_parsed_from_string(self):
        from app.integrations.shopify.serializers import ShopifyProductIn

        product = ShopifyProductIn(
            id="gid://shopify/Product/123",
            title="Test Product",
            variants=[{"id": "var_1", "price": "29.99", "inventory_quantity": 10}],
        )
        assert product.primary_price == 29.99

    def test_order_price_parsed_from_string(self):
        from app.integrations.shopify.serializers import ShopifyOrderIn
        from datetime import datetime, timezone

        order = ShopifyOrderIn(
            id="gid://shopify/Order/456",
            total_price="149.99",
            subtotal_price="129.99",
            created_at=datetime.now(timezone.utc),
        )
        assert order.total_price == 149.99

    def test_meta_roas_computed_from_actions(self):
        from app.integrations.meta.serializers import MetaInsightRow

        row = MetaInsightRow(
            campaign_id="camp_1",
            date_start="2024-01-15",
            date_stop="2024-01-15",
            spend="100.00",
            impressions="5000",
            clicks="200",
            purchase_roas=[{"action_type": "purchase", "value": "3.5"}],
        )
        assert row.roas == 3.5
        assert row.revenue == pytest.approx(350.0)


class TestShopifyPagination:
    def test_parse_next_link_header(self):
        from app.integrations.shopify.service import _parse_next_link

        header = '<https://shop.myshopify.com/admin/api/products.json?page_info=CURSOR123>; rel="next"'
        result = _parse_next_link(header)
        assert result == "https://shop.myshopify.com/admin/api/products.json?page_info=CURSOR123"

    def test_parse_no_next_link(self):
        from app.integrations.shopify.service import _parse_next_link

        header = '<https://shop.myshopify.com/admin/api/products.json?page_info=CUR>; rel="previous"'
        result = _parse_next_link(header)
        assert result is None

    def test_parse_none_link(self):
        from app.integrations.shopify.service import _parse_next_link

        assert _parse_next_link(None) is None