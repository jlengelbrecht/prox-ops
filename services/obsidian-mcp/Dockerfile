FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS build
WORKDIR /app
COPY requirements.txt ./
RUN python -m venv /opt/venv && \
    /opt/venv/bin/python -m pip install --no-cache-dir --no-compile --require-hashes -r requirements.txt

FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/jlengelbrecht/prox-ops" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="0.1.0"
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=0:0 --chmod=0444 gateway.py approval.py bridge.py owner_auth.py server.py /app/
COPY --chown=0:0 bridge-plugin/ /opt/obsidian-mcp/bridge-plugin/
RUN chmod 0555 /opt/obsidian-mcp /opt/obsidian-mcp/bridge-plugin && \
    chmod 0444 /opt/obsidian-mcp/bridge-plugin/*
USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1
CMD ["python", "/app/gateway.py"]
