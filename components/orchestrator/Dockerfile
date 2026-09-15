FROM public.ecr.aws/docker/library/python:3.12-slim AS base
WORKDIR /app

COPY pyproject.toml .
COPY app/ app/

# The shared VTON prompt-engineering package is synced into this build context
# (canonical copy: components/shared/vton_engines) and the synced copy is
# gitignored, so a fresh clone has no app/vton_engines. Without this guard the
# image builds fine and then raises ImportError on the first try-on request.
# `./deploy/storeai up` syncs automatically; for a direct build, run
# scripts/sync-vton-engines.sh from the repo root first.
RUN test -f app/vton_engines/__init__.py || { \
      echo "ERROR: app/vton_engines/ is missing from the build context." >&2; \
      echo "Run ./scripts/sync-vton-engines.sh from the repo root, then rebuild." >&2; \
      exit 1; }

RUN pip install --no-cache-dir . && \
    rm -rf /root/.cache/pip

EXPOSE 8080

# Run as non-root (matches the k8s securityContext runAsUser 10001).
USER 10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=3).status==200 else 1)"]

# Workers=2 for concurrency on Graviton (2 vCPU pod), keep-alive for ALB health checks
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2", "--timeout-keep-alive", "75"]
