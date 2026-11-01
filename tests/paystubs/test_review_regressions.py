"""Independent-review regressions, using synthetic, hand-calculated amounts only."""

import asyncio
import hashlib
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.routers.paycheck import money
from core.db import get_engine, make_engine
from core.models import Account, Paystub, PaystubJobState, PaystubLine, PaystubPending, Transaction
from paystubs.accounts import spending
from paystubs.analysis import Stub, facts, latest_stub, pay_frequency, segments, ytd_totals
from paystubs.config import Settings
from paystubs.gmail_fetch import FetchResult, fetch_once
from paystubs.limits import limits_for
from paystubs.parsers.pdf import ParsedStub, UnreadablePdf, parse_lines
from paystubs.rules import GENERIC_MATCHES, GROSS_YTD_FELL, Rule, classify, load_rules, save_rule
from paystubs.schedule import MonthlyJob
from paystubs.service import JOB_NAME, MailboxJob
from paystubs.store import data_path, ingest_file, known_sha
from paystubs.watcher import PaystubWatcher

from .make_stubs import stub_a, write_pdf
from .test_gmail_fetch import FakeImap, factory_for, message_bytes
from .test_watcher import wait_until


@pytest.fixture
def simple_rule():
    return Rule(
        "Regression Labs",
        ["Regression Labs"],
        {"Gross": "gross", "Tax": "tax", "Net": "net", "Match": "employer"},
    )


def simple_pdf(settings, name, day="10/30/2026", gross_ytd="1000.00", extra=()):
    return write_pdf(
        settings.inbox_dir / name,
        [
            "Regression Labs",
            f"Pay Date {day}",
            f"Gross 100.00 {gross_ytd}",
            "Tax 10.00 100.00",
            "Net 90.00 900.00",
            *extra,
        ],
    )


def view_stub(result, day=date(2026, 10, 30)):
    return Stub(
        "Regression Labs",
        day,
        None,
        None,
        result.gross,
        result.net,
        result.gross_ytd,
        result.net_ytd,
        result.lines,
        None,
    )


def test_new_employer_match_is_blank(web_client, paystub_env):
    queued = ingest_file(stub_a(paystub_env.inbox_dir / "new.pdf"), paystub_env)
    response = web_client.get(f"/paycheck/review/{queued.pending_id}")
    assert 'name="employer" value="Northwind Test Labs Inc"' in response.text
    assert 'name="match_text" value="" required' in response.text
    assert "Text printed only on this employer&#39;s stubs" in response.text or (
        "Text printed only on this employer's stubs" in response.text
    )


@pytest.mark.parametrize("match", ["", "abc", *sorted(GENERIC_MATCHES)])
def test_generic_or_short_matches_rejected(paystub_env, simple_rule, resolve, match):
    queued = ingest_file(simple_pdf(paystub_env, "new.pdf"), paystub_env)
    result = resolve(queued.pending_id, simple_rule, match_text=match.upper())
    assert result.status == "error"
    assert "at least 4" in result.reason or "generic" in result.reason
    assert not list(paystub_env.rules_dir.glob("*.yaml"))


def test_falling_ytd_review_and_existing_data_warning(
    web_client, paystub_env, simple_rule, mapping_form
):
    save_rule(paystub_env.rules_dir, simple_rule)
    first = ingest_file(simple_pdf(paystub_env, "first.pdf", "10/01/2026"), paystub_env)
    assert first.status == "imported"
    queued = ingest_file(simple_pdf(paystub_env, "lower.pdf", gross_ytd="500.00"), paystub_env)
    assert queued.status == "needs_review" and GROSS_YTD_FELL in queued.reason
    url = f"/paycheck/review/{queued.pending_id}"
    assert 'name="accept_mismatch"' in web_client.get(url).text
    form = mapping_form(queued.pending_id, simple_rule)
    assert web_client.post(url, data=form).status_code == 400
    response = web_client.post(url, data=form | {"accept_mismatch": "on"})
    assert response.status_code == 200
    assert "Gross YTD fell between consecutive stubs for Regression Labs" in response.text
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(Paystub)) == 2
        assert session.get(Paystub, first.paystub_id).gross_ytd == D("1000")


