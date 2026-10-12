"""Calls user + knowledge tools through the gateway as a seeded dev user.

    GATEWAY_URL=http://127.0.0.1:9010/mcp uv run python scripts/phase5_demo.py sub-alice
Requires the gateway to run with GATEWAY_AUTH_MODE=dev_header (local development only).
"""

import asyncio
import os
import sys

import httpx2
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client

URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:9000/mcp")
QUESTION = "how many vacation days carry over to next year"


async def main(sub: str) -> None:
    http = httpx2.AsyncClient(headers={"X-Dev-Sub": sub})
    async with Client(streamable_http_client(URL, http_client=http)) as client:
        print(f"[{sub}] tools:", sorted(t.name for t in (await client.list_tools()).tools))
        for name, args in (
            ("user.get_user_profile", {}),
            ("knowledge.search_company_docs", {"query": QUESTION, "top_k": 2}),
        ):
            try:
                result = await client.call_tool(name, args)
            except MCPError as exc:
                print(f"[{sub}] {name} -> protocol error: {exc}")
                continue
            if result.is_error:
                print(f"[{sub}] {name} -> tool error: {result.content[0].text}")
            elif name.startswith("user"):
                p = result.structured_content
                print(
                    f"[{sub}] profile: {p['display_name']} <{p['email']}> roles={p['roles']} "
                    f"tenant={p['tenant']['slug']}"
                )
            else:
                for hit in result.structured_content["hits"]:
                    print(
                        f"[{sub}] hit {hit['score']:.2f} | {hit['title']}: {hit['snippet'][:70]}..."
                    )
                if not result.structured_content["hits"]:
                    print(f"[{sub}] no results")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "sub-alice"))
