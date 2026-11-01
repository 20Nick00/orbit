import pytest


def test_stylesheet(client):
    response = client.get("/static/orbit.css")

    assert response.status_code == 200
    assert "--accent:#0E6E7A" in response.text
    assert "--accent:#4FB8C4" in response.text
    assert "data-brand" not in response.text


@pytest.mark.parametrize(
    "path",
    ["htmx.min.js", "fonts/manrope-latin-wght-normal.woff2"],
)
def test_vendor_asset(client, path):
    response = client.get(f"/static/vendor/{path}")

    assert response.status_code == 200
    assert response.content