def test_refunds_ytd_offsets_and_employer_reversals(simple_rule):
    rule = replace(simple_rule, labels=simple_rule.labels | {"Offset": "benefit", "Refund": "tax"})
    parsed = ParsedStub(
        parse_lines(
            "Gross 100.00 1000.00\nTax 60.00 600.00\nRefund (50.00) (50.00)\n"
            "Offset 0.00 (120.00)\nMatch (50.00) 150.00\nNet 90.00 570.00"
        ),
        pay_date=date(2026, 10, 30),
    )
    # A tie among nonzero taxes keeps printed signs: 100 - 60 - (-50) = 90.
    result = classify(parsed, rule)
    assert result.reconciles and not result.problems
    lines = {line.label: line for line in result.lines}
    assert lines["Refund"].amount == D("-50")
    assert lines["Offset"].ytd == D("-120")
    assert lines["Match"].amount == D("-50")
    assert money(D("-50"), True, "tax") == "+$50.00"
    assert money(D("-50"), True, "employer") == "−$50.00"
    assert money(D("-50"), True) == "−$50.00"
    assert segments(view_stub(result))[0].share == D("0.1")


def test_negative_deduction_convention_and_fallback(simple_rule):
    parsed = ParsedStub(
        parse_lines("Gross 100.00\nTax (10.00) (100.00)\nNet 90.00\nMatch (50.00) 150.00"),
        pay_date=date(2026, 10, 30),
    )
    for group in ("tax", "pretax", "posttax", "benefit"):
        rule = replace(simple_rule, labels=simple_rule.labels | {"Tax": group})
        result = classify(parsed, rule)
        assert result.reconciles
        assert result.lines[0].amount == D("10") and result.lines[0].ytd == D("100")
        assert result.lines[1].amount == D("-50")
    assert parsed.lines[1].amount == D("-10")  # Classification must not mutate extraction.


@pytest.mark.parametrize(
    "value,current,total", [("1800.00", "0", "1800"), ("300.00", "300", "2100")]
)
def test_single_value_uses_previous_employer_ytd(paystub_env, simple_rule, value, current, total):
    save_rule(paystub_env.rules_dir, simple_rule)
    first = ingest_file(
        simple_pdf(paystub_env, "first.pdf", "10/01/2026", extra=["Match 100.00 1800.00"]),
        paystub_env,
    )
    second = ingest_file(
        simple_pdf(paystub_env, "second.pdf", gross_ytd="1100.00", extra=[f"Match {value}"]),
        paystub_env,
    )
    assert first.status == second.status == "imported"
    with Session(get_engine()) as session:
        line = session.scalar(
            select(PaystubLine).where(
                PaystubLine.paystub_id == second.paystub_id, PaystubLine.label == "Match"
            )
        )
        assert line.amount == D(current)
        assert line.ytd_amount == (D("1800") if current == "0" else None)
        stubs = []
        for row in session.scalars(select(Paystub).order_by(Paystub.pay_date)):
            from paystubs.rules import StubLine

            lines = [
                StubLine(item.label, item.group_, item.amount, item.ytd_amount)
                for item in session.scalars(
                    select(PaystubLine).where(PaystubLine.paystub_id == row.id)
                )
            ]
            stubs.append(
                Stub(
                    row.employer,
                    row.pay_date,
                    None,
                    None,
                    row.gross,
                    row.net,
                    row.gross_ytd,
                    row.net_ytd,
                    lines,
                    None,
                )
            )
        assert ytd_totals(stubs, 2026).totals["employer"] == D(total)


@pytest.mark.parametrize("group", ["tax", "pretax", "posttax", "benefit", "employer"])
def test_single_number_heuristic_covers_all_line_groups(simple_rule, group):
    rule = replace(simple_rule, labels=simple_rule.labels | {"Match": group})
    parsed = ParsedStub(
        parse_lines("Gross 100.00\nNet 100.00\nMatch 1800.00"), pay_date=date(2026, 10, 30)
    )
    result = classify(parsed, rule, {"match": D("1800")})
    assert result.lines[0].amount == D("0") and result.lines[0].ytd == D("1800")
    assert result.reconciles


