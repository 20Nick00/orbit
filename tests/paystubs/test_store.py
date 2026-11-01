import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from time import monotonic, sleep

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db import get_engine
from core.models import Paystub, PaystubLine, PaystubPending
from paystubs.rules import save_rule, slugify
from paystubs.store import data_path, dismiss_pending, ingest_file, known_sha, retry_pending
from paystubs.watcher import PaystubWatcher

from .make_stubs import stub_a, write_pdf


def test_mapping_storage_duplicates_and_next_stub(paystub_env, rule_a, resolve):
    settings = paystub_env
    path = stub_a(settings.inbox_dir / "a.pdf")
    payload = path.read_bytes()
    outcome = ingest_file(path, settings)
    assert outcome.status == "needs_mapping"
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(Paystub)) == 0
        pending = session.get(PaystubPending, outcome.pending_id)
        assert data_path(pending.source_file, settings).parent == settings.pending_dir
        assert pending.reason == "New employer"
        assert pending.suggested_employer == "Northwind Test Labs Inc"
        assert "text" not in PaystubPending.__table__.columns
    imported = resolve(outcome.pending_id, rule_a)
    assert imported.status == "imported"
    assert (settings.rules_dir / f"{slugify(rule_a.employer)}.yaml").is_file()
    with Session(get_engine()) as session:
        row = session.get(Paystub, imported.paystub_id)
        assert row.gross == D("10417") and row.net == D("5890")
        assert row.gross_ytd == D("208340") and row.net_ytd == D("117800")
        assert row.reconcile_diff is None
        archive = data_path(row.source_file, settings)
        assert archive.parent == settings.archive_dir / "2026"
        assert archive.read_bytes() == payload
        lines = list(session.scalars(select(PaystubLine)))
        assert len(lines) == 10
        retirement = next(line for line in lines if line.label == "401(k) Pre-tax")
        assert retirement.limit_bucket == "401k" and retirement.ytd_amount == D("20840")
        assert next(line for line in lines if line.label == "HSA").limit_bucket == "hsa"
        assert session.get(PaystubPending, outcome.pending_id) is None
    assert known_sha(hashlib.sha256(payload).hexdigest())
    duplicate = settings.inbox_dir / "copy.pdf"
    duplicate.write_bytes(payload)
    assert ingest_file(duplicate, settings).status == "duplicate"
    natural = stub_a(settings.inbox_dir / "rendered.pdf", extra=["Different rendering"])
    assert hashlib.sha256(natural.read_bytes()).digest() != hashlib.sha256(payload).digest()
    assert ingest_file(natural, settings).status == "duplicate"
    assert len(list(settings.duplicates_dir.glob("*"))) == 2
    later = stub_a(settings.inbox_dir / "later.pdf", pay_date="11/15/2026")
    assert ingest_file(later, settings).status == "imported"
    assert ingest_file(later, settings).status == "noop"


