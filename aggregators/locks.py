"""Shared process-local serialization for scheduled, webhook and UI work."""

from threading import Lock

connections_lock = Lock()
_guard = Lock()
_items = {}


def item_lock(item_id):
    with _guard:
        return _items.setdefault(item_id, Lock())
