import json
import re
from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.db import get_engine
from core.models import Account, BalanceSnapshot, Paystub, PaystubPending, Transaction
from paystubs.rules import get_rule, save_rule, slugify
from paystubs.store import data_path, ingest_file

from .make_stubs import stub_a, stub_b, write_pdf
from .test_accounts import seed_accounts


def chart_data(response):
    match = re.search(
        r'<script type="application/json" id="paycheck-data">(.*?)</script>', response.text
    )
    assert match
    return json.loads(match[1])


def import_examples(settings, rule_a, rule_b):
    save_rule(settings.rules_dir, rule_a)
    save_rule(settings.rules_dir, rule_b)
    assert ingest_file(stub_a(settings.inbox_dir / "a.pdf"), settings).status == "imported"
    assert ingest_file(stub_b(settings.inbox_dir / "b.pdf"), settings).status == "imported"


def test_health_dashboard_and_empty_paycheck(web_client):
    assert web_client.get("/health").status_code == 200
    assert web_client.get("/dashboard").status_code == 200
    response = web_client.get("/paycheck")
    assert response.status_code == 200
    assert "No paystubs" in response.text and "data/inbox/paystubs/" in response.text
    assert "Import paystub" in response.text
    assert 'id="wf"' not in response.text


def test_upload_mapping_post_persists_and_reimports(web_client, paystub_env, rule_a, mapping_form):
    payload = stub_a(paystub_env.data_dir / "fixture.pdf").read_bytes()
    response = web_client.post(
        "/paycheck/upload", files={"file": ("../../..\\odd file?.pdf", payload, "application/pdf")}
    )
    assert response.status_code == 200 and "/paycheck/review/" in str(response.url)
    pending_id = int(response.url.path.rsplit("/", 1)[-1])
    assert 'value="Northwind Test Labs Inc"' in response.text
    assert 'value="2026-10-30"' in response.text
    assert 'name="label_0"' in response.text
    response = web_client.post(
        f"/paycheck/review/{pending_id}", data=mapping_form(pending_id, rule_a)
    )
    assert response.status_code == 200 and "Paystub imported." in response.text
    assert (paystub_env.rules_dir / f"{slugify(rule_a.employer)}.yaml").is_file()
    with Session(get_engine()) as session:
        row = session.scalar(select(Paystub))
        path = data_path(row.source_file, paystub_env)
        assert path.parent == paystub_env.archive_dir / "2026"
        assert re.fullmatch(r"[a-f0-9]{12}_[A-Za-z0-9._-]+", path.name)
        assert path.read_bytes() == payload
    response = web_client.post("/paycheck/upload", files={"file": ("again.pdf", payload)})
    assert "Already imported" in response.text
    assert len(list(paystub_env.duplicates_dir.iterdir())) == 1
    assert web_client.get(f"/paycheck/review/{pending_id}").status_code == 404


def test_requested_facts_and_waterfall(web_client, paystub_env, rule_a, rule_b):
    import_examples(paystub_env, rule_a, rule_b)
    with Session(get_engine()) as session:
        seed_accounts(session)
    response = web_client.get("/paycheck")
    assert response.status_code == 200
    for text in ("Oct 30, 2026", "29.4%", "over by $2,340", "$3,900", "of $4,400"):
        assert text in response.text
    assert "Employer</th>" in response.text
    assert "$2,400 of post-tax deductions stays in the flow" in response.text
    data = chart_data(response)
    assert len(data) == 9
    assert [D(step["v"]) for step in data] == list(
        map(
            D, ["256340", "-75272", "-27740", "-4400", "-60000", "27740", "8940", "14392", "140000"]
        )
    )
    assert sum(D(step["v"]) for step in data[:-1]) == D("140000")
    assert not any(step["missing"] for step in data)


