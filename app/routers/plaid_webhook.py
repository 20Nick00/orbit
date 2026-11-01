"""Verify Plaid's signed raw request before interpreting any webhook data."""

import base64
import hashlib
import hmac
import json
import math
import threading
import time
from collections import deque

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi import APIRouter, BackgroundTasks, Request, Response
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from aggregators.plaid import PlaidClient
from core.config import get_settings
from core.db import session_scope
from core.models import PlaidItem
from jobs.nightly import sync_transactions

router = APIRouter()
MAX_BODY = 1024 * 1024
_keys = {}
_key_fetches = deque()
_key_lock = threading.Lock()


def _decode(value):
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _key(kid, client):
    with _key_lock:
        now = time.monotonic()
        cached = _keys.get(kid)
        if cached is not None and now < cached[0]:
            if cached[1] is None:
                raise ValueError("Invalid key")
            return cached[1]
        while _key_fetches and _key_fetches[0] <= now - 60:
            _key_fetches.popleft()
        if len(_key_fetches) >= 10:
            raise ValueError("Key fetch limit reached")
        _key_fetches.append(now)
    try:
        # Plaid can be slow. Never hold the cache lock over a network request.
        key = (client or PlaidClient()).get_webhook_key(kid)["key"]
        if key.get("kid") != kid or key.get("expired_at") is not None:
            raise ValueError("Invalid key")
        if key.get("alg") != "ES256" or key.get("kty") != "EC" or key.get("crv") != "P-256":
            raise ValueError("Invalid key")
        public_key = ec.EllipticCurvePublicNumbers(
            int.from_bytes(_decode(key["x"]), "big"),
            int.from_bytes(_decode(key["y"]), "big"),
            ec.SECP256R1(),
        ).public_key()
    except Exception:
        _cache_key(kid, None, 60)
        raise ValueError("Invalid key") from None
    _cache_key(kid, public_key, 300)
    return public_key


def _cache_key(kid, key, lifetime):
    with _key_lock:
        if kid not in _keys and len(_keys) >= 32:
            del _keys[min(_keys, key=lambda key_id: _keys[key_id][0])]
        _keys[kid] = (time.monotonic() + lifetime, key)


def verify(raw, token, client=None):
    try:
        if not token or len(token) > 8192:
            return False
        header_part, payload_part, signature_part = token.split(".")
        header = json.loads(_decode(header_part))
        if header.get("alg") != "ES256":
            return False
        kid = header.get("kid")
        if not isinstance(kid, str) or not 1 <= len(kid) <= 128:
            return False
        signature = _decode(signature_part)
        if len(signature) != 64:
            return False
        key = _key(kid, client)
        key.verify(
            encode_dss_signature(
                int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
            ),
            f"{header_part}.{payload_part}".encode("ascii"),
            ec.ECDSA(hashes.SHA256()),
        )
        claims = json.loads(_decode(payload_part))
        issued = claims.get("iat")
        if type(issued) not in {int, float} or not math.isfinite(issued):
            return False
        now = time.time()
        if not now - 300 <= issued <= now + 60:
            return False
        digest = claims.get("request_body_sha256")
        return isinstance(digest, str) and hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(), digest
        )
    except Exception:
        return False


def _item_id(data):
    if not isinstance(data, dict) or (data.get("webhook_type"), data.get("webhook_code")) != (
        "TRANSACTIONS",
        "SYNC_UPDATES_AVAILABLE",
    ):
        return None
    external_id = data.get("item_id")
    if not isinstance(external_id, str):
        return None
    with session_scope() as session:
        return session.scalar(select(PlaidItem.id).where(PlaidItem.item_id == external_id))


@router.post("/plaid/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    if not get_settings().plaid_configured:
        return Response(status_code=503)
    payload = bytearray()
    try:
        async for chunk in request.stream():
            if len(payload) + len(chunk) > MAX_BODY:
                return Response(status_code=401)
            payload.extend(chunk)
    except Exception:
        return Response(status_code=401)
    raw = bytes(payload)
    if not await run_in_threadpool(verify, raw, request.headers.get("Plaid-Verification")):
        return Response(status_code=401)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return Response(status_code=401)
    item_id = await run_in_threadpool(_item_id, data)
    if item_id is not None:
        background_tasks.add_task(sync_transactions, item_id)
    return Response(status_code=200)
