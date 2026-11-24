FROM python:3.13-slim

ARG IMAGE_VERSION=dev

COPY app/bridge.py /app/bridge.py

ENV PYTHONUNBUFFERED=1 \
    A2A_BRIDGE_VERSION=$IMAGE_VERSION \
    A2A_DATA_DIR=/agent/data \
    A2A_PORT=8080

# Runs as root on purpose: the inbox is written by the agent, which is root.
VOLUME ["/agent/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['A2A_PORT'])"
ENTRYPOINT ["python", "/app/bridge.py"]
