from dataclasses import replace
from decimal import Decimal as D

import pytest

from paystubs.parsers.pdf import ParsedLine
from paystubs.rules import (
    Rule,
    classify,
    get_rule,
    load_rules,
    match_rule,
    save_rule,
    slugify,
    suggest_group,
    suggest_limit,
)


def test_classify_examples(parsed_a, parsed_b, rule_a, rule_b):
    a, b = classify(parsed_a, rule_a), classify(parsed_b, rule_b)
    assert a.gross == D("10417")
    assert a.net == D("5890")
    assert a.gross_ytd == D("208340")
    assert a.net_ytd == D("117800")
    assert b.gross == D("4000")
    assert b.net == D("2394")
    assert len(a.lines) == 10
    assert len(b.lines) == 8
    for result in (a, b):
        assert result.reconcile_diff == D("0")
        assert result.reconciles
        assert result.problems == []
        assert result.unknown_labels == []
    match = next(line for line in a.lines if line.group == "employer")
    assert match.limit is None
    assert next(line for line in b.lines if line.group == "employer").limit == "hsa"


def test_unknown_and_missing_date(parsed_a, rule_a):
    parsed_a.lines.append(ParsedLine("Mystery deduction", D("5"), None))
    parsed_a.pay_date = None
    result = classify(parsed_a, rule_a)
    assert result.unknown_labels == ["Mystery deduction"]
    assert "Pay date missing" in result.problems
    assert "Unknown label: Mystery deduction" in result.problems


@pytest.mark.parametrize(
    "net,diff,reconciles",
    [("5891", "-1", False), ("5890.02", "-0.02", True), ("5889.97", "0.03", False)],
)
def test_reconciliation_tolerance(parsed_a, rule_a, net, diff, reconciles):
    parsed_a.lines[-1].amount = D(net)
    result = classify(parsed_a, rule_a)
    assert result.reconcile_diff == D(diff)
    assert result.reconciles is reconciles


def test_missing_totals_and_current_amount(parsed_a, rule_a):
    parsed_a.lines = [line for line in parsed_a.lines if line.label not in {"Gross Pay", "Net Pay"}]
    parsed_a.lines[0].amount = None
    result = classify(parsed_a, rule_a)
    assert result.gross is None and result.net is None
    assert result.gross_ytd is None and result.net_ytd is None
    assert "Gross missing" in result.problems and "Net missing" in result.problems
    assert any("Current amount missing" in problem for problem in result.problems)
    assert result.reconcile_diff is None and not result.reconciles


def test_gross_roles_sum_and_missing_ytd(parsed_a, rule_a):
    parsed_a.lines.append(ParsedLine("Other gross", D("10"), None))
    rule_a.labels["Other gross"] = "gross"
    result = classify(parsed_a, rule_a)
    assert result.gross == D("10427")
    assert result.gross_ytd is None


def test_yaml_roundtrip_merge_and_normalization(tmp_path, rule_a):
    saved = save_rule(tmp_path, rule_a)
    assert get_rule(tmp_path, slugify(rule_a.employer)) == saved
    merged = save_rule(
        tmp_path,
        Rule(
            rule_a.employer,
            ["NORTHWIND TEST", "Another payroll name"],
            {" hSa ": "employer", "New label": "ignore"},
            {" hSa ": "hsa"},
        ),
    )
    assert merged.labels["hsa"] == "employer"
    assert merged.labels["new label"] == "ignore"
    assert merged.labels["gross pay"] == "gross"
    assert merged.limits["hsa"] == "hsa"
    assert "northwind test labs" in merged.match
    assert "another payroll name" in merged.match
    assert load_rules(tmp_path) == [merged]
    assert not list(tmp_path.glob("*.tmp"))
    assert (
        not (tmp_path / f"{slugify(rule_a.employer)}.yaml").read_bytes().startswith(b"\xef\xbb\xbf")
    )