@pytest.mark.parametrize(
    "mode,missing",
    [("none", {4, 7, 8}), ("spending", {7, 8}), ("networth", {4, 7}), ("no-baseline", {4, 7, 8})],
)
def test_missing_inputs_and_reasons(web_client, paystub_env, rule_a, rule_b, mode, missing):
    import_examples(paystub_env, rule_a, rule_b)
    with Session(get_engine()) as session:
        account = Account(name="Synthetic account", type="cash")
        if mode != "none":
            session.add(account)
            session.flush()
        if mode == "spending":
            session.add(
                Transaction(account_id=account.id, date=date(2026, 1, 2), amount=D("-60000"))
            )
            session.add(
                Transaction(
                    account_id=account.id,
                    date=date(2026, 10, 30),
                    amount=D("100"),
                    category="Income",
                )
            )
        if mode in {"networth", "no-baseline"}:
            session.add(
                BalanceSnapshot(
                    account_id=account.id,
                    date=date(2026, 10, 30),
                    balance=D("140000"),
                    source="test",
                )
            )
        if mode == "networth":
            session.add(
                BalanceSnapshot(
                    account_id=account.id, date=date(2025, 12, 31), balance=D("0"), source="test"
                )
            )
        session.commit()
    response = web_client.get("/paycheck")
    data = chart_data(response)
    assert {i for i, step in enumerate(data) if step["missing"]} == missing
    if 4 in missing:
        assert "Spending: needs transactions" in response.text
    if 8 in missing:
        assert "Needs a balance from on or before Dec 31, 2025" in response.text
    assert "needs every pay, spending and net-worth input" in response.text
    if mode == "no-baseline":
        assert "1 account first seen after Dec 31, 2025" in response.text
        assert "counted from $0" in response.text


def test_new_account_disclosure_with_known_baseline(web_client, paystub_env, rule_a, rule_b):
    import_examples(paystub_env, rule_a, rule_b)
    with Session(get_engine()) as session:
        seed_accounts(session)
        new = Account(name="New synthetic account", type="cash")
        session.add(new)
        session.flush()
        session.add(
            BalanceSnapshot(
                account_id=new.id, date=date(2026, 5, 1), balance=D("1000"), source="test"
            )
        )
        session.commit()
    response = web_client.get("/paycheck")
    assert "counted from $0" in response.text
    assert D(chart_data(response)[-1]["v"]) == D("141000")


def test_review_mismatch_and_match_text_rejections(web_client, paystub_env, rule_a, mapping_form):
    pending = ingest_file(
        stub_a(paystub_env.inbox_dir / "bad.pdf", overrides={"Net Pay": ("5900.00", "117800.00")}),
        paystub_env,
    )
    form = mapping_form(pending.pending_id, rule_a)
    url = f"/paycheck/review/{pending.pending_id}"
    wrong_match = web_client.post(url, data=form | {"match_text": "NOT ON THE STUB"})
    assert wrong_match.status_code == 400 and "future stubs wouldn" in wrong_match.text
    response = web_client.post(url, data=form)
    assert response.status_code == 400 and "off by $10.00" in response.text
    assert 'name="accept_mismatch"' in response.text
    assert not list(paystub_env.rules_dir.glob("*.yaml"))
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(Paystub)) == 0
    response = web_client.post(url, data=form | {"accept_mismatch": "on"})
    assert response.status_code == 200
    assert "accepted though its lines are off by $10.00" in response.text


def test_upload_rejects_bad_and_oversize_pdf(web_client, monkeypatch):
    response = web_client.post("/paycheck/upload", files={"file": ("a.pdf", b"not pdf")})
    assert response.status_code == 400 and "Choose a PDF file" in response.text
    # Exercise the boundary without constructing a 20 MB request in every test run.
    monkeypatch.setattr("app.routers.paycheck.MAX_UPLOAD", 20)
    response = web_client.post("/paycheck/upload", files={"file": ("a.pdf", b"%PDF-" + b"x" * 16)})
    assert response.status_code == 400 and "no larger than 20 MB" in response.text


@pytest.mark.parametrize(
    "url",
    [
        "/paycheck/upload",
        "/paycheck/review/1",
        "/paycheck/review/1/dismiss",
        "/paycheck/rules/fake",
    ],
)
@pytest.mark.parametrize(
    "headers", [{"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.invalid"}]
)
def test_every_post_rejects_cross_site(web_client, url, headers):
    response = web_client.post(url, headers=headers, files={"file": ("a.pdf", b"%PDF-test")})
    assert response.status_code == 403


