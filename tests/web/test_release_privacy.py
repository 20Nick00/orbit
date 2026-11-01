import imaplib
import json
import logging
from datetime import date
from pathlib import Path

import httpx
import plaid
from cryptography.fernet import Fernet

from aggregators.plaid import PlaidClient
from app.routers import paycheck
from core.config import get_settings
from core.crypto import encrypt
from core.db import session_scope
from core.models import Account, AccountFact, Institution, PaystubJobState, PlaidItem
from jobs.nightly import sync_item
from paystubs.config import Settings as PaystubSettings
from paystubs.gmail_fetch import fetch_once
from paystubs.service import JOB_NAME, MailboxJob
from property.rentcast import RentCastClient


def test_provider_failures_never_log_or_respond_with_secrets(client, monkeypatch, tmp_path, caplog):
    private = {
        "access": "SENTINEL_ACCESS_TOKEN",
        "public": "sentinel-public-token",
        "link": "sentinel-link-token",
        "state": "sentinel-state",
        "client": "sentinel-client-id",
        "secret": "sentinel-plaid-secret",
        "rentcast": "sentinel-rentcast-key",
        "password": "sentinel-imap-password",
        "paystub": "sentinel-private-paystub-text",
        "account": "9876543210987654",
        "fernet": Fernet.generate_key().decode(),
    }
    for name, value in {
        "PLAID_CLIENT_ID": private["client"],
        "PLAID_SECRET": private["secret"],
        "FERNET_KEY": private["fernet"],
        "RENTCAST_API_KEY": private["rentcast"],
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(logging.getLogger("jobs.nightly"), "disabled", False)
    raw_message = " ".join(private.values())

    class BrokenPlaid:
        def fail(self, *args, **kwargs):
            error = plaid.ApiException(status=400, reason=raw_message)
            error.body = json.dumps(
                {
                    "error_code": private["access"],
                    "error_type": private["public"],
                    "request_id": private["account"],
                    "error_message": raw_message,
                }
            )
            raise error

        accounts_get = item_remove = fail

    sdk = PlaidClient(BrokenPlaid())
    monkeypatch.setattr("aggregators.plaid.PlaidClient", lambda: sdk)
    with session_scope() as session:
        institution = Institution(name="Example Bank", aggregator="plaid", external_id="example")
        session.add(institution)
        session.flush()
        item = PlaidItem(
            institution_id=institution.id,
            item_id="example-item",
            access_token_encrypted=encrypt(private["access"]),
        )
        home = Account(name="Example home", type="property")
        session.add_all([item, home])
        session.flush()
        item_id, home_id = item.id, home.id
        session.add(AccountFact(account_id=home_id, key="address", value="Example address"))
    assert not sync_item(item_id, client=sdk)
    with session_scope() as session:
        assert session.get(PlaidItem, item_id).last_error_code == "UNKNOWN_ERROR"
    responses = [client.get("/settings")]
    responses.append(client.post(f"/settings/plaid/items/{item_id}/remove"))
    assert responses[-1].status_code == 200 and "could not finish" in responses[-1].text

    def rentcast_fail(request):
        raise httpx.ReadTimeout(raw_message, request=request)

    # Exercise the real RentCast exception boundary through the web form.
    original_init = RentCastClient.__init__

    def init(self, api_key, **kwargs):
        original_init(self, api_key, transport=httpx.MockTransport(rentcast_fail))

    monkeypatch.setattr(RentCastClient, "__init__", init)
    response = client.post(f"/account/{home_id}/valuation/refresh", follow_redirects=False)
    assert response.status_code == 303 and "message=failed" in response.headers["location"]
    responses.append(response)

    settings = PaystubSettings(
        data_dir=tmp_path / "pay",
        background=False,
        imap_host="example.invalid",
        imap_user="example",
        imap_password=private["password"],
        imap_sender=("pay@example.invalid",),
    )

    class BrokenIMAP:
        def __init__(self, *args, **kwargs):
            pass

        def login(self, user, password):
            assert password == private["password"]
            raise imaplib.IMAP4.error(raw_message)

        def logout(self):
            raise imaplib.IMAP4.error(raw_message)

    def fetch(settings, seen, **kwargs):
        return fetch_once(settings, seen, imap_factory=BrokenIMAP, **kwargs)

    monkeypatch.setattr("paystubs.service.fetch_once", fetch)
    monkeypatch.setattr(paycheck, "settings_for_request", lambda: settings)
    MailboxJob(settings).run(date(2026, 10, 31))
    with session_scope() as session:
        assert (
            "IMAP connection or fetch failed" in session.get(PaystubJobState, JOB_NAME).last_error
        )
    responses.append(client.get("/paycheck"))
    assert responses[-1].status_code == 200
    assert "Plaid item=" in caplog.text  # Ensure the logging assertion is not vacuous.
    for secret in private.values():
        assert secret not in caplog.text
        for response in responses:
            assert secret not in response.text
            assert secret not in str(response.headers)


def test_crypto_example_is_consistent():
    data = json.loads(Path("core/demo_data.json").read_text(encoding="utf-8"))
    account = next(account for account in data["accounts"] if account["name"] == "Crypto wallet")
    assert account["inst"] == "Example Exchange" and account["bal"] == 8200
    assert account["sub"] == "USDC"
    assert account["facts"] == [
        ["Asset", "USDC"],
        ["Balance", "$8,200"],
        ["Storage", "Example wallet"],
        ["Source", "Manual entry"],
    ]
