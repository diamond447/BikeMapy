FROM python:3.13-slim

# Copy the official uv binary without running an unpinned installer script.
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend
WORKDIR /app

RUN apt-get update \
  && apt-get install --no-install-recommends -y binutils libproj-dev gdal-bin \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .
RUN uv sync --locked --no-dev
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
