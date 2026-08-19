FROM python:3.13-slim

# Standard library only - nothing to install.
COPY shim.py /app/shim.py

USER 65532:65532
WORKDIR /app
ENTRYPOINT ["python", "-u", "/app/shim.py"]
