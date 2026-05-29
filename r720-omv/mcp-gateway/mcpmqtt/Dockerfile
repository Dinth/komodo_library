FROM python:3.12-slim

RUN pip install --no-cache-dir mcpMQTT \
 && useradd -r -u 65532 -s /sbin/nologin mcpmqtt

USER 65532:65532
ENTRYPOINT ["mcpMQTT", "--config", "/etc/mcpmqtt/config.json"]