@pytest.mark.parametrize(
    "first,last,known",
    [
        (date(2026, 10, 1), date(2026, 10, 30), False),
        (date(2026, 1, 31), date(2026, 9, 29), True),
        (date(2026, 1, 31), date(2026, 9, 28), False),
    ],
)
def test_spending_requires_coverage(paystub_env, first, last, known):
    with Session(get_engine()) as session:
        account = Account(name="Coverage", type="cash")
        hidden = Account(name="Hidden", type="cash", hidden=True)
        session.add_all([account, hidden])
        session.flush()
        session.add_all(
            [
                Transaction(account_id=account.id, date=first, amount=D("-100")),
                Transaction(account_id=account.id, date=last, amount=D("20")),
                Transaction(
                    account_id=account.id, date=date(2026, 1, 1), amount=D("-900"), pending=True
                ),
                Transaction(account_id=hidden.id, date=date(2026, 1, 1), amount=D("-900")),
            ]
        )
        session.flush()
        result = spending(session, date(2026, 1, 1), date(2026, 10, 30))
        assert result.value == (D("80") if known else None)
        assert result.late_accounts == (1 if first.month == 10 else 0)
        if not known:
            assert "Spending would be understated" in result.reason


def test_late_account_disclosure_and_coverage_reason(web_client, paystub_env, simple_rule):
    save_rule(paystub_env.rules_dir, simple_rule)
    ingest_file(simple_pdf(paystub_env, "pay.pdf"), paystub_env)
    with Session(get_engine()) as session:
        late = Account(name="Late", type="cash")
        session.add(late)
        session.flush()
        session.add_all(
            [
                Transaction(account_id=late.id, date=date(2026, 10, 1), amount=D("-100")),
                Transaction(account_id=late.id, date=date(2026, 10, 30), amount=D("-200")),
            ]
        )
        session.commit()
        text = web_client.get("/paycheck").text
        assert "Transactions only cover Oct 1, 2026 to Oct 30, 2026" in text
        early = Account(name="Early", type="cash")
        session.add(early)
        session.flush()
        session.add(Transaction(account_id=early.id, date=date(2026, 1, 2), amount=D("-50")))
        session.commit()
        result = spending(session, date(2026, 1, 1), date(2026, 10, 30))
        assert result.value == D("350") and result.late_accounts == 1
    assert "Spending may be understated: 1 account(s)" in web_client.get("/paycheck").text


def test_same_date_order_and_group_changes(stubs):
    from paystubs.rules import StubLine

    low = replace(
        stubs[0],
        gross_ytd=D("1000"),
        lines=[
            StubLine("HSA", "benefit", D("500"), D("500")),
        ],
    )
    high = replace(
        low,
        gross_ytd=D("2000"),
        lines=[
            StubLine(" hsa ", "pretax", D("500"), D("1000"), "hsa"),
        ],
    )
    for ordered in ([high, low], [low, high]):
        assert latest_stub(ordered) is high
        totals = ytd_totals(ordered, 2026)
        assert totals.totals["gross"] == D("2000")
        assert totals.totals["benefit"] == D("0")
        assert totals.totals["pretax"] == D("1000")
        rows = [row for row in totals.lines if row.label.strip() == "hsa"]
        assert len(rows) == 1 and rows[0].limit == "hsa"


@pytest.mark.parametrize(
    "extra,needle,gross,net,tax",
    [
        (["Tax 10.00 100.00"], "appears twice with identical amounts", "100", "90", "10"),
        (
            ["Gross 200.00 2000.00", "Tax 20.00 200.00", "Net 180.00 1800.00"],
            "This PDF seems to hold more than one statement",
            "300",
            "270",
            "30",
        ),
    ],
)
def test_repeats_and_multiple_statements_can_be_accepted(
    paystub_env, simple_rule, resolve, extra, needle, gross, net, tax
):
    save_rule(paystub_env.rules_dir, simple_rule)
    queued = ingest_file(simple_pdf(paystub_env, "repeat.pdf", extra=extra), paystub_env)
    assert queued.status == "needs_review" and needle in queued.reason
    assert resolve(queued.pending_id, simple_rule).status == "error"
    accepted = resolve(queued.pending_id, simple_rule, accept_mismatch=True)
    assert accepted.status == "imported"
    with Session(get_engine()) as session:
        row = session.get(Paystub, accepted.paystub_id)
        assert row.gross == D(gross) and row.net == D(net)
        assert session.scalar(
            select(PaystubLine.amount).where(
                PaystubLine.paystub_id == row.id, PaystubLine.label == "Tax"
            )
        ) == D(tax)


