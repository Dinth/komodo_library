# Local image for the Homebox MCP server (read-only inventory access).
# Spawned as a stdio MCP server by docker/mcp-gateway (see registry.yaml).
FROM python:3.12-slim

# mcp<2: 2.x renamed FastMCP to MCPServer and server.py imports
# mcp.server.fastmcp -- an uncapped fresh build crashes at import (checked
# 2026-09-26 against mcp 2.2.0). The weekly rebuild still takes 1.x patches;
# moving to 2.x is a deliberate port of server.py.
RUN pip install --no-cache-dir "mcp>=1.2,<2" "httpx>=0.27" \
 && useradd -r -u 65532 -s /sbin/nologin homeboxmcp

COPY server.py /app/server.py

USER 65532:65532
ENTRYPOINT ["python", "/app/server.py"]
