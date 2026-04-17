"""v3 Dashboard Backend — v2 `prime_jennie/services/dashboard/` 포팅.

FastAPI app + routers. Postgres(async) + Redis(async) + Loki 프록시.
"""

from .app import create_app

__all__ = ["create_app"]