def test_identical_summary_and_ignored_repeats_are_safe(simple_rule):
    parsed = ParsedStub(
        parse_lines(
            "Gross 100.00\nGross 100.00\nNet 90.00\nNet 90.00\nTax 10.00\nHours 80.00\nHours 80.00"
        ),
        pay_date=date(2026, 10, 30),
    )
    result = classify(parsed, replace(simple_rule, labels=simple_rule.labels | {"Hours": "ignore"}))
    assert not result.problems and result.reconciles
    assert result.gross == D("100") and result.net == D("90")


@pytest.mark.parametrize(
    "label",
    [
        "FITW",
        "FWT",
        "FIT",
        "Fed",
        "Federal",
        "FICA",
        "OASDI",
        "Soc Sec",
        "Social Security",
        "SS Tax",
        "SS EE",
        "Medicare",
        "Med EE",
        "EE Medicare",
        "Med Tax",
    ],
)
def test_federal_tax_aliases_have_word_boundaries(simple_rule, label):
    parsed = ParsedStub(
        parse_lines(
            f"Gross 100.00 1000.00\n{label} 10.00 100.00\nMassachusetts 2.00 20.00\n"
            "Med Premium 3.00 30.00\nNet 85.00 850.00"
        ),
        pay_date=date(2026, 10, 30),
    )
    rule = replace(
        simple_rule,
        labels={
            "Gross": "gross",
            "Net": "net",
            label: "tax",
            "Massachusetts": "tax",
            "Med Premium": "tax",
        },
    )
    result = facts([view_stub(classify(parsed, rule))], 2026, limits_for(2026), "self")
    assert result.federal_fica_rate == D("0.10")
    assert result.state_city_rate == D("0.05")


def test_absent_tax_and_limit_lines_stay_missing(web_client, paystub_env, simple_rule):
    path = write_pdf(
        paystub_env.inbox_dir / "bare.pdf",
        [
            "Regression Labs",
            "Pay Date 10/30/2026",
            "Gross 100.00 1000.00",
            "Net 100.00 1000.00",
        ],
    )
    save_rule(paystub_env.rules_dir, simple_rule)
    assert ingest_file(path, paystub_env).status == "imported"
    parsed = ParsedStub(
        parse_lines("Gross 100.00 1000.00\nNet 100.00 1000.00"), pay_date=date(2026, 10, 30)
    )
    result = facts([view_stub(classify(parsed, simple_rule))], 2026, limits_for(2026), "self")
    assert result.federal_fica_rate is result.state_city_rate is None
    assert result.hsa.ytd is result.retirement_401k.ytd is None
    text = web_client.get("/paycheck").text
    assert text.count("No line tagged") == 2
    assert "$0 <small>of $4,400" not in text


def test_all_accepted_differences_in_selected_year(web_client, paystub_env, simple_rule, resolve):
    save_rule(paystub_env.rules_dir, simple_rule)
    for day, net in (("01/15/2026", "80.00"), ("02/15/2026", "70.00"), ("01/15/2025", "60.00")):
        path = write_pdf(
            paystub_env.inbox_dir / f"bad-{net}.pdf",
            [
                "Regression Labs",
                f"Pay Date {day}",
                "Gross 100.00",
                "Tax 10.00",
                f"Net {net}",
            ],
        )
        queued = ingest_file(path, paystub_env)
        assert resolve(queued.pending_id, simple_rule, accept_mismatch=True).status == "imported"
    assert ingest_file(simple_pdf(paystub_env, "latest.pdf"), paystub_env).status == "imported"
    text = web_client.get("/paycheck?year=2026").text
    assert "Regression Labs · 2026-01-15" in text and "off by $10.00" in text
    assert "Regression Labs · 2026-02-15" in text and "off by $20.00" in text
    assert "off by $30.00" not in text


def test_rules_default_and_documentation_excluded(tmp_path, simple_rule):
    assert Settings(data_dir=tmp_path).rules_dir == tmp_path / "paystubs" / "rules"
    assert Settings.from_env({"ORBIT_DATA_DIR": str(tmp_path)}).rules_dir == (
        tmp_path / "paystubs" / "rules"
    )
    save_rule(tmp_path, simple_rule, slug="example")
    assert load_rules(tmp_path) == []


