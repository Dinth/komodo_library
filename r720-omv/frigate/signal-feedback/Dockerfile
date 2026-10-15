FROM python:3.13-slim

# Standard library only - nothing to install.
COPY feedback.py /app/feedback.py

USER 65532:65532
WORKDIR /app
ENTRYPOINT ["python", "-u", "/app/feedback.py"]
