"""Browser-like test client: obtain the cookie and submit its matching token."""

from fastapi.testclient import TestClient as BaseClient


class TestClient(BaseClient):
    __test__ = False

    def request(self, method, url, **kwargs):
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            if "orbit_csrf" not in self.cookies:
                super().request("GET", "/health")
            token = self.cookies.get("orbit_csrf")
            headers = dict(kwargs.get("headers") or {})
            data = kwargs.get("data")
            explicit = any(key.lower() == "x-csrf-token" for key in headers) or (
                isinstance(data, dict) and "csrf_token" in data
            )
            if token and not explicit:
                headers["X-CSRF-Token"] = token
            kwargs["headers"] = headers
        return super().request(method, url, **kwargs)
