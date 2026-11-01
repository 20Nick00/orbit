"""Browser origin checks and double-submit CSRF protection for the main app."""

import re
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.responses import Response

COOKIE = "orbit_csrf"
COOKIE_SECONDS = 365 * 24 * 60 * 60
# The largest legitimate form is a paystub upload (20 MB) plus its fields.
MAX_FORM_BYTES = 21 * 1024 * 1024
PRIVATE_TYPES = ("text/html", "text/csv")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class CSRFMiddleware:
    """Set the token before rendering; append cookies without buffering responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        existing = request.cookies.get(COOKIE, "")
        token = existing if TOKEN.fullmatch(existing) else secrets.token_urlsafe(32)
        scope.setdefault("state", {})["csrf_token"] = token

        async def send_with_cookie(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # Pages and exports hold financial information; pages also hold the token.
                if headers.get("content-type", "").startswith(PRIVATE_TYPES):
                    headers["Cache-Control"] = "no-store"
                if token != existing:
                    response = Response()
                    response.set_cookie(
                        COOKIE,
                        token,
                        httponly=True,
                        # Strict would drop the cookie when arriving by a link, replacing the
                        # token and breaking open tabs. POSTs still need the Origin and token.
                        samesite="lax",
                        max_age=COOKIE_SECONDS,
                        path="/",
                        secure=scope.get("scheme") == "https",
                    )
                    headers.append("set-cookie", response.headers["set-cookie"])
            await send(message)

        await self.app(scope, receive, send_with_cookie)


def _same_host(source, host_header):
    parsed = urlsplit(source)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Invalid origin")
    # Compare host and port only: behind an HTTPS proxy such as `tailscale serve` the app
    # sees plain HTTP, so its own scheme is not the one the browser used.
    host = urlsplit("//" + host_header)
    default = 443 if parsed.scheme == "https" else 80
    return parsed.hostname == host.hostname and (parsed.port or default) == (host.port or default)


async def require_csrf(request: Request):
    error = HTTPException(403, "This form must be submitted from Orbit. Reload and try again.")
    if request.headers.get("sec-fetch-site", "").lower() in {"cross-site", "same-site"}:
        raise error
    # A supplied Origin takes precedence, including an empty/invalid value.
    source = request.headers.get("origin", request.headers.get("referer"))
    if source is not None:
        try:
            if not _same_host(source, request.headers.get("host", "")):
                raise error
        except ValueError:
            raise error from None
    cookie = request.cookies.get(COOKIE, "")
    token = request.headers.get("x-csrf-token")
    if token is None:
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_FORM_BYTES:
            raise HTTPException(413, "That upload is too large.")
        token = (await request.form()).get("csrf_token")
    if (
        not TOKEN.fullmatch(cookie)
        or not isinstance(token, str)
        or not TOKEN.fullmatch(token)
        or not secrets.compare_digest(cookie, token)
    ):
        raise error
