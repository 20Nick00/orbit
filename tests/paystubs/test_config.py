from pathlib import Path

import pytest

from paystubs.config import ROOT, Settings, load_env_file


def test_defaults_and_directories():
    settings = Settings.from_env({})
    assert settings.data_dir == ROOT / "data"
    assert settings.rules_dir == ROOT / "data" / "paystubs" / "rules"
    assert settings.inbox_dir == ROOT / "data" / "inbox" / "paystubs"
    assert settings.archive_dir == ROOT / "data" / "paystubs" / "archive"
    assert settings.pending_dir.name == "pending"
    assert settings.failed_dir.name == "failed"
    assert settings.duplicates_dir.name == "duplicates"
    assert settings.imap_port == 993
    assert settings.imap_lookback_days == 45
    assert settings.imap_day_of_month == 1
    assert settings.hsa_coverage == "self"
    assert not settings.imap_enabled


def test_environment_mapping_and_hidden_password(tmp_path):
    settings = Settings.from_env(
        {
            "ORBIT_DATA_DIR": str(tmp_path),
            "PAYSTUB_RULES_DIR": "custom-rules",
            "PAYSTUB_WATCH_POLLING": "1",
            "PAYSTUB_HSA_COVERAGE": "family",
            "PAYSTUB_IMAP_HOST": "imap.test.invalid",
            "PAYSTUB_IMAP_PORT": "1993",
            "PAYSTUB_IMAP_USER": "fake-user",
            "PAYSTUB_IMAP_PASSWORD": "fake-secret",
            "PAYSTUB_IMAP_SENDER": " one@test.invalid, ,two@test.invalid ",
            "PAYSTUB_IMAP_LABEL": "Payroll",
            "PAYSTUB_IMAP_LOOKBACK_DAYS": "20",
            "PAYSTUB_IMAP_DAY_OF_MONTH": "31",
        }
    )
    assert settings.data_dir == tmp_path
    assert settings.rules_dir == Path("custom-rules")
    assert settings.watch_polling
    assert settings.hsa_coverage == "family"
    assert settings.imap_sender == ("one@test.invalid", "two@test.invalid")
    assert settings.imap_enabled
    assert settings.imap_port == 1993
    assert settings.imap_lookback_days == 20
    assert settings.imap_day_of_month == 31
    assert "fake-secret" not in repr(settings)


@pytest.mark.parametrize(
    "env",
    [
        {"PAYSTUB_IMAP_DAY_OF_MONTH": "0"},
        {"PAYSTUB_IMAP_PORT": "99999"},
        {"PAYSTUB_IMAP_LOOKBACK_DAYS": "-1"},
    ],
)
def test_invalid_settings(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)


def test_env_reader_quotes_comments_and_no_override(tmp_path, monkeypatch):
    for key in (
        "ORBIT_TEST_A",
        "ORBIT_TEST_B",
        "ORBIT_TEST_C",
        "ORBIT_TEST_D",
        "ORBIT_TEST_E",
        "ORBIT_TEST_BAD",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ORBIT_TEST_A", "existing")
    path = tmp_path / ".env"
    path.write_text(
        '# comment\nORBIT_TEST_A=replacement\nORBIT_TEST_B="two # words" # comment\n'
        "ORBIT_TEST_C='C:\\fake\\path'\nORBIT_TEST_D=plain # comment\nORBIT_TEST_E=a=b\n"
        'ORBIT_TEST_BAD="unterminated\nnot an assignment\n',
        encoding="utf-8",
    )
    load_env_file(path)
    import os

    assert os.environ["ORBIT_TEST_A"] == "existing"
    assert os.environ["ORBIT_TEST_B"] == "two # words"
    assert os.environ["ORBIT_TEST_C"] == "C:\\fake\\path"
    assert os.environ["ORBIT_TEST_D"] == "plain"
    assert os.environ["ORBIT_TEST_E"] == "a=b"
    assert "ORBIT_TEST_BAD" not in os.environ
    load_env_file(tmp_path / "absent")
