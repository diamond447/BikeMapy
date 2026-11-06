# The builder resolves the locked runtime environment with uv; the runtime
# image receives only that environment and the application source. Neither
# uv nor the GDAL command-line tools (which pull in a system Python and NumPy)
# nor the PROJ headers are needed to run Django: GeoDjango loads libgdal.
FROM python:3.13-slim-trixie AS builder

# Copy the official uv binary without running an unpinned installer script.
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /bin/

ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY backend ./backend
COPY scripts ./scripts
RUN uv sync --locked --no-dev

FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend \
  PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# libgdal brings GEOS and PROJ as dependencies. GeoDjango finds the shared
# libraries through the ldconfig cache.
RUN apt-get update \
  && apt-get install --no-install-recommends -y libgdal36 \
  && rm -rf /var/lib/apt/lists/*

# The virtual environment keeps its absolute /app paths, including the
# editable install of the project, so it is copied to the same location.
COPY --from=builder /app/.venv /app/.venv
COPY pyproject.toml uv.lock ./

# Keep the image boundary explicit. Deployment configuration, operational
# data, and local review material are intentionally not part of the backend
# image even though the build context is the repository root.
COPY backend ./backend
COPY scripts ./scripts
# Build the immutable static asset set into the release image. Production
# settings enable the manifest storage; local Compose overrides DEBUG=true.
RUN DJANGO_DEBUG=false python backend/manage.py collectstatic --noinput
EXPOSE 8000
