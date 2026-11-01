"""An unwired starting point for a future SimpleFIN integration. No network calls."""

from aggregators.base import AccountBalance, LinkStart


class SimpleFINAggregator:
    def link(self, kind, product_set, redirect_base, item=None) -> LinkStart:
        # Claim the setup token; encrypt credentials with core.crypto.encrypt before saving.
        raise NotImplementedError("SimpleFIN linking is not implemented; use Plaid or file import.")

    def sync_accounts(self, item, *, session=None) -> list[AccountBalance]:
        # Map with core.types.infer_type/is_liability_type; use Decimal balances and
        # core.snapshots.record_daily(source='simplefin'); liabilities are positive amounts owed.
        raise NotImplementedError("SimpleFIN account and balance sync is not implemented.")

    def sync_transactions(self, item, *, session=None) -> None:
        # Upsert core.models.Transaction by stable external ID; outflows are negative.
        raise NotImplementedError("SimpleFIN transaction sync is not implemented.")

    def sync_holdings(self, item, *, today=None, session=None) -> None:
        # Write core.models.Holding using Decimal quantities/values if the provider supplies them.
        raise NotImplementedError("SimpleFIN holdings sync is not implemented.")

    def sync_liabilities(self, item, *, session=None) -> None:
        # Save supplied terms with core.models.AccountFact; never invent missing values.
        raise NotImplementedError("SimpleFIN liability details are not implemented.")