def test_database_constraints_and_integer_cents(paystub_env, rule_a):
    save_rule(paystub_env.rules_dir, rule_a)
    outcome = ingest_file(stub_a(paystub_env.inbox_dir / "a.pdf"), paystub_env)
    with get_engine().connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT gross_ytd, net_ytd, typeof(gross_ytd) FROM paystubs"
        ).one() == (20834000, 11780000, "integer")
    with Session(get_engine()) as session:
        session.add(
            PaystubLine(
                paystub_id=outcome.paystub_id, label="Bad", group_="tax", limit_bucket="bad"
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
        session.add(
            PaystubPending(file_sha256="fake", source_file="test.pdf", status="bad", reason="test")
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
        existing = session.get(Paystub, outcome.paystub_id)
        session.add(
            Paystub(
                employer="Duplicate", pay_date=date(2026, 1, 1), file_sha256=existing.file_sha256
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_unknown_label_and_new_rule_retries_other_pending(paystub_env, rule_a, resolve):
    first = ingest_file(stub_a(paystub_env.inbox_dir / "a.pdf"), paystub_env)
    second = ingest_file(
        stub_a(paystub_env.inbox_dir / "later.pdf", pay_date="11/15/2026"), paystub_env
    )
    assert second.status == "needs_mapping"
    assert resolve(first.pending_id, rule_a).status == "imported"
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(Paystub)) == 2
        assert session.scalar(select(func.count()).select_from(PaystubPending)) == 0
    odd = ingest_file(
        stub_a(paystub_env.inbox_dir / "unknown.pdf", extra=["Mystery 5.00 5.00"]), paystub_env
    )
    assert odd.status == "needs_review" and "Unknown label: Mystery" in odd.reason
    # An old browser review URL must never address a different, newly queued file.
    assert odd.pending_id > second.pending_id


def test_mismatch_rejects_without_writing_rule_then_accepts(paystub_env, rule_a, resolve):
    path = stub_a(
        paystub_env.inbox_dir / "bad.pdf", overrides={"Net Pay": ("5,900.00", "117,800.00")}
    )
    queued = ingest_file(path, paystub_env)
    rejected = resolve(queued.pending_id, rule_a)
    assert rejected.status == "error" and "off by $10.00" in rejected.reason
    assert not list(paystub_env.rules_dir.glob("*.yaml"))
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(Paystub)) == 0
    accepted = resolve(queued.pending_id, rule_a, accept_mismatch=True)
    assert accepted.status == "imported"
    with Session(get_engine()) as session:
        assert session.get(Paystub, accepted.paystub_id).reconcile_diff == D("-10")


def test_match_and_required_labels_dates_validated(paystub_env, rule_a, resolve):
    pending = ingest_file(stub_a(paystub_env.inbox_dir / "a.pdf"), paystub_env)
    result = resolve(pending.pending_id, rule_a, match_text="Not on the PDF")
    assert result.status == "error"
    assert "future stubs wouldn't be recognised" in result.reason
    assert resolve(pending.pending_id, rule_a, labels={}).status == "error"
    assert resolve(pending.pending_id, rule_a, employer="").status == "error"
    result = resolve(
        pending.pending_id, rule_a, period_start=date(2026, 11, 1), period_end=date(2026, 10, 1)
    )
    assert result.status == "error"
    assert not list(paystub_env.rules_dir.glob("*.yaml"))
    accepted = resolve(pending.pending_id, rule_a, pay_date=date(2026, 10, 29))
    with Session(get_engine()) as session:
        assert session.get(Paystub, accepted.paystub_id).pay_date == date(2026, 10, 29)


@pytest.mark.parametrize("encrypted", [True, False])
def test_unreadable_and_dismiss_never_lose_file(paystub_env, encrypted):
    path = write_pdf(
        paystub_env.inbox_dir / "unreadable.pdf",
        ["Synthetic private text"] if encrypted else [],
        password="fake-password" if encrypted else None,
    )
    payload = path.read_bytes()
    outcome = ingest_file(path, paystub_env)
    assert outcome.status == "failed"
    assert ("password-protected" if encrypted else "No extractable text") in outcome.reason
    with Session(get_engine()) as session:
        row = session.get(PaystubPending, outcome.pending_id)
        before = data_path(row.source_file, paystub_env)
        assert before.parent == paystub_env.failed_dir
    dismiss_pending(outcome.pending_id, paystub_env)
    with Session(get_engine()) as session:
        row = session.get(PaystubPending, outcome.pending_id)
        assert row.status == "dismissed"
        assert data_path(row.source_file, paystub_env) == before
        assert before.read_bytes() == payload


def test_dismiss_mapping_and_same_name_copies_are_preserved(paystub_env):
    path = stub_a(paystub_env.inbox_dir / "a.pdf")
    payload = path.read_bytes()
    queued = ingest_file(path, paystub_env)
    dismiss_pending(queued.pending_id, paystub_env)
    for expected in ("needs_mapping", "already_pending"):
        path.write_bytes(payload)
        assert ingest_file(path, paystub_env).status == expected
    files = (
        list(paystub_env.duplicates_dir.iterdir())
        + list(paystub_env.failed_dir.iterdir())
        + list(paystub_env.pending_dir.iterdir())
    )
    assert len(files) == 3
    assert all(file.read_bytes() == payload for file in files)


def test_serialized_ingest_same_file(paystub_env, rule_a):
    save_rule(paystub_env.rules_dir, rule_a)
    path = stub_a(paystub_env.inbox_dir / "a.pdf")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ingest_file(path, paystub_env), range(2)))
    assert sorted(result.status for result in results) == ["imported", "noop"]
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(Paystub)) == 1


def test_committed_stub_survives_move_failure(paystub_env, rule_a, monkeypatch):
    save_rule(paystub_env.rules_dir, rule_a)
    path = stub_a(paystub_env.inbox_dir / "a.pdf")
    original_rename = Path.rename

    def fail_move(self, target):
        raise OSError("simulated interruption")

    monkeypatch.setattr(Path, "rename", fail_move)
    with pytest.raises(OSError):
        ingest_file(path, paystub_env)
    assert path.is_file()
    with Session(get_engine()) as session:
        row = session.scalar(select(Paystub))
        assert data_path(row.source_file, paystub_env) == path
    monkeypatch.setattr(Path, "rename", original_rename)
    assert ingest_file(path, paystub_env).status == "duplicate"
    with Session(get_engine()) as session:
        row = session.scalar(select(Paystub))
        assert data_path(row.source_file, paystub_env).is_file()
        assert session.scalar(select(func.count()).select_from(Paystub)) == 1


def test_recover_pending_after_archive_before_queue_delete(paystub_env, rule_a, resolve):
    pending = ingest_file(stub_a(paystub_env.inbox_dir / "a.pdf"), paystub_env)
    with Session(get_engine()) as session:
        row = session.get(PaystubPending, pending.pending_id)
        digest, source = row.file_sha256, row.source_file
    resolve(pending.pending_id, rule_a)
    with Session(get_engine()) as session:
        session.add(
            PaystubPending(
                file_sha256=digest, source_file=source, status="needs_review", reason="Interrupted"
            )
        )
        session.commit()
    retry_pending(paystub_env)
    with Session(get_engine()) as session:
        assert session.scalar(select(func.count()).select_from(PaystubPending)) == 0


def test_real_observer_imports_dropped_pdf(paystub_env, rule_a, tmp_path):
    save_rule(paystub_env.rules_dir, rule_a)
    payload = stub_a(tmp_path / "fixture.pdf").read_bytes()
    watcher = PaystubWatcher(paystub_env.inbox_dir, lambda path: ingest_file(path, paystub_env))
    watcher.start()
    try:
        (paystub_env.inbox_dir / "drop.pdf").write_bytes(payload)
        deadline = monotonic() + 10
        while monotonic() < deadline:
            with Session(get_engine()) as session:
                count = session.scalar(select(func.count()).select_from(Paystub))
            if count:
                break
            sleep(0.1)
        assert count == 1
    finally:
        watcher.stop()