def test_merge_limit_for_existing_label(tmp_path):
    save_rule(tmp_path, Rule("Fake Test", ["fake test"], {"HSA": "pretax"}))
    saved = save_rule(tmp_path, Rule("Fake Test", ["fake test"], {}, {"HSA": "hsa"}))
    assert saved.labels == {"hsa": "pretax"}
    assert saved.limits == {"hsa": "hsa"}


@pytest.mark.parametrize(
    "bad",
    [
        Rule("Fake", ["fake"], {"x": "wrong"}),
        Rule("Fake", ["fake"], {"x": "pretax"}, {"x": "bad"}),
        Rule("Fake", ["fake"], {"match": "employer"}, {"match": "401k"}),
        Rule("Fake", ["fake"], {"x": "tax"}, {"unknown": "hsa"}),
    ],
)
def test_invalid_rule_cannot_be_saved(tmp_path, bad):
    with pytest.raises(ValueError):
        save_rule(tmp_path, bad)
    assert list(tmp_path.iterdir()) == []


def test_malformed_rules_report_file_only(tmp_path, rule_a, caplog):
    save_rule(tmp_path, rule_a)
    (tmp_path / "broken.yaml").write_text("employer: [secret-pay-data", encoding="utf-8")
    (tmp_path / "unsafe.yaml").write_text(
        "!!python/object/apply:os.system ['secret-pay-data']", encoding="utf-8"
    )
    loaded = load_rules(tmp_path)
    assert len(loaded) == 1
    assert loaded.errors == ["broken.yaml", "unsafe.yaml"]
    assert "secret-pay-data" not in caplog.text
    # Rule files are named after employers, so logs leave the name out; the page lists it.
    assert "broken.yaml" not in caplog.text
    assert get_rule(tmp_path, "broken") is None


def test_slug_traversal_and_collision(tmp_path):
    assert slugify("../../evil") == "evil"
    assert slugify("Northwind Test Labs Inc.") == "northwind-test-labs-inc"
    with pytest.raises(ValueError):
        slugify("...///")
    with pytest.raises(ValueError):
        get_rule(tmp_path, "../../evil")
    save_rule(tmp_path, Rule("../../evil", ["fake"], {}))
    assert (tmp_path / "evil.yaml").is_file()
    save_rule(tmp_path, Rule("Fake & Co", ["fake"], {}))
    with pytest.raises(ValueError, match="slug"):
        save_rule(tmp_path, Rule("Fake Co", ["fake"], {}))


def test_longest_match_and_tie():
    short = Rule("One", ["test"], {})
    long = Rule("Two", ["Northwind Test Labs"], {})
    assert match_rule([short, long], "NORTHWIND   test\nLABS") is long
    assert match_rule([short, long, replace(long, employer="Three")], "Northwind Test Labs") is None
    assert match_rule([short, long], "unmatched") is None


@pytest.mark.parametrize(
    "label,group,limit",
    [
        ("Employer 401(k) Match", "employer", None),
        ("ER HSA", "employer", "hsa"),
        ("Company Medical", "employer", None),
        ("Net Pay", "net", None),
        ("Total Earnings", "gross", None),
        ("Gross Pay", "gross", None),
        ("Roth 401K", "posttax", "401k"),
        ("After-tax 403(b)", "posttax", None),
        ("After-Tax 401(k)", "posttax", None),
        ("Roth 401(k)", "posttax", "401k"),
        ("401(k) Pre-tax", "pretax", "401k"),
        ("HSA", "pretax", "hsa"),
        ("Federal Income Tax", "tax", None),
        ("OASDI", "tax", None),
        ("FICA", "tax", None),
        ("Medical Premium", "benefit", None),
        ("FSA", "benefit", None),
        ("Regular", "ignore", None),
        ("Hours", "ignore", None),
        ("Mystery", None, None),
    ],
)
def test_suggestions(label, group, limit):
    assert suggest_group(label) == group
    assert suggest_limit(label, group) == limit
