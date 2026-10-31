import hashlib
import hmac

from app.bitvavo.client import sign


def test_sign_matches_reference_hmac():
    secret = "topsecret"
    ts = 1_700_000_000_000
    body = '{"market":"BTC-EUR","side":"buy","orderType":"market","amountQuote":"25.00"}'
    expected = hmac.new(
        secret.encode(), f"{ts}POST/v2/order{body}".encode(), hashlib.sha256
    ).hexdigest()
    assert sign(secret, ts, "post", "/v2/order", body) == expected


def test_sign_get_without_body():
    assert sign("s", 1, "GET", "/v2/balance") == sign("s", 1, "GET", "/v2/balance", "")
