# The runtime image gets only the virtualenv and the source; GeoDjango needs
# libgdal, not uv or the GDAL command-line tools.
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
# Set in the image, not in Compose: an older image's gunicorn rejects the flag.
ENV GUNICORN_CMD_ARGS="--no-control-socket"
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

COPY backend ./backend
COPY scripts ./scripts
RUN DJANGO_DEBUG=false python backend/manage.py collectstatic --noinput
EXPOSE 8000
