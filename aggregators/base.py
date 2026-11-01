"""Provider-neutral connection and synchronization contract."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class LinkStart:
    url: str
    state: str


@dataclass(frozen=True)
class AccountBalance:
    account_id: int
    balance: Decimal


@runtime_checkable
class Aggregator(Protocol):
    def link(self, kind, product_set, redirect_base, item=None) -> LinkStart: ...

    def sync_accounts(self, item, *, session=None) -> list[AccountBalance]: ...

    def sync_transactions(self, item, *, session=None) -> None: ...

    def sync_holdings(self, item, *, today=None, session=None) -> None: ...

    def sync_liabilities(self, item, *, session=None) -> None: ...
