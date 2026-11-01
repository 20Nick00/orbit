import base64
import hashlib
import json
import threading
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.main import create_app
from app.routers import plaid_webhook
from app.webhook_app import app as webhook_app
from core.config import get_settings
from core.models import Account, BalanceSnapshot, PlaidItem, Transaction
from jobs import nightly
from tests.csrf_client import TestClient

from .fake import page, transaction
from .test_sync import rows


@pytest.fixture
def web_client(database, configured, fake, monkeypatch):
    monkeypatch.setattr("aggregators.plaid.PlaidClient", lambda: fake)
    monkeypatch.setattr(plaid_webhook, "PlaidClient", lambda: fake)
    with TestClient(webhook_app) as client:
        yield client


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def encode_json(value):
    return b64(json.dumps(value, separators=(",", ":")).encode())


def signed(key, body, *, alg="ES256", kid="test-key", iat=None, digest=None):
    header = encode_json({"alg": alg, "kid": kid})
    payload = encode_json(
        {
            "iat": time.time() if iat is None else iat,
            "request_body_sha256": digest or hashlib.sha256(body).hexdigest(),
        }
    )
    message = f"{header}.{payload}"
    r, s = decode_dss_signature(key.sign(message.encode(), ec.ECDSA(hashes.SHA256())))
    return message + "." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


@pytest.fixture
def signing(fake):
    plaid_webhook._keys.clear()
    plaid_webhook._key_fetches.clear()
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_numbers()
    fake.keys["test-key"] = {
        "kid": "test-key",
        "alg": "ES256",
        "kty": "EC",
        "crv": "P-256",
        "expired_at": None,
        "x": b64(public.x.to_bytes(32, "big")),
        "y": b64(public.y.to_bytes(32, "big")),
    }
    yield key
    plaid_webhook._keys.clear()
    plaid_webhook._key_fetches.clear()


def body(**values):
    return json.dumps(
        {
            "webhook_type": "TRANSACTIONS",
            "webhook_code": "SYNC_UPDATES_AVAILABLE",
            "item_id": "remote-item",
            **values,
        }
    ).encode()


def test_valid_webhook_queues_and_caches(web_client, item_id, fake, signing, monkeypatch):
    calls = []
    monkeypatch.setattr(plaid_webhook, "sync_transactions", lambda value: calls.append(value))
    raw = body()
    for _ in range(2):
        response = web_client.post(
            "/plaid/webhook",
            content=raw,
            headers={"Plaid-Verification": signed(signing, raw), "Sec-Fetch-Site": "cross-site"},
        )
        assert response.status_code == 200
    assert calls == [item_id, item_id]
    assert [name for name, _ in fake.calls].count("get_webhook_key") == 1


@pytest.mark.parametrize(
    "failure",
    [
        "forged",
        "none",
        "HS256",
        "hash",
        "stale",
        "future",
        "unknown",
        "expired",
        "missing",
        "malformed",
        "boolean-iat",
    ],
)
def test_invalid_webhooks_rejected(web_client, item_id, fake, signing, monkeypatch, failure):
    calls = []
    monkeypatch.setattr(plaid_webhook, "sync_transactions", lambda value: calls.append(value))
    raw, options = body(), {}
    key = signing
    if failure == "forged":
        key = ec.generate_private_key(ec.SECP256R1())
    if failure in {"none", "HS256"}:
        options["alg"] = failure
    if failure == "hash":
        options["digest"] = "0" * 64
    if failure == "stale":
        options["iat"] = time.time() - 301
    if failure == "future":
        options["iat"] = time.time() + 120
    if failure == "boolean-iat":
        options["iat"] = True
    if failure == "unknown":
        options["kid"] = "unknown"
    if failure == "expired":
        fake.keys["test-key"]["expired_at"] = int(time.time())
    token = signed(key, raw, **options)
    headers = {} if failure == "missing" else {"Plaid-Verification": token}
    if failure == "malformed":
        headers = {"Plaid-Verification": "broken"}
    response = web_client.post("/plaid/webhook", content=raw, headers=headers)
    assert response.status_code == 401
    assert response.content == b""
    assert not calls


