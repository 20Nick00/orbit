import logging

from core.clock import local_today
from property.valuation import refresh_due

logger = logging.getLogger(__name__)


def run():
    try:
        return refresh_due(local_today())
    except Exception as exc:
        logger.warning("Property refresh job failed (%s).", type(exc).__name__)
        return None
