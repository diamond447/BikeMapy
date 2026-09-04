FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend
WORKDIR /app

RUN apt-get update \
  && apt-get install --no-install-recommends -y binutils libproj-dev gdal-bin \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./
COPY backend ./backend
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
EXPOSE 8000
