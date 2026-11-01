from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "app" / "static"


def test_trends_uses_the_chart_function_charts_js_exports():
    assert "window.OrbitCharts=" in (STATIC / "charts.js").read_text(encoding="utf-8")
    assert "{barChart} = window.OrbitCharts" in (STATIC / "trends.js").read_text(encoding="utf-8")
