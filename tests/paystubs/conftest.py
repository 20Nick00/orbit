from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from app.main import create_app
from core import db
from core.config import get_settings
from core.models import PaystubPending
from paystubs.analysis import Stub
from paystubs.config import Settings
from paystubs.parsers.pdf import normalize, parse_pdf
from paystubs.rules import Rule, classify, get_rule
from paystubs.store import data_path, resolve_pending
from tests.csrf_client import TestClient

from .make_stubs import stub_a, stub_b


@pytest.fixture
def paystub_env(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", rules_dir=tmp_path / "rules", background=False)
    settings.inbox_dir.mkdir(parents=True)
    monkeypatch.setenv("ORBIT_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("PAYSTUB_RULES_DIR", str(settings.rules_dir))
    monkeypatch.setenv("PAYSTUB_BACKGROUND", "0")
    monkeypatch.setenv("PAYSTUB_HSA_COVERAGE", "self")
    url = "sqlite:///" + (settings.data_dir / "orbit.sqlite").as_posix()
    monkeypatch.setenv("ORBIT_DATABASE_URL", url)
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    get_settings.cache_clear()
    db.configure(url)
    command.upgrade(cfg, "head")
    try:
        yield settings
    finally:
        db.configure(None)
        get_settings.cache_clear()


@pytest.fixture
def web_client(paystub_env):
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def resolve(paystub_env):
    def run(pending_id, rule, **kwargs):
        values = dict(
            pending_id=pending_id,
            employer=rule.employer,
            match_text=rule.match[0],
            labels=rule.labels,
            limits=rule.limits,
            pay_date=None,
            period_start=None,
            period_end=None,
            accept_mismatch=False,
            settings=paystub_env,
        )
        return resolve_pending(**(values | kwargs))

    return run


@pytest.fixture
def mapping_form(paystub_env):
    def build(pending_id, rule):
        with Session(db.get_engine()) as session:
            row = session.get(PaystubPending, pending_id)
            parsed = parse_pdf(data_path(row.source_file, paystub_env))
        labels = {normalize(label): group for label, group in rule.labels.items()}
        limits = {normalize(label): limit for label, limit in rule.limits.items()}
        form = {"employer": rule.employer, "match_text": rule.match[0]}
        for key in ("pay_date", "period_start", "period_end"):
            value = getattr(parsed, key)
            form[key] = value.isoformat() if value else ""
        for i, line in enumerate(parsed.lines):
            form[f"label_{i}"] = line.label
            form[f"group_{i}"] = labels.get(normalize(line.label), "")
            form[f"limit_{i}"] = limits.get(normalize(line.label), "")
        return form

    return build


@pytest.fixture
def rule_a():
    rule = get_rule(Path(__file__).resolve().parents[2] / "paystubs" / "rules", "example")
    assert rule is not None
    return rule


@pytest.fixture
def rule_b():
    return Rule(
        "Contoso Test Corp",
        ["Contoso Test"],
        {
            "Regular": "ignore",
            "Gross Pay": "gross",
            "Net Pay": "net",
            "Federal Withholding": "tax",
            "State Tax": "tax",
            "OASDI": "tax",
            "Medicare": "tax",
            "401K Pre-Tax": "pretax",
            "Health": "benefit",
            "Roth 401K": "posttax",
            "ER HSA": "employer",
        },
        {"401K Pre-Tax": "401k", "Roth 401K": "401k", "ER HSA": "hsa"},
    )


@pytest.fixture
def parsed_a(tmp_path):
    return parse_pdf(stub_a(tmp_path / "a.pdf"))


@pytest.fixture
def parsed_b(tmp_path):
    return parse_pdf(stub_b(tmp_path / "b.pdf"))


@pytest.fixture
def stubs(parsed_a, parsed_b, rule_a, rule_b):
    result = []
    for parsed, rule in ((parsed_a, rule_a), (parsed_b, rule_b)):
        classified = classify(parsed, rule)
        assert classified.problems == []
        assert classified.reconciles
        result.append(
            Stub(
                rule.employer,
                parsed.pay_date,
                parsed.period_start,
                parsed.period_end,
                classified.gross,
                classified.net,
                classified.gross_ytd,
                classified.net_ytd,
                classified.lines,
                classified.reconcile_diff,
            )
        )
    return result