def test_rule_save_oserror_keeps_review(
    web_client, paystub_env, simple_rule, mapping_form, monkeypatch
):
    queued = ingest_file(simple_pdf(paystub_env, "pending.pdf"), paystub_env)
    form = mapping_form(queued.pending_id, simple_rule)

    def fail(*args, **kwargs):
        raise OSError("private folder details")

    monkeypatch.setattr("paystubs.store.save_rule", fail)
    response = web_client.post(f"/paycheck/review/{queued.pending_id}", data=form)
    assert response.status_code == 400 and "rules folder is writable" in response.text
    assert "private folder" not in response.text
    with Session(get_engine()) as session:
        assert session.get(PaystubPending, queued.pending_id) is not None
    save_rule(paystub_env.rules_dir, simple_rule)
    monkeypatch.setattr("app.routers.paycheck.save_rule", fail)
    form = {
        f"{key}_{i}": value
        for i, (label, group) in enumerate(simple_rule.labels.items())
        for key, value in (("label", label.casefold()), ("group", group))
    }
    response = web_client.post("/paycheck/rules/regression-labs", data=form)
    assert response.status_code == 400 and "rules folder is writable" in response.text


def test_failed_hash_retries_same_row(paystub_env, simple_rule, monkeypatch):
    import paystubs.store as store

    original = store.parse_pdf
    path = simple_pdf(paystub_env, "retry.pdf")
    payload = path.read_bytes()

    def fail(_):
        raise UnreadablePdf("Temporary read failure")

    monkeypatch.setattr(store, "parse_pdf", fail)
    failed = ingest_file(path, paystub_env)
    assert failed.status == "failed"
    monkeypatch.setattr(store, "parse_pdf", original)
    path.write_bytes(payload)
    retried = ingest_file(path, paystub_env)
    assert retried.status == "needs_mapping" and retried.pending_id == failed.pending_id
    assert known_sha(hashlib.sha256(payload).hexdigest())


def test_pending_hash_has_distinct_message(web_client, paystub_env):
    path = simple_pdf(paystub_env, "pending.pdf")
    payload = path.read_bytes()
    queued = ingest_file(path, paystub_env)
    response = web_client.post("/paycheck/upload", files={"file": ("copy.pdf", payload)})
    assert "This file is already waiting for review" in response.text
    assert "Already imported" not in response.text
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(PaystubPending)) == 1
        assert data_path(
            session.get(PaystubPending, queued.pending_id).source_file, paystub_env
        ).exists()


def test_upload_failure_retains_file(web_client, paystub_env, monkeypatch):
    def fail(*args):
        raise OSError("private details")

    monkeypatch.setattr("app.routers.paycheck.ingest_file", fail)
    response = web_client.post("/paycheck/upload", files={"file": ("retry.pdf", b"%PDF-synthetic")})
    assert response.status_code == 503 and "retry it on the next folder sweep" in response.text
    files = list((paystub_env.data_dir / "paystubs" / "uploads").iterdir())
    assert len(files) == 1 and files[0].read_bytes() == b"%PDF-synthetic"


