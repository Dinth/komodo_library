FROM python:3.12-slim

# mcpMQTT declares mcp>=1.0.0 with no ceiling, so a fresh build would pull
# mcp 2.x, whose FastMCP -> MCPServer rename breaks v1 servers. Cap it until
# mcpMQTT itself supports 2.x. The weekly rebuild still takes 1.x patches.
RUN pip install --no-cache-dir mcpMQTT "mcp<2" \
 && useradd -r -u 65532 -s /sbin/nologin mcpmqtt

USER 65532:65532
ENTRYPOINT ["mcpMQTT", "--config", "/etc/mcpmqtt/config.json"]
