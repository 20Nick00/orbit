import re
from pathlib import Path

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient as RawClient

from app.main import create_app
from app.security import require_csrf


@pytest.fixture
def browser(client, monkeypatch, tmp_path):
    # Deliberately use the raw client: these tests must not auto-add a CSRF token.
    monkeypatch.setenv("ORBIT_DATA_DIR", str(tmp_path / "data"))
    with RawClient(client.app) as browser:
        yield browser


def test_first_visit_renders_working_cookie_and_hidden_field(browser):
    response = browser.get("/add/manual")
    assert response.status_code == 200
    token = browser.cookies["orbit_csrf"]
    assert len(token) == 43
    assert f'name="csrf_token" value="{token}"' in response.text
    assert f'"X-CSRF-Token": "{token}"' in response.text
    cookie = response.headers["set-cookie"].lower()
    assert (
        "httponly" in cookie
        and "samesite=lax" in cookie
        and "max-age=" in cookie
        and "path=/" in cookie
    )
    assert "secure" not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert browser.get("/health").status_code == 200


def test_https_cookie_is_secure(client):
    with RawClient(client.app, base_url="https://testserver") as browser:
        assert "Secure" in browser.get("/health").headers["set-cookie"]


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://testserver"}])
def test_missing_token_rejected_even_without_origin(browser, headers):
    browser.get("/health")
    assert (
        browser.post("/almanac/notes/2026", data={"note": "no"}, headers=headers).status_code == 403
    )


@pytest.mark.parametrize("token", ["wrong", "x" * 43, "non-ascii-\u2603"])
def test_wrong_token_rejected(browser, token):
    browser.get("/health")
    assert browser.post("/almanac/notes/2026", data={"csrf_token": token}).status_code == 403


def test_token_without_cookie_is_rejected(browser):
    assert browser.post("/almanac/notes/2026", data={"csrf_token": "x" * 43}).status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "https://testserver:8443"},
        {"Origin": "", "Referer": "http://testserver/dashboard"},
        {"Referer": "http://evil.example/dashboard"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_wrong_origin_rejected_even_with_token(browser, headers):
    browser.get("/health")
    assert (
        browser.post(
            "/almanac/notes/2026",
            data={"csrf_token": browser.cookies["orbit_csrf"]},
            headers=headers,
        ).status_code
        == 403
    )


@pytest.mark.parametrize("header", [False, True])
def test_form_and_htmx_tokens_work_and_partial_keeps_token(browser, header):
    browser.get("/health")
    token = browser.cookies["orbit_csrf"]
    data = {"note": "A note"}
    headers = {"HX-Request": "true"}
    if header:
        headers["X-CSRF-Token"] = token
    else:
        data["csrf_token"] = token
    response = browser.post("/almanac/notes/2026", data=data, headers=headers)
    assert response.status_code == 200 and "A note" in response.text
    assert f'name="csrf_token" value="{token}"' in response.text
    assert browser.cookies["orbit_csrf"] == token


@pytest.mark.parametrize("path", ["/add/import", "/paycheck/upload"])
@pytest.mark.parametrize("with_token", [False, True])
def test_multipart_file_survives_csrf_form_read(browser, monkeypatch, path, with_token):
    payload = b"%PDF-test upload"
    called = []

    def process(request, data, metadata):
        assert data == payload
        called.append(True)
        return HTMLResponse("Accepted")

    monkeypatch.setattr("app.routers.add._process", process)
    monkeypatch.setattr("app.routers.paycheck._upload", process)
    browser.get("/health")
    data = {"csrf_token": browser.cookies["orbit_csrf"]} if with_token else {}
    response = browser.post(path, files={"file": ("sample.csv", payload)}, data=data)
    assert response.status_code == (200 if with_token else 403)
    assert bool(called) == with_token


def walk_routes(routes):
    for route in routes:
        yield route
        if hasattr(route, "original_router"):
            yield from walk_routes(route.original_router.routes)
        elif hasattr(route, "routes"):
            yield from walk_routes(route.routes)


def dependency_calls(dependant):
    yield dependant.call
    for child in dependant.dependencies:
        yield from dependency_calls(child)


def test_every_mutating_main_app_route_requires_csrf():
    checked = []
    paths = []
    for route in walk_routes(create_app().routes):
        paths.append(getattr(route, "path", ""))
        if set(getattr(route, "methods", ())) & {"POST", "PUT", "PATCH", "DELETE"}:
            assert require_csrf in dependency_calls(route.dependant), route.path
            checked.append(route.path)
    assert len(checked) >= 20  # Prove included routers were traversed, not silently skipped.
    assert "/plaid/webhook" not in paths


def test_every_post_template_form_contains_hidden_token():
    checked = 0
    for path in Path("app/templates").rglob("*.html"):
        for form in re.findall(r"<form\b.*?</form>", path.read_text(encoding="utf-8"), re.S | re.I):
            if re.search(r"method=[\"\']post[\"\']", form, re.I):
                assert 'type="hidden" name="csrf_token"' in form, path
                assert "request.state.csrf_token" in form, path
                checked += 1
    assert checked >= 20


def test_https_proxy_origin_matches_plain_http_app(browser):
    # `tailscale serve` ends TLS and forwards plain HTTP, keeping the browser's Host.
    browser.get("/health")
    token = browser.cookies["orbit_csrf"]
    host = {"Host": "testserver", "X-CSRF-Token": token}
    allowed = {**host, "Origin": "https://testserver"}
    assert (
        browser.post("/almanac/notes/2026", data={"note": "ok"}, headers=allowed).status_code < 400
    )
    other = {**host, "Origin": "https://testserver:8443"}
    assert (
        browser.post("/almanac/notes/2026", data={"note": "no"}, headers=other).status_code == 403
    )
    wrong = {**host, "Origin": "https://attacker.example"}
    assert (
        browser.post("/almanac/notes/2026", data={"note": "no"}, headers=wrong).status_code == 403
    )


def test_returning_visit_keeps_the_existing_token(browser):
    browser.get("/health")
    token = browser.cookies["orbit_csrf"]
    assert browser.get("/dashboard").status_code == 200
    assert browser.cookies["orbit_csrf"] == token


def test_csv_exports_are_not_stored_by_the_browser(browser):
    response = browser.get("/export/almanac-years.csv")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
