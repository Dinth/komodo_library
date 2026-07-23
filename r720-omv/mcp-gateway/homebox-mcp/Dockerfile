# Local image for the Homebox MCP server (read-only inventory access).
# Spawned as a stdio MCP server by docker/mcp-gateway (see registry.yaml).
FROM python:3.12-slim

RUN pip install --no-cache-dir "mcp>=1.2" "httpx>=0.27" \
 && useradd -r -u 65532 -s /sbin/nologin homeboxmcp

COPY server.py /app/server.py

USER 65532:65532
ENTRYPOINT ["python", "/app/server.py"]
