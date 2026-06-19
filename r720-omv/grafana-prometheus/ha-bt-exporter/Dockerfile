FROM python:3.13-slim

RUN pip install --no-cache-dir "websockets==14.2" "prometheus_client==0.21.1" \
 && useradd -r -u 65532 -s /sbin/nologin htbt

COPY exporter.py /app/exporter.py

USER 65532:65532
WORKDIR /app
ENTRYPOINT ["python", "-u", "/app/exporter.py"]
