# PeerShop — Docker image
# Multi-stage build: builder installs deps, runner is lean runtime.
# One image, three entrypoints (web / admin / bot) selected by $PEERSHOP_ROLE.
#
# Platform: NOT hardcoded. Build natively for whatever host runs the build:
#   - On the Pi (arm64): docker build -f docker/Dockerfile -t peershop:arm64-dev .
#     produces an arm64 image for local dev -- no QEMU emulation needed/possible
#     if binfmt handlers are not registered (they are NOT persistent across
#     reboots/docker restarts without re-running the tonistiigi/binfmt installer).
#   - For the GCP VM (amd64): either build FROM an amd64 host directly, or
#     pass --platform linux/amd64 explicitly AND ensure QEMU binfmt handlers
#     are registered first if building cross-arch from the Pi:
#       docker run --privileged --rm tonistiigi/binfmt --install all
#       docker build --platform linux/amd64 -f docker/Dockerfile -t peershop:latest .
#   Previously this file hardcoded FROM --platform=linux/amd64, which broke
#   native arm64 builds on the Pi once QEMU handlers were lost (exec format
#   error on first RUN). Fixed 2026-10-04 by letting --platform flow through
#   normally instead of overriding it in the Dockerfile.

ARG PYTHON_VERSION=3.13

# ── Stage 1: dependency builder ─────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

WORKDIR /build

# Build deps for numpy/onnxruntime/opencv/rembg native wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libgl1 \
    libglib2.0-0 \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: lean runtime ────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runner

# Runtime system libs needed by opencv-python-headless / onnxruntime / rembg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    curl \
    sqlite3 \
  && rm -rf /var/lib/apt/lists/*

# Non-root user for container security
RUN useradd -m -u 1000 peershop

WORKDIR /app

# Bring in installed packages from the builder stage
COPY --from=builder /install /usr/local

# Application code (see .dockerignore for exclusions: .git, .venv, __pycache__,
# media/seed test fixtures, db backups, .claude/, team/ docs)
COPY . .

# Data directories that must persist across container restarts/redeploys —
# mounted as a volume in compose / a GCE persistent disk / Cloud SQL+GCS in
# the eventual cloud topology. Created here so permissions are correct even
# on a fresh volume mount.
RUN mkdir -p /app/media/products /app/media/seed /app/db \
    && if [ -f /app/peershop.db ]; then cp /app/peershop.db /app/peershop.db.image-seed; fi \
    && cp -a /app/media /app/media.image-seed \
    && chown -R peershop:peershop /app

USER peershop

# Default entrypoint role; overridden per-service in docker-compose.yml.
# Values: web | admin | bot
ENV PEERSHOP_ROLE=web
ENV PEERSHOP_HOST=0.0.0.0
ENV PEERSHOP_REVIEW_HOST=0.0.0.0
ENV PYTHONUNBUFFERED=1

EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD if [ "$PEERSHOP_ROLE" = "web" ]; then \
        curl -sf http://127.0.0.1:${PEERSHOP_PORT:-8000}/health || exit 1; \
      else \
        exit 0; \
      fi

COPY docker/entrypoint.sh /app/docker/entrypoint.sh
ENTRYPOINT ["/app/docker/entrypoint.sh"]
