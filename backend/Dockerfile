# ---------- base: shared by every stage ----------
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg is needed to convert/split meeting recordings before transcription
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as a non-root user
RUN useradd --create-home --uid 1000 meetmind \
    && mkdir -p /data/uploads \
    && chown -R meetmind:meetmind /app /data

# ---------- test: base + pytest (docker compose --profile test run --rm tests) ----------
FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
USER meetmind
CMD ["pytest", "-q"]

# ---------- runtime: what api / worker / migrate run ----------
FROM base AS runtime
USER meetmind
EXPOSE 8000
# Uvicorn (ASGI) with 3 worker processes; --proxy-headers trusts X-Forwarded-For from nginx so
# rate limits see the real client IP. The api container is only reachable through nginx.
CMD ["uvicorn", "meetmind.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "3", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
