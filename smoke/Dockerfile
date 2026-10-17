# Smoke-test runner: Python, pytest, httpx, curl and the archive tools the contract names.
# Run through docker compose (`make smoke`), on the same network as the gateway.
FROM python:3.12.15-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip p7zip-full \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "pytest==9.1.1" "httpx==0.28.1"
RUN useradd --uid 10001 --create-home smoke
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001
WORKDIR /smoke
COPY test_smoke.py ./
ENTRYPOINT ["python", "-m", "pytest", "-ra", "-p", "no:cacheprovider", "test_smoke.py"]
