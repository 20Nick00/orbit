import pytest

from aggregators.plaid import PlaidAggregator
from app.main import create_app
from core.config import get_settings
from tests.csrf_client import TestClient


@pytest.mark.parametrize("kind", ["connect", "reauth"])
@pytest.mark.parametrize(
    "url,port,usable",
    [
        ("", "", False),
        ("https://hooks.example/plaid/webhook", "", False),
        ("http://hooks.example/plaid/webhook", "8001", False),
        ("https://hooks.example/wrong", "8001", False),
        ("https://hooks.example/plaid/webhook/", "8001", False),
        ("https:///plaid/webhook", "8001", False),
        ("https://[broken/plaid/webhook", "8001", False),
        ("https://user:private-value@hooks.example/plaid/webhook", "8001", False),
        ("https://hooks.example/plaid/webhook?private-value=1", "8001", False),
        ("https://hooks.example/plaid/webhook#private-value", "8001", False),
        ("https://hooks.example/plaid/webhook", "8001", True),
    ],
)
def test_webhook_link_and_settings_require_usable_setup(
    item_id, fake, monkeypatch, kind, url, port, usable
):
    monkeypatch.setenv("PLAID_WEBHOOK_URL", url)
    monkeypatch.setenv("ORBIT_WEBHOOK_PORT", port)
    get_settings.cache_clear()
    assert get_settings().plaid_webhook_usable == usable
    PlaidAggregator(fake).link(kind, "banking", "http://localhost/callback", item_id)
    values = fake.calls[0][1][0]
    assert ("webhook" in values) == usable
    if usable:
        assert values["webhook"] == url
    # No context manager: do not start real listeners while testing the settings page.
    client = TestClient(create_app())
    try:
        response = client.get("/settings")
        assert response.status_code == 200
        assert ("Set PLAID_WEBHOOK_URL" in response.text) == (bool(url) and not usable)
        assert "private-value" not in response.text
    finally:
        client.close()
