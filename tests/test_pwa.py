import json
import re
import struct
from pathlib import Path


def test_manifest_routes_and_icons(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["name"] == "Orbit"
    assert manifest["start_url"] == "/dashboard"
    assert manifest["display"] == "standalone" and manifest["scope"] == "/"
    css = Path("app/static/orbit.css").read_text(encoding="utf-8")
    assert manifest["theme_color"] in css and manifest["background_color"] in css
    assert {icon["purpose"] for icon in manifest["icons"]} == {"any", "maskable"}
    for icon in manifest["icons"]:
        blob = Path("app" + icon["src"]).read_bytes()
        assert blob[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", blob[16:24])
        assert icon["sizes"] == f"{width}x{height}"
        assert len(blob) < 100_000
    apple = Path("app/static/icons/apple-touch-icon.png").read_bytes()
    assert struct.unpack(">II", apple[16:24]) == (180, 180)


def test_worker_only_precaches_explicit_public_shell(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    source = response.text
    paths = json.loads(
        re.search(r"const SHELL_PATHS = (\[.*?\]);", source, re.S)[1].replace("'", '"')
    )
    expected = {
        "/static/orbit.css",
        "/static/paycheck.css",
        "/static/charts.js",
        "/static/paycheck.js",
        "/static/trends.js",
        "/static/pwa.js",
        "/static/vendor/htmx.min.js",
        "/static/vendor/fonts/manrope-latin-wght-normal.woff2",
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/icon-maskable-512.png",
        "/static/icons/apple-touch-icon.png",
        "/static/offline.html",
    }
    assert set(paths) == expected
    assert all(Path("app" + path).is_file() for path in paths)
    assert [path for path in paths if path.endswith(".html")] == ["/static/offline.html"]
    # There is exactly one cache-writing operation, restricted to the explicit list.
    assert source.count("cache.addAll(") == 1
    assert "cache.addAll(SHELL_PATHS)" in source
    assert "cache.put" not in source and "cache.add(" not in source
    assert "request.method !== 'GET'" in source
    assert "url.origin !== self.location.origin" in source
    assert "url.search || !SHELL_PATHS.includes(url.pathname)" in source
    assert "request.mode === 'navigate'" in source
    assert "caches.delete(name)" in source
    offline = client.get("/static/offline.html")
    assert offline.status_code == 200 and "Orbit can't be reached" in offline.text
