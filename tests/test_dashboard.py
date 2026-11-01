from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest


def test_dashboard(client):
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Orbit" in response.text
    assert "orbit.css" in response.text
    assert "htmx.min.js" in response.text
    assert "No accounts yet." in response.text


def test_root_redirect(client):
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard"


def test_dashboard_has_only_local_references(client):
    references = []

    class ReferenceParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            references.extend(value for name, value in attrs if name in {"src", "href"})

    response = client.get("/dashboard")
    ReferenceParser().feed(response.text)

    assert response.status_code == 200
    assert references
    for reference in references:
        assert "http://" not in reference
        assert "https://" not in reference
        assert not urlsplit(reference).netloc


@pytest.fixture
def seeded_client(client):
    from datetime import date

    from sqlalchemy.orm import Session

    from app.routers.dashboard import get_today
    from core.db import get_engine
    from core.demo import seed_demo
    from core.models import Base

    Base.metadata.create_all(get_engine())
    with Session(get_engine()) as session:
        seed_demo(session, date(2026, 10, 31))
        session.commit()
    client.app.dependency_overrides[get_today] = lambda: date(2026, 10, 31)
    return client


def test_seeded_dashboard_html(seeded_client):
    from core.demo import ACCTS

    response = seeded_client.get("/dashboard")
    assert response.status_code == 200
    page = response.text
    assert '<div class="big num" id="nwBig">$504,500</div>' in page
    assert "$1,045,020" in page
    assert "−$540,520" in page
    assert "updated 29d ago" in page
    assert "updated today" in page
    assert "Example data" in page
    assert "Updated today · 15 accounts" in page
    for account in ACCTS:
        assert account["name"] in page
    assert page.count('class="row" href="/account/') == 15
    assert 'href="/type/cash"' in page
    assert 'href="/type/mortgage"' in page
    assert 'hx-sync="closest .seg:replace"' in page
    assert page.count('aria-pressed="true"') == 1
    assert page.count('aria-pressed="false"') == 4
    assert 'as="font"' in page
    assert page.index("htmx.min.js") < page.index("charts.js")


def test_update_ages_advance_and_cache_stays_warm(seeded_client, monkeypatch):
    from datetime import date

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.routers.dashboard import get_today
    from core import rollups
    from core.db import get_engine
    from core.models import RollupCache

    assert "updated 29d ago" in seeded_client.get("/dashboard").text
    seeded_client.app.dependency_overrides[get_today] = lambda: date(2026, 11, 1)
    assert "updated 30d ago" in seeded_client.get("/dashboard").text

    def unexpected_rebuild(*args):
        pytest.fail("Warm same-day cache should not rebuild")

    monkeypatch.setattr(rollups, "rebuild", unexpected_rebuild)
    page = seeded_client.get("/dashboard").text
    assert "updated 30d ago" in page
    assert "updated 1d ago" in page
    assert "Updated 1d ago · 15 accounts" in page
    with Session(get_engine()) as session:
        assert set(session.scalars(select(RollupCache.as_of))) == {date(2026, 11, 1)}


@pytest.mark.parametrize("range_key", ["1M", "3M", "6M", "1Y", "All"])
def test_chart_is_only_a_fragment(seeded_client, range_key):
    import json

    charts = []

    class ChartParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            charts.extend(json.loads(value) for name, value in attrs if name == "data-chart")

    response = seeded_client.get("/dashboard/chart", params={"range": range_key})
    assert response.status_code == 200
    assert "<html" not in response.text
    assert "Total Checking" not in response.text
    assert "Sapphire Reserve" not in response.text
    assert 'class="chartwrap"' in response.text
    ChartParser().feed(response.text)
    assert len(charts) == 1
    assert charts[0]["range"] == range_key


def test_unknown_chart_range_is_bad_request(client):
    assert client.get("/dashboard/chart?range=invalid").status_code == 400


def test_empty_chart_on_unmigrated_database(client):
    response = client.get("/dashboard/chart")
    assert response.status_code == 200
    assert '"series": []' in response.text


def test_unrecorded_balances_render_missing(client):
    from sqlalchemy.orm import Session

    from core.db import get_engine
    from core.models import Account, Base

    Base.metadata.create_all(get_engine())
    with Session(get_engine()) as session:
        session.add(Account(name="Waiting for first sync", type="cash"))
        session.commit()
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert '<div class="big num" id="nwBig">—</div>' in response.text
    assert "Waiting for first sync" in response.text
    assert "updated —" in response.text


def test_warm_10k_dashboard_reads_only_cache_under_100ms(seeded_client):
    from datetime import date
    from statistics import median
    from time import perf_counter

    from sqlalchemy import event, func, select
    from sqlalchemy.orm import Session

    from core.db import get_engine
    from core.demo import seed_demo
    from core.models import Transaction

    engine = get_engine()
    with Session(engine) as session:
        seed_demo(session, date(2026, 10, 31), transactions=10000)
        session.commit()
        assert session.scalar(select(func.count()).select_from(Transaction)) == 10000
    assert seeded_client.get("/dashboard").status_code == 200
    statements, elapsed = [], []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        for _ in range(20):
            started = perf_counter()
            response = seeded_client.get("/dashboard")
            elapsed.append(perf_counter() - started)
            assert response.status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 40
    assert all("transactions" not in sql and "balance_snapshots" not in sql for sql in statements)
    assert all(
        "from rollup_cache" in sql and 'where rollup_cache."key" =' in sql for sql in statements
    )
    assert median(elapsed) < 0.100, f"Median of 20 requests: {median(elapsed) * 1000:.1f} ms"