def test_same_origin_upload_allowed(web_client, paystub_env):
    payload = stub_a(paystub_env.data_dir / "source.pdf").read_bytes()
    response = web_client.post(
        "/paycheck/upload",
        headers={"Origin": "http://testserver"},
        files={"file": ("a.pdf", payload)},
    )
    assert response.status_code == 200 and "Review paystub" in response.text


def test_untrusted_labels_escaped_on_both_pages(web_client, paystub_env, rule_a, mapping_form):
    label = "<script>alert(1)</script>"
    pending = ingest_file(
        stub_a(paystub_env.inbox_dir / "hostile.pdf", extra=[f"{label} 0.00 0.00"]), paystub_env
    )
    url = f"/paycheck/review/{pending.pending_id}"
    response = web_client.get(url)
    assert label not in response.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    rule_a.labels[label] = "posttax"
    response = web_client.post(url, data=mapping_form(pending.pending_id, rule_a))
    assert response.status_code == 200
    assert label not in response.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert len(chart_data(response)) == 9


@pytest.mark.parametrize("password", [None, "fake-password"])
def test_failed_files_show_reason_and_dismiss(web_client, paystub_env, password):
    path = write_pdf(paystub_env.inbox_dir / "failed.pdf", [], password=password)
    queued = ingest_file(path, paystub_env)
    response = web_client.get("/paycheck")
    assert queued.reason in response.text
    assert f'href="/paycheck/review/{queued.pending_id}"' not in response.text
    response = web_client.post(f"/paycheck/review/{queued.pending_id}/dismiss")
    assert "Dismissed. The file is kept" in response.text
    with Session(get_engine()) as session:
        row = session.get(PaystubPending, queued.pending_id)
        assert row.status == "dismissed"
        assert data_path(row.source_file, paystub_env).is_relative_to(paystub_env.data_dir)
        assert data_path(row.source_file, paystub_env).is_file()


def test_rule_editor_can_clear_limit_without_rewriting_imports(web_client, paystub_env, rule_a):
    save_rule(paystub_env.rules_dir, rule_a)
    ingest_file(stub_a(paystub_env.inbox_dir / "a.pdf"), paystub_env)
    slug = slugify(rule_a.employer)
    rule = get_rule(paystub_env.rules_dir, slug)
    assert rule.employer in web_client.get("/paycheck/rules").text
    response = web_client.get(f"/paycheck/rules/{slug}")
    assert "Stubs already imported are not changed" in response.text
    form = {}
    for i, (label, group) in enumerate(rule.labels.items()):
        form[f"label_{i}"] = label
        form[f"group_{i}"] = group
        form[f"limit_{i}"] = ""
    response = web_client.post(f"/paycheck/rules/{slug}", data=form)
    assert response.status_code == 200
    assert get_rule(paystub_env.rules_dir, slug).limits == {}
    assert "of $24,500" in response.text and "$20,840" in response.text


def test_latest_year_default_year_links_and_unknown_limits(web_client, paystub_env, rule_a):
    save_rule(paystub_env.rules_dir, rule_a)
    ingest_file(stub_a(paystub_env.inbox_dir / "old.pdf"), paystub_env)
    ingest_file(
        stub_a(
            paystub_env.inbox_dir / "next.pdf",
            pay_date="01/15/2027",
            period="01/01/2027 - 01/15/2027",
        ),
        paystub_env,
    )
    response = web_client.get("/paycheck")
    assert "Jan 15, 2027" in response.text
    assert "no IRS limit on file for 2027" in response.text
    assert 'href="/paycheck?year=2026"' in response.text
    assert "Oct 30, 2026" in web_client.get("/paycheck?year=2026").text
    assert "No paystubs for 2025" in web_client.get("/paycheck?year=2025").text
    assert web_client.get("/paycheck/review/9999").status_code == 404
