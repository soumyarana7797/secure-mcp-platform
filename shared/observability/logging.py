"""Minimal log setup: every line carries the current request ID. Phase 7 makes this structured."""

import logging

from shared.observability.context import request_id_var

FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(level: str = "INFO") -> None:
    logger = logging.getLogger("apps")
    if any(isinstance(f, RequestIdFilter) for h in logger.handlers for f in h.filters):
        return  # already configured (create_app can run many times in tests)
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(logging.Formatter(FORMAT))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
