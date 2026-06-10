"""agentforge.observability — structured logging, metrics, request tracing.

Submodules:
  - logging   (configure_logging, JsonFormatter)
  - metrics   (Counter, Histogram, MetricsRegistry, get_registry)
  - context   (request_id contextvar)
  - middleware (RequestIdMiddleware — ASGI)

Library import (import agentforge) is side-effect-free. Nothing here
initializes on import — you must call configure_logging() and
get_registry() explicitly.
"""
