"""Keep callback query parameters and employer names out of Uvicorn's request logs."""

import logging
import re

EMPLOYER_PATH = re.compile(r"^(/paycheck/rules/)[^/]+")


class StripQueryString(logging.Filter):
    def filter(self, record):
        # Uvicorn's access record: client, method, path+query, HTTP version, status.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            path = str(args[2]).split("?", 1)[0]
            args[2] = EMPLOYER_PATH.sub(r"\1[employer]", path)
            record.args = tuple(args)
        return True


def install_access_log_filter():
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, StripQueryString) for item in logger.filters):
        logger.addFilter(StripQueryString())
