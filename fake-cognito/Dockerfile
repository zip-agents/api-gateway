# Local stand-in for Amazon Cognito (app.py). Development only: docker compose runs it.
FROM python:3.12.15-slim-bookworm AS runtime
RUN pip install --no-cache-dir "starlette==1.8.0" "uvicorn==0.54.0" "pyjwt[crypto]==2.15.1" \
    "cryptography==50.0.2"
RUN useradd --uid 10001 --create-home cognito && mkdir /data && chown 10001 /data
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001
WORKDIR /app
COPY app.py ./
EXPOSE 9229
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9229", "--no-access-log"]

# Contract tests: the fake against the client libraries the gateway uses (boto3, httpx,
# PyJWT). make fake-cognito-test
FROM runtime AS test
USER root
RUN pip install --no-cache-dir "pytest==9.1.1" "httpx==0.28.1" "boto3==1.43.114"
USER 10001
COPY tests ./tests
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
