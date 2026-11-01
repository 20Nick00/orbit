"""Explicitly opt-in Sandbox checks. No production account may enter this module."""

import os
import time

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from aggregators.plaid import PlaidAggregator, PlaidClient, PlaidError
from aggregators.plaid_link import _accept
from core.config import get_settings
from core.crypto import decrypt
from core.db import session_scope
from core.models import BalanceSnapshot, Institution, PlaidItem, PlaidLinkSession, Transaction
from jobs.nightly import run

pytestmark = pytest.mark.plaid_sandbox

if os.environ.get("ORBIT_RUN_SANDBOX") != "1":
    pytest.skip("Sandbox tests require explicit ORBIT_RUN_SANDBOX=1", allow_module_level=True)

sandbox_settings = get_settings()
if not sandbox_settings.plaid_configured:
    pytest.skip(
        "PLAID_CLIENT_ID and PLAID_SECRET are required for Sandbox", allow_module_level=True
    )
if sandbox_settings.plaid_env != "sandbox":
    pytest.skip(
        "Sandbox tests require PLAID_ENV=sandbox; production is never used", allow_module_level=True
    )


def test_sandbox_lifecycle(database, monkeypatch):
    # The ordinary test fixtures suppress .env. Preserve this opt-in configuration.
    monkeypatch.setenv("PLAID_CLIENT_ID", sandbox_settings.plaid_client_id.get_secret_value())
    monkeypatch.setenv("PLAID_SECRET", sandbox_settings.plaid_secret.get_secret_value())
    monkeypatch.setenv("PLAID_ENV", sandbox_settings.plaid_env)
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    client = PlaidClient()
    aggregator = PlaidAggregator(client)
    token = None
    item_id = None
    try:
        public = client.sandbox_public_token(
            "ins_109508", ["transactions", "investments", "liabilities"]
        )
        item_id, duplicate = _accept(aggregator, public["public_token"])
        assert not duplicate and item_id is not None
        with session_scope() as session:
            item = session.get(PlaidItem, item_id)
            token = decrypt(item.access_token_encrypted)
            assert session.get(Institution, item.institution_id).external_id
            assert session.scalar(select(func.count()).select_from(BalanceSnapshot)) > 0
        deadline = time.monotonic() + 60
        while True:
            try:
                aggregator.sync_transactions(item_id)
            except PlaidError as exc:
                if exc.error_code != "PRODUCT_NOT_READY":
                    raise
            with session_scope() as session:
                count = session.scalar(select(func.count()).select_from(Transaction))
            if count:
                break
            if time.monotonic() >= deadline:
                pytest.fail("Sandbox initial transactions were not ready within 60 seconds")
            time.sleep(2)
        aggregator.sync_holdings(item_id)
        aggregator.sync_liabilities(item_id)
        link = aggregator.link("connect", "banking", "https://example.com/settings/plaid/callback")
        assert link.url.startswith("https://")
        with session_scope() as session:
            hosted = session.scalar(
                select(PlaidLinkSession).where(PlaidLinkSession.state == link.state)
            )
            link_token = decrypt(hosted.link_token_encrypted)
        sessions = client.get_link_token(link_token).get("link_sessions") or []
        assert isinstance(sessions, list)
        assert run(client=client).succeeded == 1
        with session_scope() as session:
            assert session.scalar(select(func.count()).select_from(BalanceSnapshot)) > 0
        client.sandbox_reset_login(token)
        assert run(client=client).failed == 1
        with session_scope() as session:
            assert session.get(PlaidItem, item_id).status == "login_required"
        aggregator.remove(item_id)
        token = None
        with session_scope() as session:
            assert session.get(PlaidItem, item_id) is None
            assert session.scalar(select(func.count()).select_from(BalanceSnapshot)) > 0
    finally:
        if token:
            if item_id is None:
                client.remove_item(token)
            else:
                aggregator.remove(item_id)
        get_settings.cache_clear()
