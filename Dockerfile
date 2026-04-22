# syntax=docker/dockerfile:1
# --- build stage ---
FROM python:3.11-slim AS builder

WORKDIR /build

COPY pyproject.toml README.md ./
COPY parallax/ parallax/

RUN pip install --upgrade pip && \
    pip install --no-cache-dir ".[server]" --target /install

# --- runtime stage ---
FROM python:3.11-slim AS runtime

# non-root user
RUN addgroup --system parallax && adduser --system --ingroup parallax parallax

WORKDIR /app

# copy installed packages from builder
COPY --from=builder /install /usr/local/lib/python3.11/site-packages/

# copy application source
COPY --chown=parallax:parallax parallax/ parallax/
COPY --chown=parallax:parallax pyproject.toml README.md ./

# install the package itself (editable-style: just the entry point)
RUN pip install --no-cache-dir --no-deps ".[server]"

# data directory for volumes
RUN mkdir -p /data && chown parallax:parallax /data

USER parallax

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

CMD ["parallax", "serve", "--host", "0.0.0.0", "--port", "8080"]
