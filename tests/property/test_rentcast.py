import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import quote, quote_plus

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import Settings, get_settings
from core.models import RentcastCache, RentcastUsage
from property.rentcast import (
    NoEstimateError,
    NotConfiguredError,
    QuotaExceededError,
    RentCastAuthError,
    RentCastClient,
    RentCastError,
    address_key,
)

TODAY = dt.date(2026, 4, 12)
ADDRESS = "123 Private St., Unit #4"
KEY = "test-private-api-key"


def test_shape_cache_normalization_and_month(database):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.rentcast.io"
        assert request.url.path == "/v1/avm/value"
        assert request.url.params["address"] == ADDRESS
        assert request.headers["X-Api-Key"] == KEY
        assert request.extensions["timeout"]["read"] == 20
        return httpx.Response(
            200, json={"price": 456789.12, "priceRangeLow": 400000, "priceRangeHigh": 500000}
        )

    client = RentCastClient(KEY, transport=httpx.MockTransport(handle))
    first = client.estimate(ADDRESS, today=TODAY)
    assert first.price == Decimal("456789.12") and not first.from_cache
    assert first.low == Decimal("400000") and first.high == Decimal("500000")
    assert client.estimate(" 123 PRIVATE St Unit 4 ", today=TODAY).from_cache
    # Cache remains usable without a key and at the limit.
    assert RentCastClient(None).estimate(ADDRESS, today=TODAY).from_cache
    with Session(database) as active:
        assert active.get(RentcastUsage, TODAY).requests == 1
    client.estimate(ADDRESS, today=dt.date(2026, 5, 1))
    assert len(requests) == 2


def assert_private(text):
    for private in (ADDRESS, quote(ADDRESS, safe=""), quote_plus(ADDRESS), KEY):
        assert private not in text


@pytest.mark.parametrize(
    "status,error",
    [
        (401, RentCastAuthError),
        (404, NoEstimateError),
        (429, RentCastError),
        (500, RentCastError),
    ],
)
def test_status_privacy_failure_counts(database, caplog, monkeypatch, status, error):
    # Alembic disables existing loggers. Start at INFO so the production guard
    # must actively suppress httpx's request URLs for this test to pass.
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", False)
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.INFO, logger="httpx")
    client = RentCastClient(
        KEY,
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text=ADDRESS + KEY)),
    )
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    with pytest.raises(error) as caught:
        client.estimate(ADDRESS, today=TODAY)
    assert_private(str(caught.value) + caplog.text)
    with Session(database) as active:
        assert active.get(RentcastUsage, TODAY).requests == 1
        assert active.scalar(select(func.count()).select_from(RentcastCache)) == 0


def test_privacy_assertion_detects_missing_httpx_guard(database, caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", False)
    caplog.set_level(logging.INFO)
    client = RentCastClient(KEY, transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    # Deliberately undo just the production logger line: the very same privacy
    # assertion must now fail because httpx logs the URL-encoded address.
    caplog.set_level(logging.INFO, logger="httpx")
    with pytest.raises(RentCastError):
        client.estimate(ADDRESS, today=TODAY)
    assert any(r.name == "httpx" and r.levelno == logging.INFO for r in caplog.records)
    with pytest.raises(AssertionError):
        assert_private(caplog.text)


def test_timeout_no_retry_and_quota(database, monkeypatch):
    monkeypatch.setenv("ORBIT_RENTCAST_MONTHLY_LIMIT", "1")
    get_settings.cache_clear()
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout(ADDRESS + KEY, request=request)

    client = RentCastClient(KEY, transport=httpx.MockTransport(handle))
    with pytest.raises(RentCastError, match="^ReadTimeout$"):
        client.estimate(ADDRESS, today=TODAY)
    with pytest.raises(QuotaExceededError):
        client.estimate(ADDRESS, today=TODAY)
    assert len(calls) == 1
    with Session(database) as active:
        assert active.get(RentcastUsage, TODAY).requests == 1
        assert active.scalar(select(func.count()).select_from(RentcastCache)) == 0


@pytest.mark.parametrize("payload", [{}, {"price": 0}, {"price": -1}, {"price": "NaN"}])
def test_no_price_is_not_cached(database, payload):
    client = RentCastClient(
        KEY, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    with pytest.raises(NoEstimateError):
        client.estimate(ADDRESS, today=TODAY)
    with Session(database) as active:
        assert active.scalar(select(func.count()).select_from(RentcastCache)) == 0


def test_no_key_and_zero_limit_send_nothing(database, monkeypatch):
    def never(request):
        pytest.fail("Transport must not be called")

    with pytest.raises(NotConfiguredError):
        RentCastClient(None, transport=httpx.MockTransport(never)).estimate(ADDRESS, today=TODAY)
    monkeypatch.setenv("ORBIT_RENTCAST_MONTHLY_LIMIT", "0")
    get_settings.cache_clear()
    with pytest.raises(QuotaExceededError):
        RentCastClient(KEY, transport=httpx.MockTransport(never)).estimate(ADDRESS, today=TODAY)


def test_concurrent_quota_is_atomic(database, monkeypatch):
    monkeypatch.setenv("ORBIT_RENTCAST_MONTHLY_LIMIT", "1")
    get_settings.cache_clear()
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(500)

    def attempt(i):
        try:
            RentCastClient(KEY, transport=httpx.MockTransport(handle)).estimate(str(i), today=TODAY)
        except RentCastError:
            pass

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(attempt, range(4)))
    assert len(calls) == 1
    with Session(database) as active:
        assert active.get(RentcastUsage, TODAY).requests == 1


def test_settings_ceiling_and_alias(monkeypatch):
    monkeypatch.setenv("RENTCAST_API_KEY", KEY)
    assert Settings(_env_file=None).rentcast_api_key.get_secret_value() == KEY
    for limit in (-1, 51):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, rentcast_monthly_limit=limit)


def test_rolling_31_day_limit_across_month_boundary(database):
    today = dt.date(2026, 5, 2)
    with Session(database) as active, active.begin():
        active.add_all(
            [
                RentcastUsage(day=dt.date(2026, 4, 2), requests=20),
                RentcastUsage(day=dt.date(2026, 4, 30), requests=20),
                RentcastUsage(day=dt.date(2026, 5, 1), requests=5),
            ]
        )
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(500)

    client = RentCastClient(KEY, transport=httpx.MockTransport(handle))
    with pytest.raises(QuotaExceededError):
        client.estimate(ADDRESS, today=today)
    assert calls == []
    # April 2 is exactly 31 days old on May 3 and no longer counts.
    tomorrow = today + dt.timedelta(days=1)
    with pytest.raises(RentCastError):
        client.estimate(ADDRESS, today=tomorrow)
    assert len(calls) == 1
    with Session(database) as active:
        assert active.get(RentcastUsage, today) is None
        assert active.get(RentcastUsage, tomorrow).requests == 1


def test_address_normalization_preserves_hyphen_and_slash_separation():
    assert address_key("45-12 Broadway") != address_key("4512 Broadway")
    assert address_key("45/12 Broadway") == address_key("45-12 Broadway")
    assert address_key("123 Main St.") == address_key("123 main st")
