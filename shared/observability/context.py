"""Request-scoped context shared by logging, audit and tracing."""

from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
