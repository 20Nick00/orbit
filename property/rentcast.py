"""RentCast requests with a persistent, atomically reserved rolling ceiling."""

import logging
import re
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from core.config import get_settings
from core.db import session_scope
from core.models import RentcastCache, RentcastUsage, _money


class RentCastError(Exception):
    pass


class NotConfiguredError(RentCastError):
    pass


class QuotaExceededError(RentCastError):
    pass


class RentCastAuthError(RentCastError):
    pass


class NoEstimateError(RentCastError):
    pass


@dataclass(frozen=True)
class Estimate:
    price: Decimal
    low: Decimal | None
    high: Decimal | None
    from_cache: bool


def address_key(address):
    separated = re.sub(r"[-/]", " ", address.lower())
    return " ".join(re.sub(r"[^\w\s]|_", "", separated).split())


def usage_total(today):
    # No upper bound: a request logged under a later date (a timezone change can
    # move today backwards) was still sent, so it still counts.
    return select(func.coalesce(func.sum(RentcastUsage.requests), 0)).where(
        RentcastUsage.day >= today - timedelta(days=30)
    )


def _reserve(active, today):
    limit = min(50, get_settings().rentcast_monthly_limit)
    if limit <= 0:
        raise QuotaExceededError("RentCast request limit reached for the past 31 days.")
    # This transaction MUST commit independently of the caller's valuation writes.
    # The SQL predicate, not a read followed by a write, arbitrates concurrent calls.
    bind = active.get_bind()
    engine = getattr(bind, "engine", bind)
    with Session(engine) as quota, quota.begin():
        # RentCast's billing period may start on the signup date. A trailing
        # 31-day ceiling protects every billing window, including month boundaries.
        allowed = usage_total(today).scalar_subquery() < limit
        statement = insert(RentcastUsage).from_select(
            ["day", "requests"], select(literal(today), literal(1)).where(allowed)
        )
        statement = statement.on_conflict_do_update(
            index_elements=["day"],
            set_={"requests": RentcastUsage.requests + 1},
        ).returning(RentcastUsage.requests)
        if quota.scalar(statement) is None:
            raise QuotaExceededError("RentCast request limit reached for the past 31 days.")


def _store_estimate(active, address, today, estimate):
    if estimate.from_cache:
        return
    statement = insert(RentcastCache).values(
        address_key=address_key(address),
        month=today.strftime("%Y-%m"),
        price=estimate.price,
        price_low=estimate.low,
        price_high=estimate.high,
    )
    active.execute(statement.on_conflict_do_nothing())
    active.flush()


class RentCastClient:
    def __init__(self, api_key, *, transport=None):
        # Request URLs contain the private address. Apply this on the production
        # path, even if an application logging configuration ran after import.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self.api_key = (
            api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else api_key
        )
        self.transport = transport

    def estimate(self, address, *, today, session=None):
        return self._estimate(address, today=today, session=session, persist=True)

    def _estimate(self, address, *, today, session, persist):
        try:
            return self._perform_estimate(address, today=today, session=session, persist=persist)
        except RentCastError:
            raise
        except Exception as exc:
            # Database errors can include address-key SQL parameters too.
            raise RentCastError(type(exc).__name__) from None

    def _perform_estimate(self, address, *, today, session, persist):
        month = today.strftime("%Y-%m")
        key = address_key(address)
        with nullcontext(session) if session is not None else session_scope() as active:
            cached = active.get(RentcastCache, (key, month))
            if cached:
                return Estimate(cached.price, cached.price_low, cached.price_high, True)
            if not self.api_key:
                raise NotConfiguredError("RentCast is not configured. Add RENTCAST_API_KEY.")
            _reserve(active, today)
            try:
                with httpx.Client(transport=self.transport, timeout=20) as client:
                    response = client.get(
                        "https://api.rentcast.io/v1/avm/value",
                        params={"address": address},
                        headers={"X-Api-Key": self.api_key},
                    )
            except httpx.HTTPError as exc:
                # No request URLs, addresses, keys, response bodies or chained exceptions.
                raise RentCastError(type(exc).__name__) from None
            if response.status_code == 401:
                raise RentCastAuthError("RentCast authentication failed (401).")
            if response.status_code == 404:
                raise NoEstimateError("No RentCast estimate is available (404).")
            if response.status_code != 200:
                raise RentCastError(f"RentCast returned status {response.status_code}.")
            try:
                payload = response.json()
                price = _money(Decimal(str(payload.get("price"))))
                if price <= 0:
                    raise ValueError
                low = payload.get("priceRangeLow")
                high = payload.get("priceRangeHigh")
                low = None if low is None else _money(Decimal(str(low)))
                high = None if high is None else _money(Decimal(str(high)))
            except (ValueError, TypeError, AttributeError, InvalidOperation):
                raise NoEstimateError("No valid RentCast estimate is available.") from None
            estimate = Estimate(price, low, high, False)
            if persist:
                _store_estimate(active, address, today, estimate)
            return estimate
