import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def extract(source, name):
    """These prototype helpers have balanced braces, including template literals."""
    constant = re.search(rf"^const {name}=.*$", source, re.MULTILINE)
    if constant:
        return constant.group()
    start = source.index(f"function {name}(")
    opening = source.index("{", start)
    # Default options use {} before the function body.
    while source[opening + 1 : opening + 3] == "})":
        opening = source.index("{", opening + 1)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Unterminated function {name}")


@pytest.mark.parametrize(
    "name", ["areaChart", "barChart", "waterfall", "mk", "fmt", "fmtS", "fmtK", "dayStr", "el"]
)
def test_chart_helpers_match_prototype(name):
    prototype = (ROOT / "docs/index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
    port = (ROOT / "app/static/charts.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert extract(prototype, name) in port


def test_spark_exists():
    assert "function spark(" in (ROOT / "app/static/charts.js").read_text(encoding="utf-8")
