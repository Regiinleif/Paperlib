# Container image for the PaperLib web app (FastAPI backend + HTML dashboard).
#
# Only the server is containerized — the legacy Tkinter desktop client is not
# (a container has no display). Build and run locally:
#
#     docker build -t paperlib .
#     docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... paperlib
#
# then open http://127.0.0.1:8000. The chat/agent need a key; upload,
# categorization, retrieval and the eval harness work without one.

FROM python:3.13-slim

# - PYTHONDONTWRITEBYTECODE: no .pyc files in the image layer.
# - PYTHONUNBUFFERED: logs stream out immediately (important under a container
#   log collector like Azure Container Apps).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (their own layer) so code-only changes don't bust
# the pip cache. Only the slim web subset is installed — see requirements-web.txt.
COPY requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

# Now the application code and its static assets.
COPY src/ ./src/

# The app persists user data (library.db, uploaded papers) under these dirs.
# In a container they are ephemeral unless a volume is mounted — for a durable
# deployment, mount storage at /app/data and /app/papers.
RUN mkdir -p /app/data /app/papers

# Run as a non-root user (defense in depth; also required by some platforms).
# The app dir must be writable so the SQLite DB and papers/ can be created.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# A container-level health probe hitting the app's own liveness endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"

# Bind to 0.0.0.0 so the port is reachable from outside the container.
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