@pytest.mark.parametrize(
    "values",
    [
        {"item_id": "unknown"},
        {"webhook_code": "OTHER"},
        {"webhook_type": "ITEM"},
    ],
)
def test_other_webhooks_ignore(web_client, item_id, signing, monkeypatch, values):
    calls = []
    monkeypatch.setattr(plaid_webhook, "sync_transactions", lambda value: calls.append(value))
    raw = body(**values)
    response = web_client.post(
        "/plaid/webhook", content=raw, headers={"Plaid-Verification": signed(signing, raw)}
    )
    assert response.status_code == 200 and not calls


def test_body_limit_and_missing_configuration(web_client, monkeypatch):
    assert web_client.post("/plaid/webhook", content=b"x" * (1024 * 1024 + 1)).status_code == 401
    monkeypatch.setenv("PLAID_SECRET", "")
    get_settings.cache_clear()
    assert web_client.post("/plaid/webhook", content=body()).status_code == 503


def test_new_key_fetched_and_expiration_rechecked(fake, signing):
    fake.keys["second-key"] = fake.keys["test-key"] | {"kid": "second-key"}
    raw = body()
    assert plaid_webhook.verify(raw, signed(signing, raw), fake)
    assert plaid_webhook.verify(raw, signed(signing, raw, kid="second-key"), fake)
    fake.keys["test-key"] = fake.keys["test-key"] | {"expired_at": int(time.time())}
    timestamp, key = plaid_webhook._keys["test-key"]
    plaid_webhook._keys["test-key"] = (timestamp - 301, key)
    assert not plaid_webhook.verify(raw, signed(signing, raw), fake)


def test_verified_webhook_runs_real_sync_and_handles_failure(web_client, item_id, fake, signing):
    assert plaid_webhook.sync_transactions is nightly.sync_transactions
    # This also exercises discovery of an account and its initial balance in the webhook path.
    fake.pages = [page(added=[transaction()], next_cursor="saved")]
    raw = body()
    response = web_client.post(
        "/plaid/webhook", content=raw, headers={"Plaid-Verification": signed(signing, raw)}
    )
    assert response.status_code == 200
    assert len(rows(Account)) == len(rows(BalanceSnapshot)) == len(rows(Transaction)) == 1
    assert rows(PlaidItem)[0].transactions_cursor == "saved"
    fake.errors["sync_transactions"] = "ITEM_LOCKED"
    response = web_client.post(
        "/plaid/webhook", content=raw, headers={"Plaid-Verification": signed(signing, raw)}
    )
    assert response.status_code == 200
    assert rows(PlaidItem)[0].last_error_code == "ITEM_LOCKED"
    assert rows(PlaidItem)[0].status == "login_required"
    assert rows(PlaidItem)[0].transactions_cursor == "saved"
    assert len(rows(Transaction)) == 1


