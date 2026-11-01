import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from aggregators.plaid import PlaidClient, PlaidError
from core.config import Settings, get_settings
from core.crypto import CryptoError, decrypt, encrypt


def test_crypto_round_trip(configured):
    encrypted = encrypt("private-access-token")
    assert "private-access-token" not in encrypted
    assert decrypt(encrypted) == "private-access-token"


@pytest.mark.parametrize("key", ["", "not-a-key", "💥"])
def test_missing_invalid_key(configured, monkeypatch, key):
    monkeypatch.setenv("FERNET_KEY", key)
    get_settings.cache_clear()
    with pytest.raises(CryptoError, match="FERNET_KEY"):
        encrypt("private-access-token")


def test_wrong_key(configured, monkeypatch):
    token = encrypt("private-access-token")
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    with pytest.raises(CryptoError, match="cannot be decrypted"):
        decrypt(token)


def test_environment_validation():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, PLAID_ENV="development")
    assert Settings(_env_file=None, PLAID_ENV="production").plaid_env == "production"


def test_sandbox_helpers_reject_production(configured, monkeypatch):
    monkeypatch.setenv("PLAID_ENV", "production")
    get_settings.cache_clear()
    api = StubApi()
    client = PlaidClient(api)
    with pytest.raises(PlaidError, match="SANDBOX_ONLY"):
        client.sandbox_public_token()
    with pytest.raises(PlaidError, match="SANDBOX_ONLY"):
        client.sandbox_reset_login("access")
    assert not api.calls


class StubApi:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(request, **kwargs):
            self.calls.append((name, request))
            assert kwargs["_request_timeout"] == 30
            return SimpleNamespace(to_dict=lambda: {"request_id": "safe-request"})

        return call


@pytest.mark.parametrize(
    "method,args,endpoint,model,fields",
    [
        (
            "get_link_token",
            ("link",),
            "link_token_get",
            "LinkTokenGetRequest",
            {"link_token": "link"},
        ),
        (
            "exchange_public_token",
            ("public",),
            "item_public_token_exchange",
            "ItemPublicTokenExchangeRequest",
            {"public_token": "public"},
        ),
        ("get_item", ("access",), "item_get", "ItemGetRequest", {"access_token": "access"}),
        (
            "get_institution",
            ("ins_1",),
            "institutions_get_by_id",
            "InstitutionsGetByIdRequest",
            {"institution_id": "ins_1"},
        ),
        (
            "get_accounts",
            ("access",),
            "accounts_get",
            "AccountsGetRequest",
            {"access_token": "access"},
        ),
        (
            "sync_transactions",
            ("access", "cursor", 500),
            "transactions_sync",
            "TransactionsSyncRequest",
            {"cursor": "cursor", "count": 500},
        ),
        (
            "get_holdings",
            ("access",),
            "investments_holdings_get",
            "InvestmentsHoldingsGetRequest",
            {"access_token": "access"},
        ),
        (
            "get_liabilities",
            ("access",),
            "liabilities_get",
            "LiabilitiesGetRequest",
            {"access_token": "access"},
        ),
        (
            "remove_item",
            ("access",),
            "item_remove",
            "ItemRemoveRequest",
            {"access_token": "access"},
        ),
        (
            "get_webhook_key",
            ("kid",),
            "webhook_verification_key_get",
            "WebhookVerificationKeyGetRequest",
            {"key_id": "kid"},
        ),
        (
            "sandbox_public_token",
            (),
            "sandbox_public_token_create",
            "SandboxPublicTokenCreateRequest",
            {"institution_id": "ins_109508"},
        ),
        (
            "sandbox_reset_login",
            ("access",),
            "sandbox_item_reset_login",
            "SandboxItemResetLoginRequest",
            {"access_token": "access"},
        ),
    ],
)
def test_sdk_request_models(configured, method, args, endpoint, model, fields):
    api = StubApi()
    client = PlaidClient(api)
    assert getattr(client, method)(*args) == {"request_id": "safe-request"}
    called, request = api.calls[0]
    assert called == endpoint
    assert type(request).__name__ == model
    assert type(request).__module__.startswith("plaid.model.")
    for key, value in fields.items():
        assert getattr(request, key) == value


@pytest.mark.parametrize("update", [False, True])
def test_link_request_models(configured, update):
    api = StubApi()
    values = dict(
        client_name="Orbit",
        language="en",
        country_codes=["US"],
        user={"client_user_id": "orbit-owner"},
        hosted_link={
            "completion_redirect_uri": "http://localhost/settings/plaid/callback?state=abc"
        },
        webhook="https://example.com/plaid/webhook",
    )
    values.update(
        {"access_token": "access"}
        if update
        else {
            "products": ["transactions"],
            "optional_products": ["investments", "liabilities"],
            "transactions": {"days_requested": 730},
        }
    )
    PlaidClient(api).create_link_token(**values)
    request = api.calls[0][1]
    assert type(request).__name__ == "LinkTokenCreateRequest"
    assert request.user.client_user_id == "orbit-owner"
    assert request.country_codes[0].value == "US"
    assert request.hosted_link.completion_redirect_uri.endswith("state=abc")
    assert request.webhook == values["webhook"]
    if update:
        assert "products" not in request.to_dict()
    else:
        assert request.products[0].value == "transactions"
        assert request.transactions.days_requested == 730
        assert {value.value for value in request.optional_products} == {
            "investments",
            "liabilities",
        }


def test_error_discards_body(configured):
    import plaid

    class Broken:
        def accounts_get(self, request, **kwargs):
            error = plaid.ApiException(status=400)
            error.body = json.dumps(
                {
                    "error_code": "ITEM_LOGIN_REQUIRED",
                    "error_type": "ITEM_ERROR",
                    "request_id": "safe-request",
                    "error_message": "private-access-token",
                }
            )
            raise error

    with pytest.raises(PlaidError) as caught:
        PlaidClient(Broken()).get_accounts("private-access-token")
    assert caught.value.error_code == "ITEM_LOGIN_REQUIRED"
    assert caught.value.request_id == "safe-request"
    assert "private-access-token" not in str(caught.value)
    assert caught.value.__suppress_context__