def test_timer_sweeps_missed_events_and_retries_uploads(tmp_path, monkeypatch):
    inbox, uploads = tmp_path / "inbox", tmp_path / "uploads"
    attempts = {}

    class NoEvents:
        def schedule(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def join(self, timeout):
            assert timeout == 5

    def process(path):
        attempts[path.name] = attempts.get(path.name, 0) + 1
        if attempts[path.name] == 1:
            raise OSError("Retry later")
        path.rename(path.with_suffix(".done"))

    monkeypatch.setattr("paystubs.watcher.Observer", NoEvents)
    watcher = PaystubWatcher(inbox, process, sweep_interval=0.05, extra_dirs=[uploads])
    monkeypatch.setattr(watcher, "_ready", lambda path: path.is_file())
    with watcher:
        (inbox / "missed.pdf").write_bytes(b"%PDF-inbox")
        (uploads / "stranded.pdf").write_bytes(b"%PDF-upload")
        wait_until(
            lambda: (inbox / "missed.done").exists() and (uploads / "stranded.done").exists()
        )
    assert attempts == {"missed.pdf": 2, "stranded.pdf": 2}


def test_stop_joins_are_bounded(tmp_path):
    calls = []

    class Worker:
        def stop(self):
            pass

        def join(self, timeout):
            calls.append(timeout)

    watcher = PaystubWatcher(tmp_path, lambda path: None)
    watcher._observer = Worker()
    watcher._worker = Worker()
    monthly = MonthlyJob(lambda: False, lambda: None)
    monthly._thread = Worker()
    monthly.stop()
    watcher.stop()
    assert calls == [5, 5, 5]


def test_mailbox_gap_uses_last_success_with_overlap(paystub_env, monkeypatch):
    with Session(get_engine()) as session:
        session.add(PaystubJobState(name=JOB_NAME, last_success_month="2026-02"))
        session.commit()
    calls = []

    def fetch(settings, seen, **kwargs):
        calls.append(kwargs)
        return FetchResult()

    monkeypatch.setattr("paystubs.service.fetch_once", fetch)
    MailboxJob(paystub_env).run(date(2026, 10, 31))
    assert calls == [{"today": date(2026, 10, 31), "since": date(2026, 1, 25)}]
    MailboxJob(paystub_env).run(date(2026, 11, 1))
    assert calls[-1]["since"] == date(2026, 9, 17)  # 45 days is earlier than Sep 24.


def test_fetch_explicit_since_and_timeout(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        imap_host="imap.test.invalid",
        imap_user="fake",
        imap_password="fake",
        imap_sender=("payroll@test.invalid",),
    )
    client = FakeImap(message_bytes([]))
    result = fetch_once(
        settings, lambda _: False, date(2026, 10, 31), factory_for(client), since=date(2026, 1, 25)
    )
    assert not result.errors
    assert next(call for call in client.calls if call[0] == "search")[2].startswith(
        "SINCE 25-Jan-2026"
    )


def test_mailbox_status_on_empty_page(web_client, monkeypatch):
    for key, value in {
        "HOST": "test.invalid",
        "USER": "fake",
        "PASSWORD": "fake",
        "SENDER": "pay@test.invalid",
    }.items():
        monkeypatch.setenv("PAYSTUB_IMAP_" + key, value)
    with Session(get_engine()) as session:
        session.add(
            PaystubJobState(
                name=JOB_NAME,
                last_success_month="2026-08",
                last_saved_count=2,
                last_error="IMAP login failed",
            )
        )
        session.commit()
    text = web_client.get("/paycheck").text
    assert "last successful month 2026-08; 2 files saved." in text
    assert "IMAP login failed" in text


def test_blank_settings_and_invalid_coverage(web_client, monkeypatch, caplog):
    values = {
        "PAYSTUB_IMAP_PORT": " ",
        "PAYSTUB_IMAP_LOOKBACK_DAYS": "",
        "PAYSTUB_IMAP_DAY_OF_MONTH": "",
        "PAYSTUB_HSA_COVERAGE": "invalid",
    }
    settings = Settings.from_env(values)
    assert (settings.imap_port, settings.imap_lookback_days, settings.imap_day_of_month) == (
        993,
        45,
        1,
    )
    assert settings.hsa_coverage == "self"
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert web_client.get("/paycheck").status_code == 200
    assert "PAYSTUB_HSA_COVERAGE" in caplog.text


def test_foreign_host_rejected_and_parameters_hidden(web_client):
    assert web_client.get("/health", headers={"Host": "evil.invalid"}).status_code == 400
    assert web_client.get("/health", headers={"Host": "localhost"}).status_code == 200
    engine = make_engine("sqlite:///:memory:")
    try:
        assert engine.hide_parameters is True
    finally:
        engine.dispose()


def test_routes_do_blocking_work_in_worker_threads(
    web_client, paystub_env, simple_rule, mapping_form, monkeypatch
):
    import app.routers.paycheck as router

    calls = []

    def wrap(name):
        original = getattr(router, name)

        def check(*args):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            calls.append(name)
            return original(*args)

        monkeypatch.setattr(router, name, check)

    for name in ("_upload", "_save_review", "_update_rule"):
        wrap(name)
    payload = simple_pdf(paystub_env, "source.pdf").read_bytes()
    response = web_client.post("/paycheck/upload", files={"file": ("new.pdf", payload)})
    pending_id = int(response.url.path.rsplit("/", 1)[-1])
    form = mapping_form(pending_id, simple_rule)
    assert web_client.post(f"/paycheck/review/{pending_id}", data=form).status_code == 200
    form = {
        f"{key}_{i}": value
        for i, (label, group) in enumerate(simple_rule.labels.items())
        for key, value in (("label", label.casefold()), ("group", group))
    }
    assert web_client.post("/paycheck/rules/regression-labs", data=form).status_code == 200
    assert calls == ["_upload", "_save_review", "_update_rule"]


def test_natural_duplicates_include_ytd_and_latest_source(web_client, paystub_env, simple_rule):
    save_rule(paystub_env.rules_dir, simple_rule)
    higher = ingest_file(simple_pdf(paystub_env, "higher.pdf", gross_ytd="2000.00"), paystub_env)
    lower = ingest_file(simple_pdf(paystub_env, "lower.pdf"), paystub_env)
    assert higher.status == lower.status == "imported"
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(Paystub)) == 2
    text = web_client.get("/paycheck").text
    assert "higher.pdf" in text and "lower.pdf" not in text