def test_failed_key_cached_for_sixty_seconds_and_cache_bounded(fake, signing, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(plaid_webhook.time, "monotonic", lambda: now[0])
    raw = body()
    token = signed(signing, raw, kid="unknown")
    assert not plaid_webhook.verify(raw, token, fake)
    now[0] += 59
    assert not plaid_webhook.verify(raw, token, fake)
    assert [name for name, _ in fake.calls].count("get_webhook_key") == 1
    now[0] += 1
    assert not plaid_webhook.verify(raw, token, fake)
    assert [name for name, _ in fake.calls].count("get_webhook_key") == 2
    for index in range(40):
        now[0] += 61
        assert not plaid_webhook.verify(raw, signed(signing, raw, kid=f"bad-{index}"), fake)
    assert len(plaid_webhook._keys) == 32


def test_key_fetch_does_not_hold_cache_lock(fake, signing, monkeypatch):
    original = fake.get_webhook_key

    def unlocked(kid):
        assert plaid_webhook._key_lock.acquire(blocking=False)
        plaid_webhook._key_lock.release()
        return original(kid)

    monkeypatch.setattr(fake, "get_webhook_key", unlocked)
    raw = body()
    assert plaid_webhook.verify(raw, signed(signing, raw), fake)


@pytest.mark.parametrize("host", ["hooks.example", "localhost", "127.0.0.1", "unknown.example"])
def test_webhook_listener_only_serves_webhooks(web_client, item_id, fake, signing, host):
    fake.pages = [page(added=[transaction()])]
    raw = body()
    with TestClient(webhook_app) as client:
        response = client.post(
            "/plaid/webhook",
            content=raw,
            headers={"Host": host, "Plaid-Verification": signed(signing, raw)},
        )
        assert response.status_code == 200 and len(rows(Transaction)) == 1
        for path in ("/settings", "/dashboard", "/", "/docs", "/redoc", "/openapi.json"):
            assert client.get(path, headers={"Host": host}).status_code == 404
        assert (
            client.post(
                f"/settings/plaid/items/{item_id}/remove", headers={"Host": host}
            ).status_code
            == 404
        )
    assert len(rows(PlaidItem)) == 1
    assert not any(name == "remove_item" for name, _ in fake.calls)


def test_main_app_has_no_webhook_with_any_host(configured, monkeypatch):
    monkeypatch.setenv("ORBIT_ALLOWED_HOSTS", "*")
    monkeypatch.setenv("PLAID_WEBHOOK_URL", "https://hooks.example/plaid/webhook")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        for host in ("localhost", "hooks.example", "unknown.example"):
            assert client.post("/plaid/webhook", headers={"Host": host}).status_code == 404


def test_webhook_url_does_not_expand_allowed_hosts(configured, monkeypatch):
    monkeypatch.setenv("ORBIT_ALLOWED_HOSTS", "localhost")
    monkeypatch.setenv("PLAID_WEBHOOK_URL", "https://hooks.example/plaid/webhook")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        assert client.get("/settings", headers={"Host": "hooks.example"}).status_code == 400


def test_random_key_ids_are_globally_limited(fake, signing, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(plaid_webhook.time, "monotonic", lambda: now[0])
    raw = body()
    # A cached valid key remains usable even when random unknown keys exhaust the limit.
    assert plaid_webhook.verify(raw, signed(signing, raw), fake)
    for index in range(30):
        assert not plaid_webhook.verify(raw, signed(signing, raw, kid=f"random-{index}"), fake)
    assert [name for name, _ in fake.calls].count("get_webhook_key") == 10
    assert plaid_webhook.verify(raw, signed(signing, raw), fake)
    now[0] += 59
    assert not plaid_webhook.verify(raw, signed(signing, raw, kid="too-soon"), fake)
    assert [name for name, _ in fake.calls].count("get_webhook_key") == 10
    now[0] += 1
    assert not plaid_webhook.verify(raw, signed(signing, raw, kid="next-minute"), fake)
    assert [name for name, _ in fake.calls].count("get_webhook_key") == 11


@pytest.mark.parametrize("bind", [None, "0.0.0.0"])
def test_listener_lifespan_starts_daemon_and_stops(configured, monkeypatch, bind):
    import app.main as main

    instances = []
    started = threading.Event()

    class Server:
        should_exit = False

        def __init__(self, config):
            self.config = config
            instances.append(self)

        def run(self):
            self.thread = threading.current_thread()
            started.set()
            while not self.should_exit:
                time.sleep(0.01)

    monkeypatch.setattr(main.uvicorn, "Server", Server)
    monkeypatch.setenv("ORBIT_WEBHOOK_PORT", "8001")
    if bind is not None:
        monkeypatch.setenv("ORBIT_WEBHOOK_BIND", bind)
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        assert started.wait(2)
        server = instances[0]
        assert server.config.app is webhook_app
        assert server.config.host == (bind or "127.0.0.1")
        assert server.config.port == 8001
        assert server.thread.daemon and server.thread.is_alive()
        assert client.post("/plaid/webhook").status_code == 404
    assert server.should_exit and not server.thread.is_alive()


def test_no_listener_without_port(configured, monkeypatch):
    import app.main as main

    def forbidden(*args, **kwargs):
        pytest.fail("No webhook server should be constructed")

    monkeypatch.setattr(main.uvicorn, "Server", forbidden)
    with TestClient(create_app()) as client:
        assert client.post("/plaid/webhook").status_code == 404


@pytest.mark.parametrize("failure", [OSError, SystemExit])
def test_listener_startup_failure_keeps_main_app_running(configured, monkeypatch, caplog, failure):
    import logging

    import app.main as main

    finished = threading.Event()

    class Server:
        def __init__(self, config):
            pass

        def run(self):
            try:
                logging.getLogger("uvicorn.error").error("private-bind-details")
                raise failure("private-startup-details")
            finally:
                finished.set()

    monkeypatch.setattr(main.uvicorn, "Server", Server)
    monkeypatch.setenv("ORBIT_WEBHOOK_PORT", "8001")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        assert finished.wait(2)
        assert client.get("/", follow_redirects=False).status_code == 307
    assert f"Webhook startup: {failure.__name__}" in caplog.text
    assert "private" not in caplog.text
