"""OAuth 2.0 Protected Resource Metadata (RFC 9728). MCP clients read this to learn which
authorization server to use. It is public by design and contains no secrets."""

from fastapi import APIRouter, Request

router = APIRouter(tags=["oauth"])


def _metadata(request: Request) -> dict[str, object]:
    cfg = request.app.state.settings
    return {
        "resource": f"{cfg.public_base_url.rstrip('/')}/mcp",
        "authorization_servers": [cfg.cognito_issuer] if cfg.cognito_issuer else [],
        "scopes_supported": [cfg.required_scope],
        "bearer_methods_supported": ["header"],
    }


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata(request: Request) -> dict[str, object]:
    return _metadata(request)


@router.get("/.well-known/oauth-protected-resource/mcp")  # path-suffixed form some clients try
async def protected_resource_metadata_for_mcp(request: Request) -> dict[str, object]:
    return _metadata(request)