@pytest.mark.parametrize(
    "start,days,expected",
    [
        (date(2026, 1, 1), 13, "Semi-monthly"),
        (date(2026, 1, 16), 14, "Semi-monthly"),
        (date(2026, 1, 2), 14, "Biweekly"),
    ],
)
def test_semimonthly_before_biweekly(stubs, start, days, expected):
    from datetime import timedelta

    assert (
        pay_frequency(
            replace(stubs[0], period_start=start, period_end=start + timedelta(days=days - 1))
        )
        == expected
    )


def test_stale_labels_have_specific_message(web_client, paystub_env, simple_rule, mapping_form):
    queued = ingest_file(simple_pdf(paystub_env, "stale.pdf"), paystub_env)
    form = mapping_form(queued.pending_id, simple_rule) | {"label_0": "Old label"}
    response = web_client.post(f"/paycheck/review/{queued.pending_id}", data=form)
    assert response.status_code == 400
    assert "The labels changed. Reload the review and try again." in response.text


def test_signed_display_and_bar_widths(web_client, paystub_env, simple_rule):
    rule = replace(simple_rule, labels=simple_rule.labels | {"Refund": "benefit"})
    save_rule(paystub_env.rules_dir, rule)
    path = write_pdf(
        paystub_env.inbox_dir / "refund.pdf",
        [
            "Regression Labs",
            "Pay Date 10/30/2026",
            "Gross 100.00 1000.00",
            "Tax 10.00 100.00",
            "Refund (50.00) (50.00)",
            "Match (50.00) (50.00)",
            "Net 140.00 950.00",
        ],
    )
    assert ingest_file(path, paystub_env).status == "imported"
    text = web_client.get("/paycheck").text
    assert "+$50.00" in text and "−$50.00" in text and "−$-" not in text
    assert "-50.0%" in text
    assert "width:0%;opacity:.3" in text


def test_employer_line_printed_in_parentheses_is_not_a_reversal():
    rule = Rule(
        "Synthetic",
        ["Synthetic"],
        {"Gross Pay": "gross", "Net Pay": "net", "Fed": "tax", "ER Match": "employer"},
    )
    parsed = ParsedStub(
        lines=parse_lines(
            "Gross Pay 1,000.00 12,000.00\n"
            "Fed (200.00) (2,400.00)\n"
            "ER Match (50.00) (600.00)\n"
            "Net Pay 800.00 9,600.00"
        ),
        pay_date=date(2026, 6, 12),
    )
    match = classify(parsed, rule).lines[-1]
    assert (match.amount, match.ytd) == (D("50.00"), D("600.00"))
    # A real reversal keeps its sign, because the running total stays positive.
    parsed.lines = parse_lines(
        "Gross Pay 1,000.00 12,000.00\nFed 200.00 2,400.00\n"
        "ER Match (50.00) 550.00\nNet Pay 800.00 9,600.00"
    )
    match = classify(parsed, rule).lines[-1]
    assert (match.amount, match.ytd) == (D("-50.00"), D("550.00"))
