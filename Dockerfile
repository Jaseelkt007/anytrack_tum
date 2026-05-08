# Single image used for both the api and worker services.
# Build:   docker compose build
# Run api:    docker compose --profile api up
# Run worker: docker compose --profile worker up
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps that PyGithub / psycopg occasionally need at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Default to the worker; the api service overrides via `command`.
CMD ["python", "-m", "worker"]
