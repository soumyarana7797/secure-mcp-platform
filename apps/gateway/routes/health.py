from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. Never touches dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, object]:
    """Readiness: registry loaded, a backend serving tools, and the database reachable."""
    catalog = request.app.state.catalog
    db = request.app.state.db
    db_ok = await db.ping() if db is not None else True
    ready = catalog.ready and db_ok
    if not ready:
        response.status_code = 503
    return {
        "status": "ready" if ready else "not_ready",
        "database": db_ok,
        # Public endpoint: keep it minimal. Drift and unregistered-tool details go to the logs.
        "backends": {
            name: {
                "healthy": s.healthy,
                "exposed_tools": s.exposed,
                "degraded": bool(s.drifted or s.missing),
            }
            for name, s in catalog.status.items()
        },
    }
