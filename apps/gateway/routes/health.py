from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. Never touches dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, object]:
    """Readiness: can the gateway serve traffic? Reports per-backend discovery status."""
    catalog = request.app.state.catalog
    if not catalog.ready:
        response.status_code = 503
    return {
        "status": "ready" if catalog.ready else "not_ready",
        "backends": {
            name: {"healthy": s.healthy, "tools": s.tool_count, "error": s.error}
            for name, s in catalog.status.items()
        },
    }
