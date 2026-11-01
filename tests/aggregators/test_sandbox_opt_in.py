import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "opt_in,configured,environment,reason",
    [
        (None, True, "sandbox", "ORBIT_RUN_SANDBOX=1"),
        (None, True, "production", "ORBIT_RUN_SANDBOX=1"),
        ("0", True, "sandbox", "ORBIT_RUN_SANDBOX=1"),
        ("1", False, "sandbox", "PLAID_CLIENT_ID and PLAID_SECRET"),
        ("1", True, "production", "PLAID_ENV=sandbox"),
    ],
)
def test_sandbox_module_skips_before_any_client_use(
    monkeypatch, opt_in, configured, environment, reason
):
    def forbidden(*args, **kwargs):
        pytest.fail("Skipped sandbox tests must never construct a Plaid client")

    monkeypatch.setattr("aggregators.plaid.PlaidClient", forbidden)
    monkeypatch.setattr(
        "core.config.get_settings",
        lambda: SimpleNamespace(plaid_configured=configured, plaid_env=environment),
    )
    if opt_in is None:
        monkeypatch.delenv("ORBIT_RUN_SANDBOX", raising=False)
    else:
        monkeypatch.setenv("ORBIT_RUN_SANDBOX", opt_in)
    with pytest.raises(pytest.skip.Exception, match=reason):
        runpy.run_path(str(Path(__file__).with_name("test_sandbox.py")))
