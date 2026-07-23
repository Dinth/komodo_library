"""Homebox MCP server.

Purpose:
    A minimal, read-only Model Context Protocol (stdio) server exposing the
    Homebox home-inventory API (sysadminsmedia fork, v0.26.x) as MCP tools.
    Spawned as a container by docker/mcp-gateway (listed in the gateway's
    registry.yaml) and multiplexed to clients over the gateway's SSE endpoint.

    Targets the 0.26.x API surface (entities / tags / entity-types) -- NOT the
    legacy items/locations/labels routes, which were removed in the 0.24 refactor.

Dependencies:
    - Python 3.12+
    - mcp   (official MCP SDK; provides mcp.server.fastmcp.FastMCP)
    - httpx (async HTTP client)

Configuration (environment variables):
    HOMEBOX_URL       Base URL of the Homebox instance, e.g. http://homebox:7745
    HOMEBOX_EMAIL     Login username/email of a member of the target group.
    HOMEBOX_PASSWORD  Password for that account.

Author: AI (Claude)
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("HOMEBOX_URL", "http://homebox:7745").rstrip("/")
EMAIL = os.environ.get("HOMEBOX_EMAIL", "")
PASSWORD = os.environ.get("HOMEBOX_PASSWORD", "")
API = f"{BASE_URL}/api/v1"

mcp = FastMCP("homebox")

# Cached bearer token + the header scheme that Homebox actually accepted, so we
# only probe "Bearer <t>" vs raw "<t>" once per process.
_token: str | None = None
_token_expiry: float = 0.0
_auth_prefix: str = "Bearer "  # flipped to "" if the server rejects Bearer


async def _login(client: httpx.AsyncClient) -> str:
    """Authenticate against Homebox and return a fresh session token."""
    global _token, _token_expiry
    if not EMAIL or not PASSWORD:
        raise RuntimeError("HOMEBOX_EMAIL / HOMEBOX_PASSWORD are not set")
    resp = await client.post(
        f"{API}/users/login",
        json={"username": EMAIL, "password": PASSWORD, "stayLoggedIn": True},
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"login succeeded but no token in response: {data}")
    _token = token
    # Refresh a little before the hour-ish default expiry; cheap to re-login.
    _token_expiry = time.time() + 30 * 60
    return token


async def _request(method: str, path: str, params: dict[str, Any] | None = None) -> Any:
    """Perform an authenticated Homebox API request, re-logging in as needed.

    Tries the cached auth scheme first; on a 401 it re-logs in and, once, flips
    the Bearer/raw prefix in case the server wants the token verbatim.
    """
    global _auth_prefix
    async with httpx.AsyncClient(timeout=20.0) as client:
        if _token is None or time.time() >= _token_expiry:
            await _login(client)

        for attempt in range(2):
            headers = {"Authorization": f"{_auth_prefix}{_token}"}
            resp = await client.request(method, f"{API}{path}", params=params, headers=headers)
            if resp.status_code == 401 and attempt == 0:
                # Either the token expired or the prefix is wrong; try both.
                _auth_prefix = "" if _auth_prefix else "Bearer "
                await _login(client)
                continue
            resp.raise_for_status()
            if resp.content:
                return resp.json()
            return None
    raise RuntimeError("unreachable")


@mcp.tool()
async def search_entities(query: str = "", page: int = 1, page_size: int = 25) -> Any:
    """Search inventory entities (items) by name/description.

    Args:
        query: Free-text search string. Empty returns everything (paginated).
        page: 1-based page number.
        page_size: Results per page (max ~100 recommended).
    """
    return await _request(
        "GET", "/entities",
        params={"q": query, "page": page, "pageSize": page_size},
    )


@mcp.tool()
async def get_entity(entity_id: str) -> Any:
    """Get full details of a single entity (item or location) by its UUID."""
    return await _request("GET", f"/entities/{entity_id}")


@mcp.tool()
async def list_locations(page: int = 1, page_size: int = 100) -> Any:
    """List storage locations (entities flagged as locations)."""
    return await _request(
        "GET", "/entities",
        params={"isLocation": "true", "page": page, "pageSize": page_size},
    )


@mcp.tool()
async def get_entities_in_location(location_id: str, page: int = 1, page_size: int = 100) -> Any:
    """List entities whose parent is the given location UUID."""
    return await _request(
        "GET", "/entities",
        params={"parentIds": location_id, "page": page, "pageSize": page_size},
    )


@mcp.tool()
async def list_tags() -> Any:
    """List all tags (labels) defined in the inventory."""
    return await _request("GET", "/tags")


@mcp.tool()
async def get_entities_by_tag(tag_id: str, page: int = 1, page_size: int = 100) -> Any:
    """List entities carrying the given tag UUID."""
    return await _request(
        "GET", "/entities",
        params={"tags": tag_id, "page": page, "pageSize": page_size},
    )


@mcp.tool()
async def list_entity_types() -> Any:
    """List entity types (e.g. 'Item', 'Location') defined in the group."""
    return await _request("GET", "/entity-types")


if __name__ == "__main__":
    mcp.run()
