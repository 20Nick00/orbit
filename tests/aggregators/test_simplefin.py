import inspect

import pytest

from aggregators.base import Aggregator
from aggregators.simplefin import SimpleFINAggregator


def test_stub_conforms_to_protocol():
    assert isinstance(SimpleFINAggregator(), Aggregator)


@pytest.mark.parametrize(
    "method,args",
    [
        ("link", ("connect", "banking", "http://localhost/callback")),
        ("sync_accounts", (1,)),
        ("sync_transactions", (1,)),
        ("sync_holdings", (1,)),
        ("sync_liabilities", (1,)),
    ],
)
def test_stub_signatures_and_helpful_error(method, args):
    assert inspect.signature(getattr(SimpleFINAggregator, method)) == inspect.signature(
        getattr(Aggregator, method)
    )
    with pytest.raises(NotImplementedError, match="SimpleFIN.*not implemented"):
        getattr(SimpleFINAggregator(), method)(*args)
