"""Use the owner's calendar day, including when the server runs in UTC."""

from datetime import datetime
from zoneinfo import ZoneInfo

from tzlocal import get_localzone

from core.config import get_settings


def local_timezone(settings=None):
    settings = settings or get_settings()
    return ZoneInfo(settings.timezone) if settings.timezone else get_localzone()


def local_today():
    return datetime.now(local_timezone()).date()
